"""Convert a vendor's stated price into the RFx line's canonical unit, INR ex-tax.

Only conversions that are certain are made:
- USD -> INR at USD_INR (fixed assumption, recorded as a step)
- per pack of 100 pieces -> per piece, and per piece -> per 100 pieces (the RFx uses PACK = 100)
- same unit -> no change
Anything else returns None with a reason, and the line goes to review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

USD_INR = 83.50  # same fixed rate the dataset uses; a real deployment would take a dated rate
RFX_PACK_SIZE = 100


@dataclass
class Normalized:
    value: Optional[float]
    steps: List[str] = field(default_factory=list)
    reason: str = ""


_CURRENCY_ALIASES = {"₹": "INR", "RS": "INR", "INR": "INR", "RUPEE": "INR", "RUPEES": "INR",
                     "$": "USD", "US$": "USD", "USD": "USD"}


def canonical_currency(raw: Optional[str]) -> str:
    """ISO code for what a vendor wrote: '₹', 'Rs.', 'Rs', 'INR' -> INR; '$', 'US$' -> USD."""
    s = (raw or "INR").strip().upper().replace(".", "")
    return _CURRENCY_ALIASES.get(s, s)


def normalize_price(value: float, stated_uom: str, pack_size: Optional[float], currency: str,
                    rfx_uom: str) -> Normalized:
    steps: List[str] = []
    v = float(value)
    cur = canonical_currency(currency)
    if cur == "USD":
        v *= USD_INR
        steps.append(f"fx: USD x {USD_INR} to INR")
    elif cur != "INR":
        return Normalized(None, steps, f"currency {cur} cannot be converted")

    su, ru = stated_uom, rfx_uom
    if su == ru:
        if su == "PACK" and pack_size not in (None, RFX_PACK_SIZE):
            return Normalized(None, steps, f"pack size {pack_size} differs from RFx pack of {RFX_PACK_SIZE}")
        return Normalized(round(v, 4), steps)
    if su == "PACK" and ru == "EA":
        if not pack_size:
            return Normalized(None, steps, "quoted per pack but pack size is missing")
        v = v / float(pack_size)
        steps.append(f"unit: per pack of {pack_size} to per piece (divide by {pack_size})")
        return Normalized(round(v, 4), steps)
    if su == "EA" and ru == "PACK":
        v = v * RFX_PACK_SIZE
        steps.append(f"unit: per piece to per {RFX_PACK_SIZE} pieces (multiply by {RFX_PACK_SIZE})")
        return Normalized(round(v, 4), steps)
    return Normalized(None, steps, f"cannot convert {su} to {ru}")


def stated_basis(uom: str, pack_size: Optional[float]) -> Tuple[str, Optional[float]]:
    """(uom, pack) pair as stated, for comparing a basis with the ground truth."""
    return uom, (float(pack_size) if pack_size else None)
