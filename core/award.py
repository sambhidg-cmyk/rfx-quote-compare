"""Award scenarios over the comparison matrix.

Every number the analyst reports comes from here. The model never does arithmetic itself.

Basis: prices are INR ex-tax per RFx unit, and freight is NOT included, because no response
quantifies freight. A landed view is therefore not available, and the award says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from core.compare import Cell, Matrix

BASIS_NOTE = ("Prices are ex-tax and ex-freight. No response quantifies freight, so a landed "
              "comparison cannot be produced.")


@dataclass
class Scenario:
    eligible_vendors: Optional[Set[str]] = None  # vendor ids allowed to win lines; None = all
    exclude_vendors: Set[str] = field(default_factory=set)
    max_share: Optional[float] = None  # 0..1, cap on any vendor's share of award value
    include_usd_lines: bool = True
    exclude_unconfirmed: bool = False  # drop cells that are low/medium confidence or flagged for review
    basis: str = "ex_freight"

    def describe(self) -> str:
        parts = []
        if self.eligible_vendors is not None:
            parts.append(f"eligible: {sorted(self.eligible_vendors)}")
        if self.exclude_vendors:
            parts.append(f"excluded: {sorted(self.exclude_vendors)}")
        if self.max_share is not None:
            parts.append(f"cap {self.max_share:.0%} per vendor")
        if not self.include_usd_lines:
            parts.append("USD-sourced lines excluded")
        if self.exclude_unconfirmed:
            parts.append("unconfirmed values excluded")
        return "; ".join(parts) or "no restrictions"


@dataclass
class Award:
    scenario: str
    assignments: Dict[str, str]  # line_id -> vendor_id
    per_vendor: Dict[str, Dict[str, float]]  # vendor_id -> {"lines", "value", "share"}
    total: float
    unawarded: List[str]  # lines with no eligible priced offer
    unconfirmed_used: List[Dict[str, object]]  # cells that feed the total and are not confirmed
    baseline_total: float  # cheapest per line with no restrictions
    feasible: bool
    notes: List[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "scenario": self.scenario,
            "total_inr_ex_tax": round(self.total, 2),
            "baseline_total_inr_ex_tax": round(self.baseline_total, 2),
            "difference_vs_baseline": round(self.total - self.baseline_total, 2),
            "feasible": self.feasible,
            "per_vendor": {k: {"lines": int(v["lines"]), "value": round(v["value"], 2),
                               "share_pct": round(100 * v["share"], 1)} for k, v in self.per_vendor.items()},
            "assignments": self.assignments,
            "unawarded_lines": self.unawarded,
            "unconfirmed_values_in_award": self.unconfirmed_used,
            "notes": self.notes,
        }


def _is_usd(cell: Cell) -> bool:
    return bool(cell.stated and cell.stated.upper().startswith("USD"))


def _is_unconfirmed(cell: Cell) -> bool:
    return cell.flagged or cell.confidence in (None, "low", "med")


def candidates(m: Matrix, sc: Scenario) -> Dict[str, List[Tuple[str, Cell]]]:
    """Per line, the eligible priced offers sorted cheapest first."""
    out: Dict[str, List[Tuple[str, Cell]]] = {}
    for l in m.lines:
        offers = []
        for vid, _ in m.vendors:
            c = m.cell(vid, l["id"])
            if c.price is None:
                continue
            if vid in sc.exclude_vendors:
                continue
            if sc.eligible_vendors is not None and vid not in sc.eligible_vendors:
                continue
            if not sc.include_usd_lines and _is_usd(c):
                continue
            if sc.exclude_unconfirmed and _is_unconfirmed(c):
                continue
            offers.append((vid, c))
        offers.sort(key=lambda t: t[1].price)
        out[l["id"]] = offers
    return out


def _qty(m: Matrix, line_id: str) -> float:
    return float(next(l["qty"] for l in m.lines if l["id"] == line_id))


def _spend(m: Matrix, assign: Dict[str, Tuple[str, Cell]]) -> Dict[str, float]:
    spend: Dict[str, float] = {vid: 0.0 for vid, _ in m.vendors}
    for lid, (vid, c) in assign.items():
        spend[vid] += c.price * _qty(m, lid)
    return spend


def _baseline(m: Matrix) -> float:
    total = 0.0
    for l in m.lines:
        priced = [m.cell(vid, l["id"]).price for vid, _ in m.vendors if m.cell(vid, l["id"]).price is not None]
        if priced:
            total += min(priced) * _qty(m, l["id"])
    return total


def run_award(m: Matrix, sc: Scenario) -> Award:
    cands = candidates(m, sc)
    assign: Dict[str, Tuple[str, Cell]] = {lid: offers[0] for lid, offers in cands.items() if offers}
    unawarded = [lid for lid, offers in cands.items() if not offers]
    notes: List[str] = [BASIS_NOTE]
    feasible = True

    if sc.max_share is not None and assign:
        cap = sc.max_share
        for _ in range(10_000):
            spend = _spend(m, assign)
            total = sum(spend.values())
            over = [v for v, s in spend.items() if total and s > cap * total + 1e-9]
            if not over:
                break
            moved = False
            best = None
            for lid, (vid, c) in assign.items():
                if vid not in over:
                    continue
                alts = [(v2, c2) for v2, c2 in cands[lid] if v2 != vid]
                if not alts:
                    continue
                v2, c2 = alts[0]
                extra = (c2.price - c.price) * _qty(m, lid)
                if best is None or extra < best[0]:
                    best = (extra, lid, v2, c2)
            if best is None:
                feasible = False
                notes.append(f"No split within the {cap:.0%} cap was found: " + ", ".join(
                    f"{v} would hold {spend[v] / total:.0%}" for v in over) +
                    " and no alternative offer exists for their remaining lines. "
                    "This method is greedy, so a feasible split may still exist if other lines move.")
                break
            _, lid, v2, c2 = best
            assign[lid] = (v2, c2)
        else:
            feasible = False  # iteration limit reached without meeting the cap
            notes.append("No split within the cap was found within the iteration limit.")
        spend_final = _spend(m, assign)
        total_final = sum(spend_final.values())
        if total_final and any(s_ > cap * total_final + 1e-9 for s_ in spend_final.values()) and feasible:
            feasible = False
            notes.append(f"No split within the {cap:.0%} cap was found after rebalancing.")

    spend = _spend(m, assign)
    total = sum(spend.values())
    per_vendor = {}
    for vid, _ in m.vendors:
        lines = [lid for lid, (v, _) in assign.items() if v == vid]
        per_vendor[vid] = {"lines": len(lines), "value": spend[vid],
                           "share": (spend[vid] / total) if total else 0.0}

    unconfirmed = []
    for lid, (vid, c) in assign.items():
        if _is_unconfirmed(c):
            unconfirmed.append({"quote_id": c.quote_id, "vendor": vid, "line": lid,
                                "confidence": c.confidence or "unknown", "flagged": c.flagged,
                                "price": round(c.price, 4)})

    if sc.max_share is not None and feasible and total:
        top = max(per_vendor.values(), key=lambda v: v["share"])["share"]
        notes.append(f"Largest vendor share after the cap: {top:.1%}.")
    if unawarded:
        notes.append(f"{len(unawarded)} line(s) have no eligible priced offer: {', '.join(unawarded)}.")

    return Award(scenario=sc.describe(), assignments={lid: v for lid, (v, _) in assign.items()},
                 per_vendor=per_vendor, total=total, unawarded=unawarded,
                 unconfirmed_used=unconfirmed, baseline_total=_baseline(m), feasible=feasible, notes=notes)


def like_for_like(m: Matrix) -> Dict[str, object]:
    """Totals over only the lines every vendor with a response priced."""
    lines = m.like_for_like_lines()
    totals = m.like_for_like_basket()
    active = [vid for vid, rid in m.responses.items() if rid]
    return {"lines": lines, "totals": {vid: round(totals.get(vid, 0.0), 2) for vid in active},
            "names": dict(m.vendors), "note": BASIS_NOTE}


def coverage_note(m: Matrix) -> List[str]:
    out = []
    for vid, name in m.vendors:
        q, t = m.coverage(vid)
        if m.responses.get(vid) and q < t:
            out.append(f"{name} priced {q} of {t} lines, so its full-basket total is partial.")
        if not m.responses.get(vid):
            out.append(f"{name} has no response in the system yet.")
    return out
