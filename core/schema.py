"""Canonical data model for RFx quote comparison.

Rules used throughout this module:

- Anything read out of a source document is an ``ExtractedValue[T]``: the value plus
  its provenance (file, locator, snippet, confidence). A field that the source does not
  contain is ``None``, never an ExtractedValue wrapping a null.
- Identifiers, foreign keys and derived data (``NormalizedQuote``) are plain fields.
- Money and quantities are ``Decimal``, never ``float``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# --------------------------------------------------------------------------- enums


class Confidence(str, Enum):
    HIGH = "high"
    MED = "med"
    LOW = "low"


class Channel(str, Enum):
    """How a vendor response reached us."""

    EMAIL = "email"
    PORTAL = "portal"
    WHATSAPP = "whatsapp"
    PHONE = "phone"
    IN_PERSON = "in_person"
    OTHER = "other"


class Uom(str, Enum):
    """Canonical units of measure. Vendor wording (e.g. "Nos", "Reams") is mapped onto
    these; the raw text is kept in ``extraction_notes``."""

    EA = "EA"  # each / piece
    PACK = "PACK"
    SHEET = "SHEET"
    REAM = "REAM"
    KG = "KG"
    M = "M"  # metre
    SQM = "SQM"
    LOT = "LOT"


class Incoterm(str, Enum):
    EXW = "EXW"
    FCA = "FCA"
    FOB = "FOB"
    CFR = "CFR"
    CIF = "CIF"
    CPT = "CPT"
    CIP = "CIP"
    DAP = "DAP"
    DPU = "DPU"
    DDP = "DDP"


class ReviewStatus(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    OVERRIDDEN = "overridden"


class ReviewEntity(str, Enum):
    """Which kind of record a ReviewItem points at."""

    RFX = "rfx"
    LINE_ITEM = "line_item"
    RESPONSE = "response"
    QUOTE = "quote"
    QUESTIONNAIRE_ANSWER = "questionnaire_answer"
    NORMALIZED_QUOTE = "normalized_quote"


class NormalizationKind(str, Enum):
    FX_CONVERSION = "fx_conversion"
    UNIT_CONVERSION = "unit_conversion"
    TAX_REMOVAL = "tax_removal"
    DISCOUNT = "discount"
    OTHER = "other"


# ------------------------------------------------------------------- provenance


class ExtractedValue(BaseModel, Generic[T]):
    """A value taken from a source file, with enough provenance to audit it."""

    value: T
    source_file: str
    # Where in the file: "Sheet1!C7" (xlsx), "p.3" (pdf), "line 12" (email/text).
    locator: Optional[str] = None
    source_snippet: Optional[str] = None
    confidence: Confidence
    extraction_notes: Optional[str] = None


class RawFile(BaseModel):
    """A file exactly as received from a vendor."""

    path: str
    sha256: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None


# ------------------------------------------------------------------ RFx side


class Dimensions(BaseModel):
    length_mm: Optional[Decimal] = None
    width_mm: Optional[Decimal] = None
    height_mm: Optional[Decimal] = None


class LineItemSpec(BaseModel):
    """Physical specification of a line item. Every attribute is optional because
    the source may not state it."""

    ply: Optional[ExtractedValue[int]] = None
    gsm: Optional[ExtractedValue[int]] = None
    dimensions: Optional[ExtractedValue[Dimensions]] = None
    printing: Optional[ExtractedValue[str]] = None


class LineItem(BaseModel):
    id: str
    description: ExtractedValue[str]
    spec: LineItemSpec = Field(default_factory=LineItemSpec)
    qty: ExtractedValue[Decimal]
    uom: ExtractedValue[Uom]  # value is the canonical Uom


class Question(BaseModel):
    id: str
    text: str
    required: bool = False


class Terms(BaseModel):
    payment_terms: Optional[ExtractedValue[str]] = None
    delivery_terms: Optional[ExtractedValue[str]] = None
    validity: Optional[ExtractedValue[str]] = None
    incoterms: Optional[ExtractedValue[Incoterm]] = None


class RFx(BaseModel):
    id: str
    title: str
    category: str
    scope: str = ""  # free-text scope statement, e.g. sites and volumes
    terms: Terms = Field(default_factory=Terms)
    line_items: List[LineItem] = Field(default_factory=list)
    questionnaire: List[Question] = Field(default_factory=list)


# ------------------------------------------------------------- vendor side


class Vendor(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    gstin: Optional[str] = None


class Response(BaseModel):
    """One vendor's reply to one RFx."""

    id: str
    rfx_id: str
    vendor_id: str
    received_at: datetime
    channel: Channel
    raw_files: List[RawFile] = Field(default_factory=list)


class Quote(BaseModel):
    """A vendor's price for one line item, exactly as stated in the response."""

    id: str
    response_id: str
    line_item_id: str
    unit_price: ExtractedValue[Decimal]
    price_uom: ExtractedValue[Uom]  # the unit the price applies to
    currency: ExtractedValue[str]  # ISO 4217, e.g. "INR", "USD"
    # Units per pack when the vendor quotes per pack (price_uom=PACK).
    pack_size: Optional[ExtractedValue[Decimal]] = None
    moq: Optional[ExtractedValue[Decimal]] = None
    lead_time_days: Optional[ExtractedValue[int]] = None
    freight_terms: Optional[ExtractedValue[str]] = None
    tax_terms: Optional[ExtractedValue[str]] = None
    discount_terms: Optional[ExtractedValue[str]] = None
    # Response-level terms, copied onto each quote from that response.
    validity: Optional[ExtractedValue[str]] = None
    payment_terms: Optional[ExtractedValue[str]] = None
    notes: Optional[ExtractedValue[str]] = None


class NormalizationStep(BaseModel):
    kind: NormalizationKind
    description: str
    factor: Optional[Decimal] = None


class NormalizedQuote(BaseModel):
    """Derived, comparable price. Plain fields: this is computed, not extracted."""

    id: str
    quote_id: str
    line_item_id: str
    unit_price_inr_ex_tax: Decimal  # per canonical_uom
    canonical_uom: Uom
    fx_rate: Optional[Decimal] = None  # INR per 1 unit of source currency
    fx_rate_source: Optional[str] = None
    unit_conversion_factor: Optional[Decimal] = None
    steps: List[NormalizationStep] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class QuestionnaireAnswer(BaseModel):
    """Vendor's answer to an RFx questionnaire question. Source and confidence live
    on the wrapped ExtractedValue."""

    id: str
    response_id: str
    question_id: str
    answer: ExtractedValue[str]


# --------------------------------------------------------------- review


class ReviewItem(BaseModel):
    """Something the pipeline is not sure about and a human must decide."""

    id: str
    response_id: str
    entity_type: ReviewEntity
    entity_id: str
    field_name: str
    issue: str  # what is uncertain
    reason: str  # why it is uncertain
    proposed_value: Optional[Any] = None
    final_value: Optional[Any] = None  # set when accepted or overridden
    status: ReviewStatus = ReviewStatus.OPEN
