#!/usr/bin/env python3
"""Evaluate extraction against data/ground_truth.json.

Runs the real extraction agent on each vendor folder (or reads the disk cache with --cache-only),
then compares the output with the ground truth per vendor and field, and lists every miss.

    python3 tests/eval/eval_extraction.py                 # all vendors, real model calls
    python3 tests/eval/eval_extraction.py --vendor vendor_b --force
    python3 tests/eval/eval_extraction.py --cache-only    # no API calls; uses cached output

Needs ANTHROPIC_API_KEY unless --cache-only. Results are also written to data/eval/.

Ground truth is read here and nowhere under app/ or core/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.extract import cache  # noqa: E402
from core.extract.agent import DEFAULT_BATCH_SIZE, extract_response  # noqa: E402
from core.llm import default_model, make_client, provider_name  # noqa: E402
from core.extract.normalize import canonical_currency, normalize_price  # noqa: E402
from core.extract.store import parse_number  # noqa: E402
from core.rfx_copilot import load_doc  # noqa: E402
from core.schema import RFx  # noqa: E402

GT_PATH = ROOT / "data" / "ground_truth.json"
RFX_PATH = ROOT / "data" / "rfx" / "rfx.json"
RESP = ROOT / "data" / "responses"
OUT = ROOT / "data" / "eval"
VENDORS = ["vendor_a", "vendor_b", "vendor_c", "vendor_d", "vendor_e"]


class CacheOnlyClient:
    def create(self, **kwargs):
        raise RuntimeError("cache miss in --cache-only mode; run once with a key to fill the cache")


def gt_basis(stated_uom: Optional[str], pack: Optional[float], currency: Optional[str]) -> Tuple[str, Optional[float], str]:
    """Stated unit as (uom, pack_size, currency) from the ground truth's wording."""
    s = (stated_uom or "").lower()
    cur = currency or "INR"
    if "100" in s:
        return "PACK", 100.0, cur
    if "kg" in s:
        return "KG", None, cur
    if "sq m" in s:
        return "SQM", None, cur
    if any(w in s for w in ("pc", "piece", "roll")):
        return "EA", None, cur
    return s, pack, cur


def freight_class(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    if "extra" in t or "excluded" in t or "not included" in t:
        return "extra"
    if "included" in t:
        return "included"
    return None


def numbers(text: str) -> set:
    """Digit runs in an answer, with thousands separators removed and leading zeros dropped."""
    return {n.lstrip("0") or "0" for n in re.findall(r"\d+", text.replace(",", ""))}


def score_vendor(vendor: str, gt: Dict[str, Any], rfx: RFx, result, usd_inr: float) -> Dict[str, Any]:
    misses: List[dict] = []
    counts: Dict[str, List[bool]] = defaultdict(list)
    gt_lines = gt["vendors"][vendor]["lines"]

    for li in rfx.line_items:
        g = gt_lines[li.id]
        out = result.lines.get(li.id)
        ex_quoted = bool(out and out.status == "quoted")
        gt_quoted = g["status"] == "quoted"
        ok = gt_quoted == ex_quoted
        counts["line_status"].append(ok)
        if not ok:
            misses.append({"line": li.id, "field": "line_status", "expected": g["status"],
                           "got": out.status if out else "missing"})
        if not (gt_quoted and ex_quoted):
            continue

        price = parse_number(out.unit_price.value)
        canon = normalize_price(price, out.price_uom.value, parse_number(out.pack_size.value)
                                if out.pack_size else None, out.currency.value if out.currency else "INR",
                                li.uom.value.value).value if price is not None else None
        exp = g["canonical_unit_price_inr_ex_tax"]
        price_ok = canon is not None and abs(canon - exp) <= max(0.01, 0.005 * exp)
        counts["price_inr_ex_tax"].append(price_ok)
        if not price_ok:
            misses.append({"line": li.id, "field": "price_inr_ex_tax", "expected": exp,
                           "got": None if canon is None else round(canon, 4),
                           "note": f"stated {out.unit_price.value} {out.price_uom.value} "
                                   f"pack={out.pack_size.value if out.pack_size else None}"})

        b_uom, b_pack, b_cur = gt_basis(g["stated_uom"], g["stated_pack_size"], g["stated_currency"])
        e_uom = out.price_uom.value
        e_pack = parse_number(out.pack_size.value) if out.pack_size else None
        basis_ok = e_uom == b_uom and (b_uom != "PACK" or e_pack == b_pack)
        counts["stated_basis"].append(basis_ok)
        if not basis_ok:
            misses.append({"line": li.id, "field": "stated_basis", "expected": f"{b_uom} pack={b_pack}",
                           "got": f"{e_uom} pack={e_pack}"})

        e_cur = (out.currency.value if out.currency else "INR") or "INR"
        cur_ok = canonical_currency(str(e_cur)) == canonical_currency(b_cur)
        counts["currency"].append(cur_ok)
        if not cur_ok:
            misses.append({"line": li.id, "field": "currency", "expected": b_cur, "got": e_cur})

    comm = result.commercial
    gt_freight = gt["vendors"][vendor]["lines"][rfx.line_items[0].id]["freight_status"]
    got_freight = freight_class(comm.freight.value if comm and comm.freight else None)
    # 'not_stated' means the files never mention freight, so the right answer is no freight value
    freight_ok = (got_freight is None) if gt_freight == "not_stated" else (got_freight == gt_freight)
    counts["freight"].append(freight_ok)
    if not freight_ok:
        misses.append({"line": "commercial", "field": "freight", "expected": gt_freight, "got": got_freight})

    gt_discount = gt["vendors"][vendor]["lines"][rfx.line_items[0].id]["discount"]
    got_discount = bool(comm and comm.discount and comm.discount.value)
    counts["discount_present"].append(got_discount == bool(gt_discount))
    if got_discount != bool(gt_discount):
        misses.append({"line": "commercial", "field": "discount_present", "expected": bool(gt_discount),
                       "got": got_discount})

    for q in rfx.questionnaire:
        g_ans = gt["vendors"][vendor]["questionnaire"][q.id]["expected_answer"]
        qa = result.questionnaire.get(q.id)
        got = qa.answer.value if qa and qa.status == "answered" and qa.answer else None
        present_ok = (g_ans is None) == (got is None)
        counts["q_answered"].append(present_ok)
        if not present_ok:
            misses.append({"line": q.id, "field": "answered", "expected": g_ans, "got": got})
            continue
        if g_ans is not None and numbers(g_ans):
            num_ok = numbers(g_ans) <= numbers(str(got))
            counts["q_numbers"].append(num_ok)
            if not num_ok:
                misses.append({"line": q.id, "field": "answer_numbers", "expected": g_ans, "got": got})

    value_checks = list(result.checks)
    verified = sum(1 for c in value_checks if c["status"] == "verified")
    visual = sum(1 for c in value_checks if c["status"] == "visual_only")
    n = len(value_checks)
    return {"counts": counts, "misses": misses,
            "verified_rate": verified / n if n else None,
            "visual_rate": visual / n if n else None,
            "value_checks": len(value_checks),
            "cache_hits": sum(b.cache_hit for b in result.batches), "batches": len(result.batches)}


def pct(xs: List[bool]) -> str:
    return f"{100 * sum(xs) / len(xs):5.1f}% ({sum(xs)}/{len(xs)})" if xs else "   n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", action="append", choices=VENDORS)
    ap.add_argument("--force", action="store_true", help="ignore the cache and call the model")
    ap.add_argument("--cache-only", action="store_true", help="never call the model")
    ap.add_argument("--provider", choices=["gemini", "anthropic"], default=None,
                    help="default: EXTRACT_PROVIDER, else gemini")
    ap.add_argument("--model", default=None, help="default: EXTRACT_MODEL or the provider default")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = ap.parse_args()

    client: Any
    if args.cache_only:
        client = type("C", (), {"messages": CacheOnlyClient()})()
    else:
        try:
            client = make_client(args.provider)
        except RuntimeError as e:
            print(f"{e}. Set it, or use --cache-only.", file=sys.stderr)
            return 2
    args.model = args.model or default_model(args.provider)
    print(f"provider={provider_name(args.provider)} model={args.model} batch_size={args.batch_size}")

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    rfx = RFx.model_validate(load_doc(RFX_PATH))
    usd_inr = float(gt["usd_inr_rate_used"])
    vendors = args.vendor or VENDORS

    report: Dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "model": args.model, "batch_size": args.batch_size, "vendors": {}}
    all_counts: Dict[str, List[bool]] = defaultdict(list)
    print(f"{'vendor':<10}{'line status':<18}{'price (INR ex-tax)':<22}{'stated basis':<18}"
          f"{'currency':<14}{'freight':<12}{'q answered':<16}{'q numbers':<16}{'text/visual':<12}cache")
    all_misses = []
    for v in vendors:
        try:
            result = extract_response(client, RESP / v, rfx, model=args.model, batch_size=args.batch_size,
                                      force=args.force)
        except Exception as e:  # keep scoring the other vendors
            print(f"{v:<10} FAILED: {e}")
            report["vendors"][v] = {"error": str(e)}
            continue
        s = score_vendor(v, gt, rfx, result, usd_inr)
        for k, xs in s["counts"].items():
            all_counts[k] += xs
        vr = ("n/a" if s["verified_rate"] is None else
              f"{100 * s['verified_rate']:.0f}%/{100 * s['visual_rate']:.0f}%v")
        print(f"{v:<10}{pct(s['counts']['line_status']):<18}{pct(s['counts']['price_inr_ex_tax']):<22}"
              f"{pct(s['counts']['stated_basis']):<18}{pct(s['counts']['currency']):<14}"
              f"{pct(s['counts']['freight']):<12}{pct(s['counts']['q_answered']):<16}"
              f"{pct(s['counts']['q_numbers']):<16}{vr:<10}{s['cache_hits']}/{s['batches']}")
        for m in s["misses"]:
            all_misses.append({"vendor": v, **m})
        report["vendors"][v] = {"counts": {k: [sum(x), len(x)] for k, x in s["counts"].items()},
                                "verified_rate": s["verified_rate"], "visual_rate": s["visual_rate"],
                                "misses": s["misses"]}

    print(f"{'ALL':<10}{pct(all_counts['line_status']):<18}{pct(all_counts['price_inr_ex_tax']):<22}"
          f"{pct(all_counts['stated_basis']):<18}{pct(all_counts['currency']):<14}"
          f"{pct(all_counts['freight']):<12}{pct(all_counts['q_answered']):<16}{pct(all_counts['q_numbers']):<16}")

    print(f"\nMisses ({len(all_misses)}):")
    for m in all_misses:
        print(f"  {m['vendor']:<9} {m['line']:<11} {m['field']:<18} expected={m.get('expected')!s:<22} "
              f"got={m.get('got')!s:<22} {m.get('note', '')}")

    if not any('counts' in v for v in report['vendors'].values()):
        print('\nNo vendor was scored, so no report was written.', file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT / f"extraction_{stamp}.json"
    report["misses"] = all_misses
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
