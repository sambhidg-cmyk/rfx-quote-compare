"""Award report, memo number check, workbook formulas, clarification drafts, and failure handling."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import openpyxl
import pytest

from core import award as award_mod
from core import award_report as ar
from core.clarify import draft_clarification
from core.compare import build_matrix
from core.db import init_db
from core.extract.agent import ExtractionResult, extract_response
from core.extract.readers import load_vendor_docs
from core.extract.store import persist_extraction, upsert_rfx
from core.rfx_copilot import load_doc
from core.schema import RFx


def _ev(value, conf="high"):
    return json.dumps({"value": value, "source_file": "q.xlsx", "locator": "Rates!D6",
                       "source_snippet": "s", "confidence": conf, "extraction_notes": None})


@pytest.fixture
def world():
    rfx = RFx.model_validate(load_doc(Path(__file__).resolve().parents[1] / "data" / "rfx" / "rfx.json"))
    rfx = rfx.model_copy(update={"line_items": rfx.line_items[:3], "questionnaire": rfx.questionnaire[:2]})
    for li, q in zip(rfx.line_items, [100, 50, 200]):
        li.qty.value = q
    conn = init_db(":memory:")
    upsert_rfx(conn, rfx)
    for vid, name in [("vA", "Alpha"), ("vB", "Beta"), ("vC", "Gamma")]:
        conn.execute("INSERT INTO vendor (id, name) VALUES (?, ?)", (vid, name))
        conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES (?,?,?,?,?)",
                     (f"R{vid}", rfx.id, vid, "2026-11-14T08:00:00+00:00", "email"))

    def quote(rid, lid, price, conf="high"):
        qid = f"{rid}:{lid}"
        conn.execute("INSERT INTO quote (id, response_id, line_item_id, unit_price_json, price_uom_json, "
                     "currency_json) VALUES (?,?,?,?,?,?)", (qid, rid, lid, _ev(price, conf), _ev("EA"), _ev("INR")))
        conn.execute("INSERT INTO normalized_quote (id, quote_id, line_item_id, unit_price_inr_ex_tax, canonical_uom, "
                     "steps_json, assumptions_json) VALUES (?,?,?,?,?,?,?)", (f"{qid}:n", qid, lid, str(price), "EA",
                                                                             "[]", "[]"))
    quote("RvA", "L01", 10.0)
    quote("RvA", "L02", 12.0)
    quote("RvA", "L03", 8.0)
    quote("RvB", "L01", 11.0)
    quote("RvB", "L02", 9.0)
    quote("RvC", "L03", 7.0, conf="low")
    conn.execute("INSERT INTO review_item (id, response_id, entity_type, entity_id, field_name, issue, reason, "
                 "proposed_value_json, status) VALUES ('rvC','RvC','quote','RvC:L03','unit_price',"
                 "'unit price read from a scanned page','no text layer','7','open')")
    conn.commit()
    responses = {"vA": "RvA", "vB": "RvB", "vC": "RvC"}
    names = {"vA": "Alpha", "vB": "Beta", "vC": "Gamma"}
    m = build_matrix(conn, rfx, responses, names)
    a = award_mod.run_award(m, award_mod.Scenario())
    return rfx, conn, m, a


def test_savings_are_against_the_highest_quote_on_awarded_lines(world):
    rfx, conn, m, a = world
    rep = ar.build_report(m, a)
    # awarded: L01 Alpha 10, L02 Beta 9, L03 Gamma 7; highest on each line: 11, 12, 8
    assert rep.highest_total == pytest.approx(11 * 100 + 12 * 50 + 8 * 200)
    assert rep.savings_vs_highest == pytest.approx(rep.highest_total - a.total)
    assert rep.last_year_total is None  # no last-year baseline in this data


def test_risks_flag_thin_coverage_and_unconfirmed_prices(world):
    rfx, conn, m, a = world
    rep = ar.build_report(m, a)
    kinds = {r["type"] for r in rep.risks}
    assert "thin_coverage" in kinds  # Gamma priced 1 of 3 lines
    assert "unconfirmed_price" in kinds  # Gamma's low-confidence L03 is in the award
    # L03 has two priced offers (Alpha 8, Gamma 7), so it is not single-source in this fixture
    assert not any(r["type"] == "single_source" for r in rep.risks)


def test_numbers_in_memo_must_come_from_facts(world):
    rfx, conn, m, a = world
    facts = ar.build_facts(rfx, m, ar.build_report(m, a), "no restrictions")
    total = f"{facts['total_award_inr_ex_tax']:,.2f}"
    assert ar.unknown_numbers(f"Total INR {total} ex-tax.", facts) == []
    assert ar.unknown_numbers("Total INR 987,654.32 ex-tax.", facts) != []


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self

    def create(self, **kw):
        return self.replies.pop(0)


def _memo_reply(text):
    return NS(content=[NS(type="tool_use", name="write_memo_sections", id="m1",
                          input={"sections": [{"heading": "Recommendation", "body": text}]})])


def test_memo_is_retried_then_accepted_when_numbers_match(world):
    rfx, conn, m, a = world
    facts = ar.build_facts(rfx, m, ar.build_report(m, a), "no restrictions")
    good = f"Total INR {facts['total_award_inr_ex_tax']:,.2f} ex-tax."
    client = Scripted([_memo_reply("Total INR 123,456.00 ex-tax."), _memo_reply(good)])
    memo = ar.draft_memo(client, facts, model="m")
    assert memo["source"] == "model" and memo["attempts"] == 2


def test_memo_falls_back_to_template_when_the_model_keeps_inventing(world):
    rfx, conn, m, a = world
    facts = ar.build_facts(rfx, m, ar.build_report(m, a), "no restrictions")
    client = Scripted([_memo_reply("Total INR 1.23."), _memo_reply("Total INR 2.34.")])
    memo = ar.draft_memo(client, facts, model="m")
    assert memo["source"] == "template"
    text = " ".join(s["body"] for s in memo["sections"])
    assert f"{facts['total_award_inr_ex_tax']:,.2f}" in text


def test_workbook_has_formulas_and_the_three_sheets(world, tmp_path):
    rfx, conn, m, a = world
    rep = ar.build_report(m, a)
    p = ar.write_workbook(tmp_path / "cmp.xlsx", rfx, m, rep, review_rows=[{"id": "rvC", "vendor": "Gamma"}],
                          source_rows=[{"quote_id": "RvA:L01", "vendor": "Alpha", "line": "L01"}])
    wb = openpyxl.load_workbook(p)
    assert wb.sheetnames == ["Comparison", "Award", "Sources", "Review items"]
    comp = wb["Comparison"]
    assert str(comp["H2"].value).startswith("=IF(COUNT(")  # lowest price formula (column after the 3 vendors)
    assert any(str(c.value).startswith("=SUMPRODUCT") for row in comp.iter_rows() for c in row)
    award_ws = wb["Award"]
    assert any(str(c.value).startswith("=SUM(") for row in award_ws.iter_rows() for c in row)
    assert any(str(c.value).startswith("=COUNTIF(") for row in award_ws.iter_rows() for c in row)


def test_clarification_draft_uses_the_record_and_is_not_sent(world):
    rfx, conn, m, a = world
    item = {"id": "rvC", "entity_type": "quote", "entity_id": "RvC:L03", "field_name": "unit_price",
            "issue": "unit price read from a scanned page", "reason": "no text layer", "proposed": 7.0}
    d = draft_clarification(item, rfx, "sales@gamma.example", "Gamma")
    assert "line L03" in d.body and "Our reading is 7.0" in d.body
    assert "DRAFT - not sent" in d.body
    assert b"X-Status: DRAFT - not sent" in d.to_eml()


# ----------------------------------------------------------- failure handling

def test_unreadable_file_is_recorded_not_dropped(tmp_path):
    folder = tmp_path / "vendor_x"
    folder.mkdir()
    (folder / "quote.xlsx").write_bytes(b"not really an excel file")
    docs = load_vendor_docs(folder)
    assert len(docs) == 1 and docs[0].error and not docs[0].usable


def test_empty_response_makes_no_model_call_and_asks_for_a_resend(world, tmp_path):
    rfx, conn, m, a = world
    folder = tmp_path / "empty_vendor"
    folder.mkdir()
    (folder / "readme.bin").write_bytes(b"\x00\x01")  # unsupported type: nothing usable

    class NoCalls:
        messages = None

        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **kw):
            self.calls += 1
            raise AssertionError("model must not be called for an empty response")

    client = NoCalls()
    res = extract_response(client, folder, rfx, cache_dir=tmp_path / "c")
    assert res.empty and client.calls == 0
    conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES "
                 "('Rempty', ?, 'vA', '2026-11-14T08:00:00+00:00', 'email')", (rfx.id,))
    summary = persist_extraction(conn, "Rempty", rfx, res)
    assert summary["status"] == "needs_review"
    issue = conn.execute("SELECT issue FROM review_item WHERE response_id='Rempty'").fetchone()[0]
    assert "empty or unreadable" in issue


def test_unreadable_file_becomes_a_review_item_with_a_resend_request(world, tmp_path):
    rfx, conn, m, a = world
    result = ExtractionResult(folder="x", model="m")
    result.unreadable = [{"file": "quote.xlsx", "error": "BadZipFile: not a zip file"}]
    conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES "
                 "('Rbad', ?, 'vB', '2026-11-14T08:00:00+00:00', 'email')", (rfx.id,))
    persist_extraction(conn, "Rbad", rfx, result)
    row = conn.execute("SELECT issue, reason, proposed_value_json FROM review_item WHERE response_id='Rbad'").fetchone()
    assert "could not read quote.xlsx" in row[0] and "BadZipFile" in row[1]
    assert "resend" in row[2]
