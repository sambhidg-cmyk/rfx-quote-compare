#!/usr/bin/env python3
"""Rebuild the comparison database from cached model extractions. Makes NO model calls.

Reads the disk cache written by the extraction agent under a given prompt version and model, persists
the results as canonical rows, and copies each vendor's files into data/inbox so sources resolve.

    python3 scripts/load_cached_extractions.py --prompt extract-v1 --model gemini-3-flash-preview

Recreates data/rfx.db (the default app database). Vendors with no cache entries are reported and skipped.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db import init_db  # noqa: E402
from core.extract import agent as agent_mod  # noqa: E402
from core.extract.store import persist_extraction, upsert_rfx  # noqa: E402
from core.rfx_copilot import load_doc  # noqa: E402
from core.schema import RFx  # noqa: E402


class CacheOnlyClient:
    class messages:  # noqa: N801  (mirrors the SDK shape)
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("cache miss: this vendor/batch was not extracted with the given prompt and model")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="prompt version the cache was written with, e.g. extract-v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--db", default=str(ROOT / "data" / "rfx.db"))
    args = ap.parse_args()

    agent_mod.PROMPT_VERSION = args.prompt  # cache keys include the prompt version
    rfx = RFx.model_validate(load_doc(ROOT / "data" / "rfx" / "rfx.json"))
    vendors = json.loads((ROOT / "data" / "vendors.json").read_text(encoding="utf-8"))

    db = Path(args.db)
    if db.exists():
        db.unlink()
    conn = init_db(db)
    upsert_rfx(conn, rfx)
    for v in vendors:
        conn.execute("INSERT INTO vendor (id, name, email) VALUES (?, ?, ?)", (v["id"], v["name"], v["email"]))
    conn.commit()

    for v in vendors:
        vid = v["id"]
        src = ROOT / "data" / "responses" / vid
        dst = ROOT / "data" / "inbox" / rfx.id / vid
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        rid = f"{rfx.id}:{vid}:cached"
        conn.execute(
            "INSERT INTO response (id, rfx_id, vendor_id, received_at, channel, status) VALUES (?,?,?,?,?,'received')",
            (rid, rfx.id, vid, datetime.now(timezone.utc).isoformat(timespec="seconds"), v["inbound_channel"]))
        try:
            result = agent_mod.extract_response(CacheOnlyClient(), dst, rfx, model=args.model,
                                                cache_dir=ROOT / "data" / "cache" / "extract")
        except RuntimeError as e:
            print(f"{vid}: skipped ({e})")
            conn.execute("DELETE FROM response WHERE id = ?", (rid,))
            conn.commit()
            continue
        summary = persist_extraction(conn, rid, rfx, result)
        print(f"{vid}: {summary}")
    conn.close()
    print(f"wrote {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
