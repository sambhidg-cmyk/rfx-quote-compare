"""The eval scorer must give 100% when the extraction output is exactly the ground truth.
This checks the scorer and the price normalization, not the model."""

import importlib.util
import json
from pathlib import Path

from core.extract.agent import ExtractionResult
from core.extract.models import BatchOut, CommercialOut, FieldOut, LineOut, QAOut
from core.rfx_copilot import load_doc
from core.schema import RFx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_extraction", ROOT / "tests" / "eval" / "eval_extraction.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def _f(value, conf="high"):
    return FieldOut(value=value, source_file="f", locator="l", snippet="s", confidence=conf)


def _oracle(vendor, gt, rfx):
    res = ExtractionResult(folder=vendor, model="oracle")
    for li in rfx.line_items:
        g = gt["vendors"][vendor]["lines"][li.id]
        if g["status"] != "quoted":
            res.lines[li.id] = LineOut(line_id=li.id, status="not_quoted")
            continue
        uom, pack, cur = ev.gt_basis(g["stated_uom"], g["stated_pack_size"], g["stated_currency"])
        pf = _f(g["stated_value"]) if pack is None else _f(g["stated_value"])
        res.lines[li.id] = LineOut(
            line_id=li.id, status="quoted", unit_price=pf, price_uom=_f(uom),
            pack_size=_f(pack) if uom == "PACK" else None, currency=_f(cur))
    freight = gt["vendors"][vendor]["lines"][rfx.line_items[0].id]["freight_status"]
    discount = gt["vendors"][vendor]["lines"][rfx.line_items[0].id]["discount"]
    res.commercial = CommercialOut(freight=None if freight == "not_stated" else _f(f"freight {freight}"),
                                   discount=_f(discount) if discount else None)
    for q in rfx.questionnaire:
        ans = gt["vendors"][vendor]["questionnaire"][q.id]["expected_answer"]
        res.questionnaire[q.id] = (QAOut(question_id=q.id, status="answered", answer=_f(ans)) if ans
                                   else QAOut(question_id=q.id, status="not_answered"))
    res.checks = []
    return res


def test_scorer_is_perfect_on_ground_truth_itself():
    gt = json.loads((ROOT / "data" / "ground_truth.json").read_text(encoding="utf-8"))
    rfx = RFx.model_validate(load_doc(ROOT / "data" / "rfx" / "rfx.json"))
    for vendor in gt["vendors"]:
        s = ev.score_vendor(vendor, gt, rfx, _oracle(vendor, gt, rfx), gt["usd_inr_rate_used"])
        bad = {k: v for k, v in s["counts"].items() if not all(v)}
        assert not bad, f"{vendor}: scorer disagrees with ground truth on {sorted(bad)}: {s['misses'][:3]}"
