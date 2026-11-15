"""Compare-tab data: matrix, totals, reviews and audit, hard requirements, certificate expiry."""

import json
from datetime import date
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from core import compare
from core.db import init_db
from core.extract.store import upsert_rfx
from core.rfx_copilot import load_doc
from core.schema import RFx

DEMO = Path(__file__).resolve().parents[1] / "data" / "rfx" / "rfx.json"
TODAY = date(2026, 11, 14)


def _ev(value, conf="high", source="quote.xlsx", locator="Rates!D6", snippet="s"):
    return json.dumps({"value": value, "source_file": source, "locator": locator,
                       "source_snippet": snippet, "confidence": conf, "extraction_notes": None})


@pytest.fixture
def db():
    rfx = RFx.model_validate(load_doc(DEMO))
    conn = init_db(":memory:")
    upsert_rfx(conn, rfx)
    for vid, name in [("vA", "Alpha"), ("vB", "Beta"), ("vC", "Gamma")]:
        conn.execute("INSERT INTO vendor (id, name) VALUES (?, ?)", (vid, name))
        conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES (?,?,?,?,?)",
                     (f"R{vid}", rfx.id, vid, "2026-11-14T08:00:00+00:00", "email"))
    # L01 (EA, qty 180000): A 14.00, B 15.00, C not quoted
    # L02 (EA, qty 150000): A 20.00, B 19.00, C 21.00
    # L03 (EA, qty 96000): only A 25.00 (B and C not quoted)
    prices = {("RvA", "L01"): (14.0, "high"), ("RvB", "L01"): (15.0, "high"),
              ("RvA", "L02"): (20.0, "med"), ("RvB", "L02"): (19.0, "high"), ("RvC", "L02"): (21.0, "high"),
              ("RvA", "L03"): (25.0, "high")}
    for (rid, lid), (price, conf) in prices.items():
        qid = f"{rid}:{lid}"
        conn.execute("INSERT INTO quote (id, response_id, line_item_id, unit_price_json, price_uom_json, "
                     "currency_json) VALUES (?,?,?,?,?,?)",
                     (qid, rid, lid, _ev(price, conf), _ev("EA", "high"), _ev("INR", "high")))
        conn.execute("INSERT INTO normalized_quote (id, quote_id, line_item_id, unit_price_inr_ex_tax, "
                     "canonical_uom, steps_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
                     (f"{qid}:n", qid, lid, str(price), "EA", "[]", "[]"))
    # A review item on Alpha's L02 price (visually read)
    conn.execute("INSERT INTO review_item (id, response_id, entity_type, entity_id, field_name, issue, reason, "
                 "proposed_value_json, status) VALUES ('rv1','RvA','quote','RvA:L02','unit_price',"
                 "'needs a human check','read from an image','20.0','open')")
    conn.commit()
    responses = {"vA": "RvA", "vB": "RvB", "vC": "RvC"}
    names = {"vA": "Alpha", "vB": "Beta", "vC": "Gamma"}
    return conn, rfx, responses, names


def test_lowest_price_flags_and_coverage(db):
    conn, rfx, responses, names = db
    m = compare.build_matrix(conn, rfx, responses, names)
    assert m.cell("vA", "L01").lowest is True and m.cell("vB", "L01").lowest is False
    assert m.cell("vB", "L02").lowest is True and m.cell("vC", "L02").lowest is False
    assert m.cell("vA", "L03").lowest is False  # only one priced offer: nothing to compare
    assert m.coverage("vA") == (3, 30)
    assert m.coverage("vC") == (1, 30)


def test_partial_and_like_for_like_totals(db):
    conn, rfx, responses, names = db
    m = compare.build_matrix(conn, rfx, responses, names)
    partial = m.partial_basket()
    assert partial["vA"] == (14 * 180000 + 20 * 150000 + 25 * 96000, 3)
    assert partial["vC"] == (21 * 150000, 1)
    # L02 is the only line all three vendors priced
    assert m.like_for_like_lines() == ["L02"]


def test_like_for_like_when_everyone_priced_a_line(db):
    conn, rfx, responses, names = db
    conn.execute("INSERT INTO quote (id, response_id, line_item_id, unit_price_json, price_uom_json, currency_json) "
                 "VALUES ('RvC:L01','RvC','L01',?,?,?)", (_ev(16.0), _ev("EA"), _ev("INR")))
    conn.execute("INSERT INTO normalized_quote (id, quote_id, line_item_id, unit_price_inr_ex_tax, canonical_uom, "
                 "steps_json, assumptions_json) VALUES ('RvC:L01:n','RvC:L01','L01','16.0','EA','[]','[]')")
    conn.commit()
    m = compare.build_matrix(conn, rfx, responses, names)
    assert m.like_for_like_lines() == ["L01", "L02"]
    lfl = m.like_for_like_basket()
    assert lfl["vA"] == 14 * 180000 + 20 * 150000
    assert lfl["vB"] == 15 * 180000 + 19 * 150000
    assert lfl["vC"] == 16 * 180000 + 21 * 150000


def test_trust_counts(db):
    conn, rfx, responses, names = db
    m = compare.build_matrix(conn, rfx, responses, names)
    counts = m.trust_counts()
    assert counts["review"] == 1  # the open review item on A-L02
    # 90 cells, 6 quoted. High and unflagged: A-L01, A-L03, B-L01, B-L02, C-L02 (A-L02 is medium and flagged)
    assert counts["high"] == 5
    assert counts["not_quoted"] == 84


def test_override_recomputes_totals_and_is_audited(db):
    conn, rfx, responses, names = db
    before = compare.build_matrix(conn, rfx, responses, names).partial_basket()["vA"][0]
    msg = compare.apply_review(conn, "rv1", "override", new_value=18.0, note="checked the photo myself")
    assert "Overrode" in msg
    after_m = compare.build_matrix(conn, rfx, responses, names)
    assert after_m.cell("vA", "L02").price == 18.0
    assert after_m.partial_basket()["vA"][0] == before - 2 * 150000
    trail = compare.audit_trail(conn, rfx.id)
    assert trail[0]["action"] == "override" and trail[0]["before_json"] == "20.0"
    assert json.loads(trail[0]["after_json"]) == 18.0
    assert after_m.cell("vA", "L02").review_ids == []  # closed review no longer flags the cell


def test_accept_closes_item_without_changing_price(db):
    conn, rfx, responses, names = db
    compare.apply_review(conn, "rv1", "accept")
    m = compare.build_matrix(conn, rfx, responses, names)
    assert m.cell("vA", "L02").price == 20.0
    assert compare.audit_trail(conn, rfx.id)[0]["action"] == "accept"
    with pytest.raises(ValueError, match="already accepted"):
        compare.apply_review(conn, "rv1", "accept")


def test_override_refused_for_non_price_items(db):
    conn, rfx, responses, names = db
    conn.execute("INSERT INTO review_item (id, response_id, entity_type, entity_id, field_name, issue, reason, "
                 "proposed_value_json, status) VALUES ('rv2','RvA','response','RvA','freight','x','y','null','open')")
    conn.commit()
    with pytest.raises(ValueError, match="only price overrides"):
        compare.apply_review(conn, "rv2", "override", new_value=1.0)


def test_override_needs_positive_price(db):
    conn, _, _, _ = db
    with pytest.raises(ValueError, match="greater than zero"):
        compare.apply_review(conn, "rv1", "override", new_value=0)


@pytest.mark.parametrize("rule,answer,expected", [
    ("in_house", "Yes. In-house ECT, BCT and Cobb test lab at Plant 1.", "pass"),
    ("in_house", "ECT and BCT testing is outsourced to an external lab.", "fail"),
    ("in_house", "We have test equipment.", "unclear"),
    ("iso_valid", "ISO 9001:2015 cert BCI-QMS-0093, valid to 31-Aug-2026.", "fail"),
    ("iso_valid", "ISO 9001:2015 cert VPW-QMS-4471, valid to 31-Mar-2027.", "pass"),
    ("iso_valid", "ISO 9001 yes, certificate to follow.", "unclear"),
    ("iso_valid", "ISO 9001 not held.", "fail"),
    ("iso_valid", None, "unclear"),
])
def test_hard_requirement_rules(rule, answer, expected):
    verdict, _ = compare.check_hard_requirement(rule, answer, TODAY)
    assert verdict == expected


def test_certificate_expiry_flag(tmp_path):
    expired = tmp_path / "old.pdf"
    c = canvas.Canvas(str(expired), pagesize=A4)
    c.drawString(72, 700, "ISO 9001 certificate. Valid until: 31-Aug-2026")
    c.save()
    d, status = compare.cert_expiry(expired, TODAY)
    assert d == date(2026, 8, 31) and status == "expired"
    unknown = tmp_path / "none.pdf"
    c = canvas.Canvas(str(unknown), pagesize=A4)
    c.drawString(72, 700, "no date here")
    c.save()
    assert compare.cert_expiry(unknown, TODAY)[1] == "unknown"
