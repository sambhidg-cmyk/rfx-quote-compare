"""Extraction pipeline tests with a scripted model. No API calls are made."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from core.db import init_db
from core.extract import cache, run_extraction
from core.extract.agent import extract_response
from core.extract.models import BatchOut, LineOut, FieldOut
from core.extract.normalize import normalize_price
from core.extract.readers import load_vendor_docs, route
from core.extract.store import upsert_rfx
from core.extract.verify import check_batch, verify_snippet
from core.rfx_copilot import load_doc
from core.schema import RFx

DEMO = Path(__file__).resolve().parents[1] / "data" / "rfx" / "rfx.json"
DATA = Path(__file__).resolve().parents[1] / "data" / "responses"


def small_rfx() -> RFx:
    doc = load_doc(DEMO)
    doc["line_items"] = doc["line_items"][:2]
    doc["questionnaire"] = doc["questionnaire"][:1]
    return RFx.model_validate(doc)


QUOTE_TXT = ("L01: RSC shipper box 200x150x100 mm. Rate Rs 14.50 per piece.\n"
             "L02: no bid this cycle.\n"
             "Freight extra at actuals.\n"
             "Q01 ISO 9001 certificate valid till 2027.\n")


def fv(value, source, locator, snippet, conf="high"):
    return {"value": value, "source_file": source, "locator": locator, "snippet": snippet, "confidence": conf}


def lines_call(price_snippet="Rate Rs 14.50 per piece.", price=14.5):
    return {"lines": [
        {"line_id": "L01", "status": "quoted",
         "unit_price": fv(price, "quote.txt", "line 1", price_snippet),
         "price_uom": fv("EA", "quote.txt", "line 1", "per piece")},
        {"line_id": "L02", "status": "not_quoted", "notes": "vendor wrote no bid"}]}


def commercial_call():
    return {"commercial": {"freight": fv("freight extra at actuals", "quote.txt", "line 3",
                                         "Freight extra at actuals.")},
            "questionnaire": [{"question_id": "Q01", "status": "answered",
                               "answer": fv("ISO 9001 valid till 2027", "quote.txt", "line 4",
                                            "ISO 9001 certificate valid till 2027")}]}


def tool(inp, uid="tu"):
    return NS(type="tool_use", name="record_extraction", input=inp, id=uid)


def reply(inp):
    return NS(content=[tool(inp)], stop_reason="tool_use")


class ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if not self.replies:
            raise AssertionError("model called more times than scripted")
        return self.replies.pop(0)


@pytest.fixture
def vendor_folder(tmp_path):
    d = tmp_path / "vendor_x"
    d.mkdir()
    (d / "quote.txt").write_text(QUOTE_TXT, encoding="utf-8")
    return d


# ---------------------------------------------------------------- readers

def test_xlsx_dump_has_cell_coordinates_and_hidden_flags():
    docs = {d.name: d for d in load_vendor_docs(DATA / "vendor_a")}
    text = docs["quote.xlsx"].text
    assert "[Rates!D6]" in text or "[Rates!E6]" in text or "[Rates!F6]" in text or "[Rates!G6]" in text
    assert "(hidden column)" in text  # the internal notes column
    assert "sheet 'Rates'" in text and "sheet 'Compliance'" in text


def test_pdf_with_no_text_layer_is_sent_as_image(tmp_path):
    p = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(p), pagesize=A4)
    c.drawString(72, 700, "x")  # too little text to be trusted
    c.showPage()
    c.save()
    doc = route(p, "scan.pdf")
    assert len(doc.images) == 1 and doc.images[0].media_type == "image/png"
    assert doc.notes and "sent as an image" in doc.notes[0]


def test_text_pdf_has_page_locators():
    docs = {d.name: d for d in load_vendor_docs(DATA / "vendor_b")}
    assert "[p.1 line" in docs["quote_letterhead.pdf"].text
    assert not docs["quote_letterhead.pdf"].images


def test_docx_paragraph_numbers_match_generator_locators():
    docs = {d.name: d for d in load_vendor_docs(DATA / "vendor_c")}
    assert "[para 7] L01, RSC shipper box" in docs["quote.docx"].text


def test_photo_is_visual_only():
    docs = {d.name: d for d in load_vendor_docs(DATA / "vendor_d")}
    assert docs["rate_card_photo.jpg"].images and not docs["rate_card_photo.jpg"].text


# ------------------------------------------------------------ verification

def test_snippet_verification_statuses(vendor_folder):
    docs = {d.name: d for d in load_vendor_docs(vendor_folder)}
    assert verify_snippet("rate rs 14.50   per piece.", "quote.txt", docs) == "verified"  # case/space tolerant
    assert verify_snippet("Rate Rs 15.00 per piece.", "quote.txt", docs) == "not_found"
    assert verify_snippet("anything", "missing.txt", docs) == "no_source"


def test_visual_snippet_is_capped_at_medium(tmp_path):
    from PIL import Image
    Image.new("RGB", (20, 20), "white").save(tmp_path / "photo.jpg")
    docs = {d.name: d for d in load_vendor_docs(tmp_path)}
    out = BatchOut.model_validate({"lines": [
        {"line_id": "L01", "status": "quoted", "unit_price": fv(9.9, "photo.jpg", "row 1", "9.90", "high"),
         "price_uom": fv("EA", "photo.jpg", "row 1", "per pc", "high")}]})
    errors, checks = check_batch(out, docs)
    assert errors == []
    assert out.lines[0].unit_price.confidence == "med"
    assert checks[0]["status"] == "visual_only"


def test_value_without_snippet_is_rejected_by_schema():
    with pytest.raises(ValidationError, match="verbatim snippet"):
        FieldOut(value=14.5, source_file="quote.txt", locator="line 1", confidence="high")


def test_not_quoted_line_cannot_carry_a_price():
    with pytest.raises(ValidationError, match="must not carry a price"):
        LineOut(line_id="L02", status="not_quoted",
                unit_price=fv(3.0, "quote.txt", "line 2", "no bid", "high"))


# ------------------------------------------------------------ normalization

@pytest.mark.parametrize("value,stated,pack,cur,rfx,expected", [
    (14.5, "EA", None, "INR", "EA", 14.5),
    (1531.0, "PACK", 100, "INR", "EA", 15.31),  # per 100 pieces -> per piece
    (20.0, "EA", None, "INR", "PACK", 2000.0),  # per piece -> per 100 pieces
    (1.0, "EA", None, "USD", "EA", 83.5),  # USD -> INR at the fixed rate
])
def test_normalize_known_conversions(value, stated, pack, cur, rfx, expected):
    n = normalize_price(value, stated, pack, cur, rfx)
    assert n.value == pytest.approx(expected, abs=1e-3)


def test_normalize_refuses_unknown_conversion():
    n = normalize_price(10.0, "SQM", None, "INR", "KG")
    assert n.value is None and "cannot convert" in n.reason


# ------------------------------------------------------- agent and retries

def test_valid_extraction_passes_without_retry(vendor_folder):
    rfx = small_rfx()
    client = ScriptedClient([reply(lines_call()), reply(commercial_call())])
    res = extract_response(client, vendor_folder, rfx, cache_dir=vendor_folder.parent / "cache")
    assert client.calls == 2
    assert res.lines["L01"].unit_price.value == 14.5
    assert res.lines["L02"].status == "not_quoted"
    assert res.commercial.freight.value == "freight extra at actuals"
    assert res.questionnaire["Q01"].status == "answered"
    assert all(b.attempts == 1 for b in res.batches)


def test_fabricated_snippet_is_retried_with_the_error(vendor_folder):
    rfx = small_rfx()
    bad = lines_call(price_snippet="Rate Rs 15.00 per piece.", price=15.0)
    client = ScriptedClient([reply(bad), reply(lines_call()), reply(commercial_call())])
    res = extract_response(client, vendor_folder, rfx, cache_dir=vendor_folder.parent / "cache")
    assert client.calls == 3
    assert res.batches[0].attempts == 2
    assert res.lines["L01"].unit_price.value == 14.5  # the corrected output won


def test_retry_message_names_the_failed_snippet(vendor_folder):
    rfx = small_rfx()
    bad = lines_call(price_snippet="Rate Rs 15.00 per piece.", price=15.0)
    client = ScriptedClient([reply(bad), reply(lines_call()), reply(commercial_call())])
    seen = []
    orig = client.create

    def spy(**kw):
        seen.append(kw["messages"])
        return orig(**kw)

    client.create = spy
    extract_response(client, vendor_folder, rfx, cache_dir=vendor_folder.parent / "cache")
    retry_turn = seen[1][-1]["content"][0]
    assert retry_turn["is_error"] is True
    assert "was not found in 'quote.txt'" in retry_turn["content"]


def test_fabrication_after_retry_is_kept_as_low_and_reviewed(vendor_folder):
    rfx = small_rfx()
    bad = lines_call(price_snippet="Rate Rs 15.00 per piece.", price=15.0)
    client = ScriptedClient([reply(bad), reply(bad), reply(commercial_call())])
    res = extract_response(client, vendor_folder, rfx, cache_dir=vendor_folder.parent / "cache")
    assert res.lines["L01"].unit_price.confidence == "low"
    assert any(b.unresolved_snippets for b in res.batches)

    conn = init_db(":memory:")
    upsert_rfx(conn, rfx)
    conn.execute("INSERT INTO vendor (id, name) VALUES ('vx','X')")
    from datetime import datetime, timezone
    conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES "
                 "('R1', ?, 'vx', ?, 'email')", (rfx.id, datetime.now(timezone.utc).isoformat()))
    summary = __import__("core.extract.store", fromlist=["x"]).persist_extraction(conn, "R1", rfx, res)
    assert summary["status"] == "needs_review"
    reasons = [r[0] for r in conn.execute("SELECT reason FROM review_item WHERE response_id='R1'")]
    assert any("fabrication" in r for r in reasons)


def test_schema_failure_after_retry_raises(vendor_folder):
    from core.extract.agent import ExtractionError
    rfx = small_rfx()
    broken = {"lines": [{"line_id": "L01", "status": "quoted"}]}  # quoted without a price
    client = ScriptedClient([reply(broken), reply(broken)])
    with pytest.raises(ExtractionError, match="schema invalid after retry"):
        extract_response(client, vendor_folder, rfx, cache_dir=vendor_folder.parent / "cache")


# ------------------------------------------------------------------ cache

def test_cache_hit_avoids_model_calls_and_force_bypasses(vendor_folder):
    rfx = small_rfx()
    cdir = vendor_folder.parent / "cache"
    first = ScriptedClient([reply(lines_call()), reply(commercial_call())])
    extract_response(first, vendor_folder, rfx, cache_dir=cdir)
    assert first.calls == 2 and len(list(cdir.glob("*.json"))) == 2

    second = ScriptedClient([])  # any call would fail the test
    res = extract_response(second, vendor_folder, rfx, cache_dir=cdir)
    assert second.calls == 0
    assert all(b.cache_hit for b in res.batches)

    forced = ScriptedClient([reply(lines_call()), reply(commercial_call())])
    extract_response(forced, vendor_folder, rfx, cache_dir=cdir, force=True)
    assert forced.calls == 2


def test_cache_key_changes_when_a_file_changes(vendor_folder):
    rfx = small_rfx()
    cdir = vendor_folder.parent / "cache"
    extract_response(ScriptedClient([reply(lines_call()), reply(commercial_call())]), vendor_folder, rfx,
                     cache_dir=cdir)
    (vendor_folder / "quote.txt").write_text(QUOTE_TXT + "Extra line.\n", encoding="utf-8")
    client = ScriptedClient([reply(lines_call()), reply(commercial_call())])
    extract_response(client, vendor_folder, rfx, cache_dir=cdir)
    assert client.calls == 2  # new file bytes, so both batches missed


# ------------------------------------------------------------- persistence

def test_persist_writes_quote_normalized_answer_and_status(vendor_folder):
    rfx = small_rfx()
    conn = init_db(":memory:")
    upsert_rfx(conn, rfx)
    conn.execute("INSERT INTO vendor (id, name) VALUES ('vx','X')")
    from datetime import datetime, timezone
    conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES "
                 "('R1', ?, 'vx', ?, 'email')", (rfx.id, datetime.now(timezone.utc).isoformat()))
    client = ScriptedClient([reply(lines_call()), reply(commercial_call())])
    _, summary = run_extraction(conn, "R1", vendor_folder, rfx, client, cache_dir=vendor_folder.parent / "cache")
    assert summary == {"quoted": 1, "not_quoted": 1, "normalized": 1, "answers": 1,
                       "review_items": 0, "status": "extracted"}
    row = conn.execute("SELECT unit_price_inr_ex_tax, canonical_uom FROM normalized_quote").fetchone()
    assert row[0] == "14.5" and row[1] == "EA"
    freight = conn.execute("SELECT freight_terms_json FROM quote").fetchone()[0]
    assert "freight extra at actuals" in freight
    status = conn.execute("SELECT status FROM response WHERE id='R1'").fetchone()[0]
    assert status == "extracted"
