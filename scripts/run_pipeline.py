#!/usr/bin/env python3
"""Run the pipeline end to end from a clean database, with the real model.

    python3 scripts/run_pipeline.py --provider gemini --model gemini-3.1-flash-lite-preview

Steps: recreate data/rfx.db; load the RFx and vendors; copy each vendor's files into the inbox as if they
arrived; extract each response with the model (cached per file hash); store canonical rows and review items.
A vendor that fails is reported and skipped; the others still run.
"""

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db import init_db  # noqa: E402
from core.extract import run_extraction  # noqa: E402
from core.extract.store import upsert_rfx  # noqa: E402
from core.llm import default_model, make_client, provider_name  # noqa: E402
from core.rfx_copilot import load_doc  # noqa: E402
from core.schema import RFx  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["gemini", "anthropic"], default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--force", action="store_true", help="ignore the extraction cache")
    ap.add_argument("--vendor", action="append", help="only these vendor ids")
    ap.add_argument("--keep-db", action="store_true", help="add to the existing database instead of recreating it")
    args = ap.parse_args()

    model = args.model or default_model(args.provider)
    client = make_client(args.provider)
    print(f"provider={provider_name(args.provider)} model={model} force={args.force}")

    db = ROOT / "data" / "rfx.db"
    if db.exists() and not args.keep_db:
        db.unlink()
    conn = init_db(db)
    rfx = RFx.model_validate(load_doc(ROOT / "data" / "rfx" / "rfx.json"))
    upsert_rfx(conn, rfx)
    vendors = json.loads((ROOT / "data" / "vendors.json").read_text(encoding="utf-8"))
    for v in vendors:
        conn.execute("INSERT INTO vendor (id, name, email) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
                     (v["id"], v["name"], v["email"]))
    conn.commit()

    report = []
    for v in vendors:
        vid = v["id"]
        if args.vendor and vid not in args.vendor:
            continue
        src = ROOT / "data" / "responses" / vid
        dst = ROOT / "data" / "inbox" / rfx.id / vid
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        rid = f"{rfx.id}:{vid}:{datetime.now(timezone.utc):%H%M%S}"
        conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel, status) "
                     "VALUES (?,?,?,?,?,'received')",
                     (rid, rfx.id, vid, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      v["inbound_channel"]))
        conn.commit()
        t0 = time.time()
        try:
            _, summary = run_extraction(conn, rid, dst, rfx, client, model=model, force=args.force,
                                        progress=lambda d, n, label: print(f"  {vid}: {d}/{n} {label}", flush=True))
            report.append((vid, "ok", summary, round(time.time() - t0)))
        except Exception as e:  # one vendor must not stop the run
            conn.execute("UPDATE response SET status='received' WHERE id=?", (rid,))
            conn.commit()
            report.append((vid, f"failed: {str(e)[:160]}", None, round(time.time() - t0)))
        print(f"{vid}: {report[-1][1]} ({report[-1][3]}s)", flush=True)

    print("\nSUMMARY")
    for vid, status, summary, secs in report:
        print(f"  {vid}: {status}  {summary or ''}  {secs}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
