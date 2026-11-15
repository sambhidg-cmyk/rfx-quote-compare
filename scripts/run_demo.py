#!/usr/bin/env python3
"""Run the one-click demo from the command line, printing each stage as it happens.

    python3 scripts/run_demo.py                 # uses caches; live calls only where nothing is cached
    python3 scripts/run_demo.py --fresh         # bypass caches and make every model call live

Resets data/rfx.db and the per-run folders first. Needs GEMINI_API_KEY unless everything is cached.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.demo import run_demo  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default="gemini-3.1-flash-lite-preview")
    ap.add_argument("--analyst-model", default="gemini-3.1-flash-lite-preview")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    for ev in run_demo(provider=args.provider, model=args.model, analyst_model=args.analyst_model, fresh=args.fresh):
        mark = {"running": "...", "done": " ok", "cached": "CACHE", "failed": "FAIL", "skipped": "skip"}[ev.status]
        print(f"[{time.time() - t0:6.1f}s] {ev.stage:<11} {mark}  {ev.detail}", flush=True)
        if ev.stage == "Ask" and ev.status in ("done", "cached") and "answer" in ev.data:
            print("          " + ev.data["answer"].replace("\n", "\n          ")[:600], flush=True)
    print(f"total {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
