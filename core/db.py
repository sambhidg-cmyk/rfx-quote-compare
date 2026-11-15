"""SQLite DDL for the canonical model in ``core.schema``.

Storage conventions:
- ``ExtractedValue`` fields are stored as JSON TEXT columns named ``<field>_json``
  holding the full wrapper (value + provenance). NULL means the source did not state it.
- Plain scalar fields get typed columns.
- ``Decimal`` values are stored as TEXT to avoid float rounding.
- Enums are enforced with CHECK constraints generated from the Python enums.
- Foreign keys are enforced (``PRAGMA foreign_keys = ON`` on every connection).
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path
from typing import Union

from core.schema import Channel, ReviewEntity, ReviewStatus, Uom

SCHEMA_VERSION = 4

# Lifecycle of a vendor response inside the system. 'received' is the only status set
# today; the rest are set by extraction and review steps once they exist.
RESPONSE_STATUSES = ("received", "processing", "extracted", "needs_review")


def _check(column: str, enum: type[Enum]) -> str:
    allowed = ", ".join(repr(m.value) for m in enum)
    return f"CHECK ({column} IN ({allowed}))"


DDL = f"""
CREATE TABLE IF NOT EXISTS vendor (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    email   TEXT,
    phone   TEXT,
    gstin   TEXT
);

CREATE TABLE IF NOT EXISTS rfx (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    category             TEXT NOT NULL,
    payment_terms_json   TEXT,   -- ExtractedValue[str]
    delivery_terms_json  TEXT,   -- ExtractedValue[str]
    validity_json        TEXT,   -- ExtractedValue[str]
    incoterms_json       TEXT    -- ExtractedValue[Incoterm]
);

CREATE TABLE IF NOT EXISTS line_item (
    id               TEXT PRIMARY KEY,
    rfx_id           TEXT NOT NULL REFERENCES rfx(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    description_json TEXT NOT NULL,  -- ExtractedValue[str]
    ply_json         TEXT,           -- ExtractedValue[int]
    gsm_json         TEXT,           -- ExtractedValue[int]
    dimensions_json  TEXT,           -- ExtractedValue[Dimensions]
    printing_json    TEXT,           -- ExtractedValue[str]
    qty_json         TEXT NOT NULL,  -- ExtractedValue[Decimal]
    uom_json         TEXT NOT NULL,  -- ExtractedValue[Uom] (value is canonical)
    UNIQUE (rfx_id, position)
);

CREATE TABLE IF NOT EXISTS questionnaire_question (
    id        TEXT PRIMARY KEY,
    rfx_id    TEXT NOT NULL REFERENCES rfx(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    text      TEXT NOT NULL,
    required  INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    UNIQUE (rfx_id, position)
);

CREATE TABLE IF NOT EXISTS response (
    id           TEXT PRIMARY KEY,
    rfx_id       TEXT NOT NULL REFERENCES rfx(id) ON DELETE CASCADE,
    vendor_id    TEXT NOT NULL REFERENCES vendor(id),
    received_at  TEXT NOT NULL,       -- ISO 8601
    channel      TEXT NOT NULL {_check("channel", Channel)},
    status       TEXT NOT NULL DEFAULT 'received' CHECK (status IN ({", ".join(repr(s) for s in RESPONSE_STATUSES)}))
);

CREATE TABLE IF NOT EXISTS outbound_message (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rfx_id       TEXT NOT NULL REFERENCES rfx(id) ON DELETE CASCADE,
    vendor_id    TEXT NOT NULL REFERENCES vendor(id),
    to_address   TEXT NOT NULL,
    sent_at      TEXT NOT NULL,       -- ISO 8601
    path         TEXT NOT NULL,       -- where the simulated message was written
    simulated    INTEGER NOT NULL DEFAULT 1 CHECK (simulated IN (0, 1))
);

CREATE TABLE IF NOT EXISTS raw_file (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id  TEXT NOT NULL REFERENCES response(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    sha256       TEXT,
    mime_type    TEXT,
    size_bytes   INTEGER
);

CREATE TABLE IF NOT EXISTS quote (
    id                  TEXT PRIMARY KEY,
    response_id         TEXT NOT NULL REFERENCES response(id) ON DELETE CASCADE,
    line_item_id        TEXT NOT NULL REFERENCES line_item(id) ON DELETE CASCADE,
    unit_price_json     TEXT NOT NULL,  -- ExtractedValue[Decimal]
    price_uom_json      TEXT NOT NULL,  -- ExtractedValue[Uom]
    currency_json       TEXT NOT NULL,  -- ExtractedValue[str]
    pack_size_json      TEXT,           -- ExtractedValue[Decimal]
    moq_json            TEXT,           -- ExtractedValue[Decimal]
    lead_time_days_json TEXT,           -- ExtractedValue[int]
    freight_terms_json  TEXT,           -- ExtractedValue[str]
    tax_terms_json      TEXT,           -- ExtractedValue[str]
    discount_terms_json TEXT,           -- ExtractedValue[str]
    validity_json       TEXT,           -- ExtractedValue[str] (response-level, copied)
    payment_terms_json  TEXT,           -- ExtractedValue[str] (response-level, copied)
    notes_json          TEXT,           -- ExtractedValue[str]
    UNIQUE (response_id, line_item_id)
);

CREATE TABLE IF NOT EXISTS normalized_quote (
    id                      TEXT PRIMARY KEY,
    quote_id                TEXT NOT NULL UNIQUE REFERENCES quote(id) ON DELETE CASCADE,
    line_item_id            TEXT NOT NULL REFERENCES line_item(id) ON DELETE CASCADE,
    unit_price_inr_ex_tax   TEXT NOT NULL,   -- Decimal
    canonical_uom           TEXT NOT NULL {_check("canonical_uom", Uom)},
    fx_rate                 TEXT,           -- Decimal, INR per 1 source-currency unit
    fx_rate_source          TEXT,
    unit_conversion_factor  TEXT,           -- Decimal
    steps_json              TEXT NOT NULL,  -- list[NormalizationStep]
    assumptions_json        TEXT NOT NULL   -- list[str]
);

CREATE TABLE IF NOT EXISTS questionnaire_answer (
    id           TEXT PRIMARY KEY,
    response_id  TEXT NOT NULL REFERENCES response(id) ON DELETE CASCADE,
    question_id  TEXT NOT NULL REFERENCES questionnaire_question(id) ON DELETE CASCADE,
    answer_json  TEXT NOT NULL,  -- ExtractedValue[str]
    UNIQUE (response_id, question_id)
);

CREATE TABLE IF NOT EXISTS review_item (
    id                  TEXT PRIMARY KEY,
    response_id         TEXT NOT NULL REFERENCES response(id) ON DELETE CASCADE,
    entity_type         TEXT NOT NULL {_check("entity_type", ReviewEntity)},
    entity_id           TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    issue               TEXT NOT NULL,
    reason              TEXT NOT NULL,
    proposed_value_json TEXT,
    final_value_json    TEXT,
    status              TEXT NOT NULL DEFAULT 'open' {_check("status", ReviewStatus)}
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,       -- ISO 8601, UTC
    actor        TEXT NOT NULL,
    rfx_id       TEXT,
    action       TEXT NOT NULL,       -- accept | override
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    field_name   TEXT NOT NULL,
    before_json  TEXT,
    after_json   TEXT,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS ix_response_rfx        ON response(rfx_id, vendor_id);
CREATE INDEX IF NOT EXISTS ix_outbound_rfx        ON outbound_message(rfx_id, vendor_id);
CREATE INDEX IF NOT EXISTS ix_quote_line_item     ON quote(line_item_id);
CREATE INDEX IF NOT EXISTS ix_review_response     ON review_item(response_id, status);
"""

def init_db(path: Union[str, Path], check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and apply the schema.

    Use ``":memory:"`` for an ephemeral database in tests. Streamlit reruns the script in
    other threads, so the app passes ``check_same_thread=False`` and shares one connection.
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn
