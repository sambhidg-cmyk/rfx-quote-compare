"""Output contract for one extraction call. The model fills the record_extraction tool; the
same pydantic models validate it. Every value carries its own provenance."""

from __future__ import annotations

import re
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

Confidence = Literal["high", "med", "low"]
PRICE_UOMS = ("EA", "KG", "PACK", "SQM", "LOT", "OTHER")


class FieldOut(BaseModel):
    value: Optional[Union[float, str]] = None
    source_file: Optional[str] = None
    locator: Optional[str] = None
    snippet: Optional[str] = Field(default=None, description="verbatim, single line, copied from the source")
    confidence: Confidence
    notes: str = ""

    @model_validator(mode="after")
    def _value_needs_provenance(self):
        if self.value is not None and not (self.source_file and self.locator and self.snippet):
            raise ValueError("a value needs source_file, locator and a verbatim snippet; "
                             "if the source does not state it, leave value null")
        return self


UOM_SYNONYMS = {
    "EA": "EA", "EACH": "EA", "PC": "EA", "PCS": "EA", "PIECE": "EA", "PIECES": "EA", "NOS": "EA", "NO": "EA",
    "UNIT": "EA", "UNITS": "EA", "ROLL": "EA", "ROLLS": "EA", "BOX": "EA", "SET": "EA",
    "KG": "KG", "KGS": "KG", "KILOGRAM": "KG", "KILOGRAMS": "KG",
    "PACK": "PACK", "100 PCS": "PACK", "100 NOS": "PACK", "HUNDRED": "PACK",
    "SQM": "SQM", "SQ M": "SQM", "SQ.M": "SQM", "SQUARE METRE": "SQM", "SQUARE METER": "SQM",
    "LOT": "LOT",
}


def canonical_uom(raw) -> object:
    """Map vendor wording ('per roll', 'pcs', 'sq m', 'per 100 pcs') onto the canonical units. Unmapped
    values are returned unchanged and fail validation, which the agent then reports."""
    if not isinstance(raw, str):
        return raw
    key = raw.strip().upper().replace("PER ", "").replace("/", " ").strip()
    key = " ".join(key.split())
    if key in UOM_SYNONYMS:
        return UOM_SYNONYMS[key]
    # wording with a number or extra words: 'per 100 pieces', 'per roll (1200 mm)', 'sq ft'
    if re.search(r"\b(100|HUNDRED)\b", key) and re.search(r"\b(PC|PCS|PIECES?|NOS|NUMBERS?)\b", key):
        return "PACK"
    if re.search(r"\bSQ\b|SQUARE|SQM|M2|\bM\^2\b", key):
        return "SQM" if "FT" not in key else raw  # sq ft is a different unit: left unmapped
    if re.search(r"\bKGS?\b|KILO", key):
        return "KG"
    if re.search(r"\b(PC|PCS|PIECES?|EACH|NOS|NO|ROLLS?|BOXES|BOX|UNITS?|SETS?)\b", key):
        return "EA"
    return raw


class LineOut(BaseModel):
    line_id: str
    status: Literal["quoted", "not_quoted"]
    unit_price: Optional[FieldOut] = None
    price_uom: Optional[FieldOut] = Field(default=None, description="one of EA, KG, PACK, SQM, LOT, OTHER")
    pack_size: Optional[FieldOut] = Field(default=None, description="units per pack when quoted per pack")
    currency: Optional[FieldOut] = None
    notes: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalise_uom(cls, data):
        if isinstance(data, dict):
            pu = data.get("price_uom")
            if isinstance(pu, dict) and "value" in pu:
                data = {**data, "price_uom": {**pu, "value": canonical_uom(pu["value"])}}
        return data

    @model_validator(mode="after")
    def _status_matches_values(self):
        if self.status == "quoted":
            if self.unit_price is None or self.unit_price.value is None:
                raise ValueError(f"{self.line_id}: status quoted needs a unit_price value")
            if self.price_uom is None or self.price_uom.value not in PRICE_UOMS:
                raise ValueError(f"{self.line_id}: price_uom must be one of {PRICE_UOMS}")
        elif self.unit_price is not None and self.unit_price.value is not None:
            raise ValueError(f"{self.line_id}: status not_quoted must not carry a price")
        return self


class CommercialOut(BaseModel):
    freight: Optional[FieldOut] = Field(default=None, description="e.g. 'freight included to Mumbai' or 'freight extra'")
    gst: Optional[FieldOut] = Field(default=None, description="whether prices include GST")
    validity: Optional[FieldOut] = None
    discount: Optional[FieldOut] = Field(default=None, description="discount rules, including footnotes")
    moq: Optional[FieldOut] = None
    lead_time_days: Optional[FieldOut] = None
    payment_terms: Optional[FieldOut] = None


class QAOut(BaseModel):
    question_id: str
    status: Literal["answered", "not_answered"]
    answer: Optional[FieldOut] = None

    @model_validator(mode="after")
    def _answered_needs_answer(self):
        if self.status == "answered" and (self.answer is None or self.answer.value is None):
            raise ValueError(f"{self.question_id}: status answered needs an answer value")
        if self.status == "not_answered" and self.answer is not None and self.answer.value is not None:
            raise ValueError(f"{self.question_id}: status not_answered must not carry an answer")
        return self


class BatchOut(BaseModel):
    lines: List[LineOut] = Field(default_factory=list)
    commercial: Optional[CommercialOut] = None
    questionnaire: List[QAOut] = Field(default_factory=list)


EXTRACT_TOOL = {
    "name": "record_extraction",
    "description": ("Record what the vendor's files say for the requested lines, commercial terms and "
                    "questionnaire questions. Use not_quoted or null when the files do not say. Never guess."),
    "input_schema": BatchOut.model_json_schema(),
}
