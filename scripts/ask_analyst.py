#!/usr/bin/env python3
"""Ask the analyst a sequence of questions in one session (memory carries across them).

    python3 scripts/ask_analyst.py --question "Who is cheapest overall, like-for-like?" --question "..."
    python3 scripts/ask_analyst.py --questions-file questions.txt

Needs GEMINI_API_KEY (or ANTHROPIC_API_KEY with --provider anthropic). Reads data/rfx.db.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import analyst  # noqa: E402
from core.channel import load_vendors  # noqa: E402
from core.db import init_db  # noqa: E402
from core.llm import make_client, provider_name  # noqa: E402
from core.rfx_copilot import load_doc  # noqa: E402
from core.schema import RFx  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", action="append", default=[])
    ap.add_argument("--questions-file")
    ap.add_argument("--provider", choices=["gemini", "anthropic"], default=None)
    ap.add_argument("--model", default="gemini-3.1-flash-lite-preview")
    ap.add_argument("--db", default=str(ROOT / "data" / "rfx.db"))
    args = ap.parse_args()

    questions = list(args.question)
    if args.questions_file:
        questions += [q.strip() for q in Path(args.questions_file).read_text(encoding="utf-8").splitlines() if q.strip()]
    if not questions:
        ap.error("give at least one --question or --questions-file")

    conn = init_db(args.db)
    rfx_id = conn.execute("SELECT id FROM rfx LIMIT 1").fetchone()[0]
    rfx = RFx.model_validate(load_doc(ROOT / "data" / "rfx" / "rfx.json"))
    names = {v.id: v.name for v in load_vendors(ROOT / "data" / "vendors.json")}
    responses = {vid: (conn.execute("SELECT id FROM response WHERE rfx_id=? AND vendor_id=? "
                                    "ORDER BY received_at DESC LIMIT 1", (rfx_id, vid)).fetchone() or [None])[0]
                 for vid in names}
    session = analyst.AnalystSession(conn=conn, rfx=rfx, responses=responses, names=names)
    client = make_client(args.provider)
    print(f"provider={provider_name(args.provider)} model={args.model} questions={len(questions)}\n")

    for i, q in enumerate(questions, start=1):
        print(f"{'=' * 78}\nQ{i}: {q}\n{'-' * 78}")
        turn = analyst.ask(client, session, q, model=args.model)
        for c in turn["tool_calls"]:
            flag = " (ERROR)" if c["error"] else ""
            print(f"  [tool] {c['tool']}{flag} args={json.dumps(c['args'], default=str)[:160]}")
        print()
        print(turn["answer"] or "(no answer text)")
        for f in turn["files"]:
            print(f"\n  [file] {f}")
        for ch in turn["charts"]:
            out = ROOT / "data" / "outbox" / "analysis" / f"q{i}_{ch}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(session.charts[ch])
            print(f"\n  [chart] {out}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
