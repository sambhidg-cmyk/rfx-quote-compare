"""One-click demo: resets state and walks the stages in order, streaming an event per step.

    Create RFx -> Send -> Receive -> Extract -> Compare -> Ask

Every step reports whether it made live model calls or used the cache. Caches are keyed by content
(file bytes, RFx, model, prompt version, data digest), so a rerun is fast and a changed input is never
served from cache. `fresh=True` bypasses both caches.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from core import analyst
from core import award as award_mod
from core.channel import SimulatedChannel, load_vendors
from core.compare import build_matrix
from core.db import init_db
from core.extract import agent as agent_mod
from core.extract import cache as extract_cache
from core.extract import run_extraction
from core.extract.store import upsert_rfx
from core.rfx_copilot import load_doc
from core.schema import RFx

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "rfx.db"
ANALYST_CACHE = ROOT / "data" / "cache" / "demo_analyst"
STAGES = ["Create RFx", "Send", "Receive", "Extract", "Compare", "Ask"]

DEMO_QUESTIONS = [
    "Who is cheapest overall, like-for-like?",
    "Split the award: cheapest per line, but only among vendors who cleared the quality questionnaire.",
    "What if we cap any vendor at 40%?",
    "Which vendor is cheapest on the full 30-line basket?",
    "Which prices should I double-check before signing?",
    "Chart the award value by vendor from the current scenario.",
    "Export the award lines to Excel.",
    "Draft the award memo for the VP.",
]


@dataclass
class Event:
    stage: str
    status: str  # running | done | cached | failed | skipped
    detail: str = ""
    live_calls: int = 0
    cache_hits: int = 0
    data: Dict[str, Any] = field(default_factory=dict)


def reset_state() -> List[str]:
    """Remove the database and the per-run folders. Model caches are kept unless fresh=True asks otherwise."""
    removed = []
    if DB.exists():
        # Reset in place: drop the tables but keep the same file. Deleting the file would leave a running
        # app (which holds an open connection) reading a file that no longer exists: "disk I/O error".
        conn = sqlite3.connect(str(DB))
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for name in names:
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            conn.commit()
        finally:
            conn.close()
        removed.append(str(DB.relative_to(ROOT)) + " (tables dropped)")
    for sub in ("inbox", "outbox/sent", "outbox/analysis", "outbox/drafts"):
        d = ROOT / "data" / sub
        if d.exists():
            shutil.rmtree(d)
            removed.append(f"data/{sub}")
    return removed


def _digest(conn) -> str:
    h = hashlib.sha256()
    for q in ("SELECT id, unit_price_json, price_uom_json FROM quote ORDER BY id",
              "SELECT quote_id, unit_price_inr_ex_tax FROM normalized_quote ORDER BY quote_id",
              "SELECT id, status FROM review_item ORDER BY id",
              "SELECT id, answer_json FROM questionnaire_answer ORDER BY id"):
        for row in conn.execute(q):
            h.update(json.dumps(tuple(row), default=str).encode())
    return h.hexdigest()


def _analyst_key(digest: str, model: str, index: int, question: str) -> str:
    return extract_cache.make_key(digest, model, analyst.SYSTEM, index, question)


def run_demo(*, provider: Optional[str], model: str, analyst_model: str, fresh: bool = False,
             questions: List[str] = DEMO_QUESTIONS) -> Iterator[Event]:
    """Yields one Event per step. Callers render them as they arrive."""
    from core.llm import make_client, provider_name

    removed = reset_state()
    yield Event("Create RFx", "running", f"reset: removed {', '.join(removed) or 'nothing'}")
    rfx = RFx.model_validate(load_doc(ROOT / "data" / "rfx" / "rfx.json"))
    conn = init_db(DB)
    upsert_rfx(conn, rfx)
    from core.rfx_export import approve_and_export

    approve_and_export(rfx, 0, ROOT / "data" / "outbox")
    yield Event("Create RFx", "done", f"{rfx.id}: {len(rfx.line_items)} lines, {len(rfx.questionnaire)} questions, "
                                      "approved and exported")

    try:
        client = make_client(provider)
        has_key = True
    except RuntimeError:
        client, has_key = None, False

    vendors = load_vendors(ROOT / "data" / "vendors.json")
    ch = SimulatedChannel(conn, ROOT / "data", ROOT / "data" / "responses", vendors)
    yield Event("Send", "running", "writing one message per vendor")
    sent = ch.send_rfx(rfx, [v.id for v in vendors])
    yield Event("Send", "done", f"{len(sent)} simulated messages written to data/outbox/sent/")

    yield Event("Receive", "running", "copying vendor files into the inbox")
    results = ch.simulate_replies(rfx.id)
    got = [r for r in results if r.get("result") == "received"]
    yield Event("Receive", "done", f"{len(got)} responses registered, {sum(r.get('files', 0) for r in got)} files")

    names = {v.id: v.name for v in vendors}
    responses = {s["vendor_id"]: s["response_id"] or None for s in ch.vendor_status(rfx.id)}
    yield Event("Extract", "running", "reading every file per vendor"
                + ("" if has_key else " (no model key: cache only)"))
    total_live = total_hits = 0
    for v in vendors:
        rid = responses.get(v.id)
        if not rid:
            yield Event("Extract", "skipped", f"{v.name}: no response")
            continue
        folder = ch.inbox_dir / rfx.id / v.id
        live = hits = 0
        t0 = time.time()

        def cb(done, n, label, _v=v):
            nonlocal live, hits
            if label.endswith("(cached)"):
                hits += 1
            else:
                live += 1

        use_client = client if has_key else _CacheOnly()
        try:
            run_extraction(conn, rid, folder, rfx, use_client, model=model, force=fresh, progress=cb)
            total_live += live
            total_hits += hits
            kind = "cached" if live == 0 else "done"
            yield Event("Extract", kind, f"{v.name}: {hits} cached, {live} live calls, {time.time() - t0:.0f}s",
                        live_calls=live, cache_hits=hits)
        except Exception as e:  # one vendor must not stop the demo
            yield Event("Extract", "failed", f"{v.name}: {str(e)[:160]}", live_calls=live, cache_hits=hits)
    yield Event("Extract", "done", f"total {total_hits} cached batches, {total_live} live model calls",
                live_calls=total_live, cache_hits=total_hits)

    matrix = build_matrix(conn, rfx, responses, names)
    partial = matrix.partial_basket()
    cov = ", ".join(f"{name} {matrix.coverage(vid)[0]}/{matrix.coverage(vid)[1]}" for vid, name in matrix.vendors)
    yield Event("Compare", "done", f"coverage: {cov}; open review items: {matrix.trust_counts()['review']}",
                data={"coverage": cov, "trust": matrix.trust_counts()})

    # Ask: the analyst answers the demo questions in one session; answers are cached with their messages.
    if not has_key and not _all_cached(conn, rfx, analyst_model, questions):
        yield Event("Ask", "skipped", "no model key and the analyst answers are not cached: set GEMINI_API_KEY")
        conn.close()
        return
    session = analyst.AnalystSession(conn=conn, rfx=rfx, responses=responses, names=names)
    digest = _digest(conn)
    ask_live = ask_hits = 0
    for i, q in enumerate(questions):
        key = _analyst_key(digest, analyst_model, i, q)
        path = ANALYST_CACHE / f"{key}.json"
        if not fresh and path.exists():
            entry = json.loads(path.read_text(encoding="utf-8"))
            _replay(session, entry)
            ask_hits += 1
            yield Event("Ask", "cached", f"Q{i + 1} (cached): {q}", cache_hits=1, data={"answer": entry["turn"]["answer"]})
            continue
        if not has_key:
            yield Event("Ask", "skipped", f"Q{i + 1}: no model key and no cached answer")
            break
        before = len(session.messages)
        yield Event("Ask", "running", f"Q{i + 1}: {q}")
        try:
            turn = analyst.ask(client, session, q, model=analyst_model)
        except Exception as e:
            yield Event("Ask", "failed", f"Q{i + 1}: {str(e)[:160]}")
            break
        entry = _store(session, turn, before)
        ANALYST_CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, default=str), encoding="utf-8")
        ask_live += 1
        yield Event("Ask", "done", f"Q{i + 1}: {len(turn['tool_calls'])} tool calls",
                    live_calls=1, data={"answer": turn["answer"], "tool_calls": turn["tool_calls"]})
    yield Event("Ask", "done", f"{ask_hits} answers from cache, {ask_live} live", live_calls=ask_live,
                cache_hits=ask_hits)
    conn.close()


class _CacheOnly:
    class messages:  # noqa: N801
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("cache miss and no model key")


def _all_cached(conn, rfx, model, questions) -> bool:
    digest = _digest(conn)
    return all((ANALYST_CACHE / f"{_analyst_key(digest, model, i, q)}.json").exists()
               for i, q in enumerate(questions))


def _store(session: analyst.AnalystSession, turn: Dict[str, Any], before: int) -> Dict[str, Any]:
    charts = {c: base64.b64encode(session.charts[c]).decode("ascii") for c in turn["charts"]}
    return {"turn": {k: v for k, v in turn.items()}, "messages": session.messages[before:], "charts": charts,
            "tables": {n: session.tables[n] for n in turn["tables"] if n in session.tables}}


def _replay(session: analyst.AnalystSession, entry: Dict[str, Any]) -> None:
    session.messages.extend(entry["messages"])
    for name, b64 in entry["charts"].items():
        session.charts[name] = base64.b64decode(b64)
    session.tables.update(entry.get("tables", {}))
    session.files.extend(entry["turn"].get("files", []))
    session.transcript.append(entry["turn"])
