"""Persist extraction output as canonical rows: quotes, normalized quotes, questionnaire answers
and review items. Sets the response status to 'extracted', or 'needs_review' when anything is
uncertain. Nothing is written for a value the model did not provide.
"""

from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from core.extract.agent import ExtractionResult
from core.extract.models import FieldOut
from core.extract.normalize import USD_INR, canonical_currency, normalize_price
from core.schema import (NormalizationKind, NormalizationStep, NormalizedQuote, Quote, RFx, Uom)

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
REVIEW_REASONS = {
    "not_found": "snippet not found in the source text (possible fabrication)",
    "no_source": "named source file does not exist",
    "visual_only": "read from an image; no text to verify against",
    "low": "model reported low confidence",
}


def parse_number(v: Any) -> Optional[float]:
    """Numeric value from a model value. '680.79/-' -> 680.79, '1,450' -> 1450.0."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _NUM.search(v)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _ev(f: FieldOut, value: Any, extra_note: str = "") -> Dict[str, Any]:
    """ExtractedValue as a plain dict; the typed models validate it."""
    notes = "; ".join(x for x in [f.notes, extra_note] if x) or None
    return {"value": value, "source_file": f.source_file, "locator": f.locator,
            "source_snippet": f.snippet, "confidence": f.confidence, "extraction_notes": notes}


def _json(obj: Any) -> Optional[str]:
    return obj.model_dump_json() if obj is not None else None


def upsert_rfx(conn: sqlite3.Connection, rfx: RFx) -> None:
    t = rfx.terms
    conn.execute(
        "INSERT INTO rfx (id, title, category, payment_terms_json, delivery_terms_json, validity_json, "
        "incoterms_json) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title = excluded.title, "
        "category = excluded.category, payment_terms_json = excluded.payment_terms_json, "
        "delivery_terms_json = excluded.delivery_terms_json, validity_json = excluded.validity_json, "
        "incoterms_json = excluded.incoterms_json",
        (rfx.id, rfx.title, rfx.category, _json(t.payment_terms), _json(t.delivery_terms),
         _json(t.validity), _json(t.incoterms)))
    for pos, li in enumerate(rfx.line_items, start=1):
        s = li.spec
        conn.execute(
            "INSERT INTO line_item (id, rfx_id, position, description_json, ply_json, gsm_json, "
            "dimensions_json, printing_json, qty_json, uom_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET description_json=excluded.description_json, "
            "ply_json=excluded.ply_json, gsm_json=excluded.gsm_json, dimensions_json=excluded.dimensions_json, "
            "printing_json=excluded.printing_json, qty_json=excluded.qty_json, uom_json=excluded.uom_json",
            (li.id, rfx.id, pos, _json(li.description), _json(s.ply), _json(s.gsm),
             _json(s.dimensions), _json(s.printing), _json(li.qty), _json(li.uom)))
    for pos, q in enumerate(rfx.questionnaire, start=1):
        conn.execute(
            "INSERT INTO questionnaire_question (id, rfx_id, position, text, required) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, required=excluded.required",
            (q.id, rfx.id, pos, q.text, int(q.required)))
    conn.commit()


def _build_quote(qid: str, response_id: str, line_id: str, out, price: float, uom: str,
                 pack: Optional[float], currency: str, comm) -> Quote:
    cur_field = (_ev(out.currency, currency) if out.currency else
                 {"value": "INR", "source_file": "(not stated)", "locator": "-", "source_snippet": None,
                  "confidence": "low", "extraction_notes": "currency not stated; INR assumed"})
    data: Dict[str, Any] = {
        "id": qid, "response_id": response_id, "line_item_id": line_id,
        "unit_price": _ev(out.unit_price, Decimal(str(price))),
        "price_uom": _ev(out.price_uom, Uom(uom)),
        "currency": cur_field,
        "pack_size": _ev(out.pack_size, Decimal(str(pack))) if pack else None,
    }
    if comm is not None:
        if comm.freight:
            data["freight_terms"] = _ev(comm.freight, str(comm.freight.value))
        if comm.gst:
            data["tax_terms"] = _ev(comm.gst, str(comm.gst.value))
        if comm.discount:
            data["discount_terms"] = _ev(comm.discount, str(comm.discount.value))
        if comm.validity:
            data["validity"] = _ev(comm.validity, str(comm.validity.value))
        if comm.payment_terms:
            data["payment_terms"] = _ev(comm.payment_terms, str(comm.payment_terms.value))
    return Quote.model_validate(data)


def persist_extraction(conn: sqlite3.Connection, response_id: str, rfx: RFx,
                       result: ExtractionResult) -> dict:
    """Write canonical rows for one response and set its status. Returns a summary."""
    upsert_rfx(conn, rfx)
    # Re-extraction replaces this response's earlier rows.
    conn.execute("DELETE FROM review_item WHERE response_id = ?", (response_id,))
    conn.execute("DELETE FROM quote WHERE response_id = ?", (response_id,))
    conn.execute("DELETE FROM questionnaire_answer WHERE response_id = ?", (response_id,))

    reviews: List[Tuple[str, str, str, str, str, Any]] = []  # entity_type, entity_id, field, issue, reason, value
    for bad in result.unreadable:
        reviews.append(("response", response_id, "file", f"could not read {bad['file']}",
                        bad.get("error") or "unknown error", "resend as a PDF with selectable text or as Excel"))
    if result.empty:
        reviews.append(("response", response_id, "file", "response is empty or unreadable",
                        "no file in this response yielded text or an image", "ask the vendor to resend"))
        conn.execute("DELETE FROM review_item WHERE response_id = ?", (response_id,))
        for i, (etype, eid, fname, issue, reason, proposed) in enumerate(reviews, start=1):
            conn.execute(
                "INSERT INTO review_item (id, response_id, entity_type, entity_id, field_name, issue, reason, "
                "proposed_value_json, status) VALUES (?,?,?,?,?,?,?,?,'open')",
                (f"{response_id}:rv{i:03d}", response_id, etype, eid, fname, issue, reason,
                 json.dumps(proposed, default=str)))
        conn.execute("UPDATE response SET status = 'needs_review' WHERE id = ?", (response_id,))
        conn.commit()
        return {"quoted": 0, "not_quoted": 0, "normalized": 0, "answers": 0,
                "review_items": len(reviews), "status": "needs_review"}
    quoted = not_quoted = normalized = 0
    comm = result.commercial
    rfx_uom = {li.id: li.uom.value.value for li in rfx.line_items}
    uom_values = {u.value for u in Uom}

    for li in rfx.line_items:
        out = result.lines.get(li.id)
        qid = f"{response_id}:{li.id}"
        if out is None:
            reviews.append(("line_item", li.id, "status", "no output for this line",
                            "the model returned nothing for it", None))
            continue
        if out.status == "not_quoted":
            not_quoted += 1
            continue
        price = parse_number(out.unit_price.value)
        uom = out.price_uom.value if out.price_uom else None
        if price is None:
            reviews.append(("quote", qid, "unit_price", "price is not a number",
                            "could not parse the stated price", out.unit_price.value))
            continue
        if uom not in uom_values:
            reviews.append(("quote", qid, "price_uom", "unit not in canonical set",
                            f"stated unit {uom!r} cannot be mapped", uom))
            continue
        pack = parse_number(out.pack_size.value) if out.pack_size else None
        currency = str(out.currency.value) if out.currency and out.currency.value else "INR"

        quote = _build_quote(qid, response_id, li.id, out, price, uom, pack, currency, comm)
        conn.execute(
            "INSERT INTO quote (id, response_id, line_item_id, unit_price_json, price_uom_json, currency_json, "
            "pack_size_json, moq_json, lead_time_days_json, freight_terms_json, tax_terms_json, "
            "discount_terms_json, validity_json, payment_terms_json, notes_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (qid, response_id, li.id, _json(quote.unit_price), _json(quote.price_uom), _json(quote.currency),
             _json(quote.pack_size), None, None, _json(quote.freight_terms), _json(quote.tax_terms),
             _json(quote.discount_terms), _json(quote.validity), _json(quote.payment_terms), None))
        quoted += 1

        norm = normalize_price(price, uom, pack, currency, rfx_uom[li.id])
        if norm.value is None:
            reviews.append(("quote", qid, "unit_price", "cannot convert to RFx unit", norm.reason, price))
            continue
        usd = canonical_currency(currency) == "USD"
        nq = NormalizedQuote(
            id=f"{qid}:norm", quote_id=qid, line_item_id=li.id,
            unit_price_inr_ex_tax=Decimal(str(norm.value)), canonical_uom=Uom(rfx_uom[li.id]),
            fx_rate=Decimal(str(USD_INR)) if usd else None,
            fx_rate_source="fixed assumption (dataset rate)" if usd else None,
            steps=[NormalizationStep(kind=NormalizationKind.OTHER, description=s) for s in norm.steps])
        conn.execute(
            "INSERT INTO normalized_quote (id, quote_id, line_item_id, unit_price_inr_ex_tax, canonical_uom, "
            "fx_rate, fx_rate_source, unit_conversion_factor, steps_json, assumptions_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (nq.id, qid, li.id, str(nq.unit_price_inr_ex_tax), nq.canonical_uom.value,
             str(nq.fx_rate) if nq.fx_rate else None, nq.fx_rate_source, None,
             json.dumps([s.model_dump(mode="json") for s in nq.steps]), json.dumps(nq.assumptions)))
        normalized += 1

    answered = 0
    for q in rfx.questionnaire:
        qa = result.questionnaire.get(q.id)
        if qa is None or qa.status != "answered" or qa.answer is None:
            continue
        conn.execute("INSERT INTO questionnaire_answer (id, response_id, question_id, answer_json) VALUES (?,?,?,?)",
                     (f"{response_id}:{q.id}", response_id, q.id,
                      json.dumps(_ev(qa.answer, str(qa.answer.value)), default=str)))
        answered += 1

    # Anything not verified against the text, read only visually, or low confidence goes to review.
    seen = set()
    for check in result.checks:
        path, status = check["path"], check["status"]
        conf = _confidence_for(result, path)
        if status == "verified" and conf != "low":
            continue
        reason = REVIEW_REASONS.get(status if status != "verified" else "low")
        etype, eid, fname = _map_path(path, response_id)
        if (etype, eid, fname) in seen:
            continue
        seen.add((etype, eid, fname))
        reviews.append((etype, eid, fname, f"{path} needs a human check", reason, check["value"]))

    for i, (etype, eid, fname, issue, reason, proposed) in enumerate(reviews, start=1):
        conn.execute(
            "INSERT INTO review_item (id, response_id, entity_type, entity_id, field_name, issue, reason, "
            "proposed_value_json, status) VALUES (?,?,?,?,?,?,?,?,'open')",
            (f"{response_id}:rv{i:03d}", response_id, etype, eid, fname, issue, reason,
             json.dumps(proposed, default=str)))

    status = "needs_review" if reviews else "extracted"
    conn.execute("UPDATE response SET status = ? WHERE id = ?", (status, response_id))
    conn.commit()
    return {"quoted": quoted, "not_quoted": not_quoted, "normalized": normalized,
            "answers": answered, "review_items": len(reviews), "status": status}


def _confidence_for(result: ExtractionResult, path: str) -> str:
    head, _, leaf = path.partition(".")
    if path.startswith("commercial.") and result.commercial is not None:
        f = getattr(result.commercial, leaf, None)
        return f.confidence if f else "low"
    if path.endswith(".answer") and head in result.questionnaire:
        qa = result.questionnaire[head]
        return qa.answer.confidence if qa.answer else "low"
    if head in result.lines:
        f = getattr(result.lines[head], leaf, None)
        return f.confidence if f else "low"
    return "low"


def _map_path(path: str, response_id: str) -> Tuple[str, str, str]:
    head, _, leaf = path.partition(".")
    if path.startswith("commercial."):
        return "response", response_id, leaf
    if path.endswith(".answer"):
        return "questionnaire_answer", f"{response_id}:{head}", "answer"
    return "quote", f"{response_id}:{head}", leaf
