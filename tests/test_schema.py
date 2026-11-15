"""Validation and storage tests for the canonical model. No business logic here."""

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.db import init_db
from core.schema import (
    Channel,
    Confidence,
    ExtractedValue,
    Quote,
    Response,
    Uom,
)


def _ev(value, **kw):
    return ExtractedValue(value=value, source_file="quote.xlsx", locator="Sheet1!C7",
                          confidence=Confidence.HIGH, **kw)


def test_extracted_value_requires_provenance():
    with pytest.raises(ValidationError):
        ExtractedValue(value=Decimal("1"), confidence="high")  # no source_file


def test_confidence_is_restricted_to_high_med_low():
    with pytest.raises(ValidationError):
        ExtractedValue(value=1, source_file="a.pdf", confidence="medium")


def test_quote_round_trip_preserves_decimal_and_provenance():
    q = Quote(
        id="q1", response_id="r1", line_item_id="li1",
        unit_price=_ev(Decimal("12.50")),
        price_uom=_ev(Uom.PACK),
        currency=_ev("INR"),
        pack_size=_ev(Decimal("100")),
    )
    restored = Quote.model_validate_json(q.model_dump_json())
    assert restored.unit_price.value == Decimal("12.50")
    assert restored.price_uom.value is Uom.PACK
    assert restored.unit_price.locator == "Sheet1!C7"
    assert restored.moq is None  # absent in source stays absent


def test_response_channel_enum():
    with pytest.raises(ValidationError):
        Response(id="r", rfx_id="x", vendor_id="v", channel="fax",
                 received_at=datetime.now(timezone.utc))
    ok = Response(id="r", rfx_id="x", vendor_id="v", channel=Channel.EMAIL,
                  received_at=datetime.now(timezone.utc))
    assert ok.raw_files == []


def test_db_creates_all_tables():
    conn = init_db(":memory:")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"vendor", "rfx", "line_item", "questionnaire_question", "response", "raw_file",
            "quote", "normalized_quote", "questionnaire_answer", "review_item"} <= names


def test_db_enforces_foreign_keys_and_enums():
    conn = init_db(":memory:")
    with pytest.raises(sqlite3.IntegrityError):  # quote points at a missing response
        conn.execute(
            "INSERT INTO quote (id, response_id, line_item_id, unit_price_json, price_uom_json, currency_json)"
            " VALUES ('q','nope','nope','{}','{}','{}')")
    conn.execute("INSERT INTO vendor (id, name) VALUES ('v','Acme')")
    conn.execute("INSERT INTO rfx (id, title, category) VALUES ('x','t','c')")
    with pytest.raises(sqlite3.IntegrityError):  # bad channel
        conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel)"
                     " VALUES ('r','x','v','2026-01-01T00:00:00Z','fax')")
