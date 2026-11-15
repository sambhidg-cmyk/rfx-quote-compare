"""Clarification emails to vendors, drafted from a review item. Drafts only: nothing is sent.

The text is built from the review record (what we read, why it is uncertain, what we need), not written
freehand by a model, so every figure in the email is the figure the system holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from email.message import EmailMessage

from core.schema import RFx

DRAFTS_DIR = Path(__file__).resolve().parents[1] / "data" / "outbox" / "drafts"


@dataclass
class Draft:
    to: str
    subject: str
    body: str

    def to_eml(self, sender: str = "RFx Desk (draft) <rfx-desk@kiranaplus.example>") -> bytes:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = self.to
        msg["Subject"] = self.subject
        msg["X-Status"] = "DRAFT - not sent"
        msg.set_content(self.body)
        return bytes(msg)


def _what_we_need(item: Dict[str, Any]) -> str:
    field_name = item.get("field_name") or ""
    proposed = item.get("proposed")
    if field_name == "unit_price":
        if proposed is not None:
            return (f"Please confirm the unit price. Our reading is {proposed}. If that is correct, reply 'confirmed'. "
                    "If not, send the correct price and the unit it applies to (per piece, per 100 pieces, per kg, "
                    "or per square metre).")
        return "Please state the unit price for this line and the unit it applies to."
    if field_name == "price_uom":
        return ("Please state the unit this price applies to (per piece, per 100 pieces, per kg, per square metre, "
                "or per roll with the number of pieces on a roll).")
    if field_name == "file":
        return ("We could not read the file you sent. Please resend it as a PDF with selectable text, or as an "
                "Excel workbook, and confirm the price for each line.")
    return f"Please confirm the value for {field_name or 'this item'} as stated in your response."


def draft_clarification(item: Dict[str, Any], rfx: RFx, vendor_email: str, vendor_name: str) -> Draft:
    """item: a review row as returned by core.compare.open_reviews()."""
    line = (item.get("entity_id") or "").split(":")[-1]
    subject = f"{rfx.id}: please confirm {('line ' + line) if line.startswith('L') else 'one item'} in your response"
    body = "\n".join([
        f"Dear {vendor_name},",
        "",
        f"Thank you for your response to {rfx.id} ({rfx.title}).",
        "",
        f"We need one point clarified before we can finalise the comparison:",
        f"  Item: {item.get('entity_type')} {item.get('entity_id')}" + (f", line {line}" if line.startswith('L') else ""),
        f"  What we are unsure of: {item.get('issue')}.",
        f"  Why: {item.get('reason')}.",
        "",
        _what_we_need(item),
        "",
        "Please reply to this email with the clarification. If you attach a revised file, keep the same line numbers.",
        "",
        "Kind regards,",
        "RFx Desk",
        "",
        "DRAFT - not sent. Review before sending.",
    ])
    return Draft(to=vendor_email, subject=subject, body=body)


def save_draft(draft: Draft, name: str, drafts_dir: Optional[Path] = None) -> Path:
    d = Path(drafts_dir or DRAFTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]
    path = d / f"{safe}.eml"
    path.write_bytes(draft.to_eml())
    return path
