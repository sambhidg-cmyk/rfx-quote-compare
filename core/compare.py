"""Data behind the Compare tab: the price matrix, totals, trust counts, hard-requirement checks,
certificate expiry, and the review actions (accept / override) with an audit trail.

Everything here reads and writes the database. The Streamlit view only renders what this returns.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.schema import RFx

UOM_LABEL = {"EA": "per piece", "KG": "per kg", "PACK": "per 100 pcs", "SQM": "per sq m",
             "LOT": "per lot", "OTHER": "other"}
CONF_ICON = {"high": "🟢", "med": "🟡", "low": "🔴"}
CONF_WORD = {"high": "high", "med": "medium", "low": "low"}


@dataclass
class Cell:
    vendor_id: str
    line_id: str
    status: str  # quoted | not_quoted | no_response
    price: Optional[float] = None  # INR ex-tax per RFx unit (normalized); None if not converted
    confidence: Optional[str] = None  # high | med | low
    flagged: bool = False  # has an open review item
    quote_id: Optional[str] = None
    review_ids: List[str] = field(default_factory=list)
    qty: Decimal = Decimal(0)
    stated: Optional[str] = None  # as the vendor wrote it, e.g. "1531.00 per 100 pcs"
    steps: List[str] = field(default_factory=list)
    snippet: Optional[str] = None
    source_file: Optional[str] = None
    locator: Optional[str] = None
    lowest: bool = False


@dataclass
class Matrix:
    rfx_id: str
    lines: List[Dict[str, Any]]  # id, description, qty, uom
    vendors: List[Tuple[str, str]]  # (vendor_id, name)
    cells: Dict[Tuple[str, str], Cell]
    responses: Dict[str, Optional[str]]  # vendor_id -> response_id

    def cell(self, vendor_id: str, line_id: str) -> Cell:
        return self.cells[(vendor_id, line_id)]

    def coverage(self, vendor_id: str) -> Tuple[int, int]:
        quoted = sum(1 for l in self.lines if self.cell(vendor_id, l["id"]).price is not None)
        return quoted, len(self.lines)

    def partial_basket(self) -> Dict[str, Tuple[float, int]]:
        """Per vendor: total over the lines it priced, and how many lines that was."""
        out = {}
        for vid, _ in self.vendors:
            total, n = 0.0, 0
            for l in self.lines:
                c = self.cell(vid, l["id"])
                if c.price is not None:
                    total += c.price * float(l["qty"])
                    n += 1
            out[vid] = (total, n)
        return out

    def like_for_like_lines(self) -> List[str]:
        """Lines that every vendor with a response has priced."""
        active = [vid for vid, rid in self.responses.items() if rid]
        return [l["id"] for l in self.lines
                if active and all(self.cell(vid, l["id"]).price is not None for vid in active)]

    def like_for_like_basket(self) -> Dict[str, float]:
        common = set(self.like_for_like_lines())
        out = {}
        for vid, _ in self.vendors:
            if not self.responses.get(vid):
                continue  # no response, so no basket
            out[vid] = sum(self.cell(vid, l["id"]).price * float(l["qty"])
                           for l in self.lines if l["id"] in common)
        return out

    def trust_counts(self) -> Dict[str, int]:
        high = review = not_quoted = 0
        for vid, _ in self.vendors:
            for l in self.lines:
                c = self.cell(vid, l["id"])
                if c.status == "quoted" and c.price is not None and c.confidence == "high" and not c.flagged:
                    high += 1
                elif c.status in ("not_quoted", "no_response"):
                    not_quoted += 1
        review = sum(len(self.cell(vid, l["id"]).review_ids) for vid, _ in self.vendors for l in self.lines)
        return {"high": high, "review": review, "not_quoted": not_quoted}


def _cur(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """Cursor whose rows can be read by column name. Leaves the connection's own settings alone."""
    c = conn.cursor()
    c.row_factory = sqlite3.Row
    return c


def _loads(s: Optional[str]) -> Any:
    return json.loads(s) if s else None


def _stated_label(value: Any, uom: Optional[str], pack: Any, currency: Optional[str]) -> str:
    v = value if value is not None else "?"
    label = UOM_LABEL.get(uom or "", uom or "?")
    if uom == "PACK" and pack:
        label = f"per {int(float(pack))} pcs"
    return f"{currency or 'INR'} {v} {label}"


def build_matrix(conn: sqlite3.Connection, rfx: RFx, responses: Dict[str, Optional[str]],
                 names: Dict[str, str]) -> Matrix:
    """responses: vendor_id -> response_id to compare (None if the vendor has not responded)."""
    lines = [{"id": li.id, "description": li.description.value, "qty": li.qty.value,
              "uom": li.uom.value.value} for li in rfx.line_items]
    vendors = [(vid, names.get(vid, vid)) for vid in responses]
    rids = [r for r in responses.values() if r]
    cells: Dict[Tuple[str, str], Cell] = {}
    qty = {l["id"]: l["qty"] for l in lines}

    quotes: Dict[Tuple[str, str], sqlite3.Row] = {}
    open_reviews: Dict[str, List[sqlite3.Row]] = {}
    if rids:
        marks = ",".join("?" * len(rids))
        for r in _cur(conn).execute(
                f"SELECT q.id, q.response_id, q.line_item_id, q.unit_price_json, q.price_uom_json, "
                f"q.currency_json, q.pack_size_json, q.freight_terms_json, nq.unit_price_inr_ex_tax, "
                f"nq.steps_json FROM quote q LEFT JOIN normalized_quote nq ON nq.quote_id = q.id "
                f"WHERE q.response_id IN ({marks})", rids):
            quotes[(r["response_id"], r["line_item_id"])] = r
        for r in _cur(conn).execute(
                f"SELECT id, response_id, entity_id, field_name, issue, reason, proposed_value_json "
                f"FROM review_item WHERE status = 'open' AND response_id IN ({marks})", rids):
            open_reviews.setdefault(r["entity_id"], []).append(r)

    for vid, _ in vendors:
        rid = responses.get(vid)
        for l in lines:
            key = (vid, l["id"])
            if not rid:
                cells[key] = Cell(vid, l["id"], "no_response", qty=qty[l["id"]])
                continue
            q = quotes.get((rid, l["id"]))
            if q is None:
                pending = open_reviews.get(f"{rid}:{l['id']}", [])
                if pending:  # the vendor priced it, but the value needs a human decision first
                    cells[key] = Cell(vid, l["id"], "quoted", flagged=True, qty=qty[l["id"]],
                                      review_ids=[r["id"] for r in pending],
                                      stated="not converted yet", source_file=None)
                else:
                    cells[key] = Cell(vid, l["id"], "not_quoted", qty=qty[l["id"]])
                continue
            up = _loads(q["unit_price_json"]) or {}
            uom = (_loads(q["price_uom_json"]) or {}).get("value")
            cur = (_loads(q["currency_json"]) or {}).get("value")
            pack = (_loads(q["pack_size_json"]) or {}).get("value")
            flagged = open_reviews.get(q["id"], [])
            cell = Cell(
                vid, l["id"], "quoted",
                price=float(q["unit_price_inr_ex_tax"]) if q["unit_price_inr_ex_tax"] is not None else None,
                confidence=up.get("confidence"),
                flagged=bool(flagged),
                quote_id=q["id"],
                review_ids=[r["id"] for r in flagged],
                qty=qty[l["id"]],
                stated=_stated_label(up.get("value"), uom, pack, cur),
                steps=[s["description"] for s in (_loads(q["steps_json"]) or [])],
                snippet=up.get("source_snippet"),
                source_file=up.get("source_file"),
                locator=up.get("locator"),
            )
            cells[key] = cell

    matrix = Matrix(rfx.id, lines, vendors, cells, dict(responses))
    for l in lines:
        priced = [(vid, matrix.cell(vid, l["id"]).price) for vid, _ in vendors
                  if matrix.cell(vid, l["id"]).price is not None]
        if len(priced) >= 2:
            best = min(p for _, p in priced)
            for vid, p in priced:
                if abs(p - best) < 1e-9:
                    matrix.cell(vid, l["id"]).lowest = True
    return matrix


# ----------------------------------------------------------------- reviews


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(conn, rfx_id, actor, action, entity_type, entity_id, field_name, before, after, note=""):
    _cur(conn).execute(
        "INSERT INTO audit_log (at, actor, rfx_id, action, entity_type, entity_id, field_name, before_json, "
        "after_json, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_now(), actor, rfx_id, action, entity_type, entity_id, field_name,
         json.dumps(before, default=str), json.dumps(after, default=str), note))


def apply_review(conn: sqlite3.Connection, review_id: str, action: str, *, actor: str = "buyer",
                 new_value: Optional[float] = None, note: str = "") -> str:
    """Accept a proposed value, or override it with a buyer value. Returns a short confirmation."""
    if action not in ("accept", "override"):
        raise ValueError("action must be accept or override")
    item = _cur(conn).execute("SELECT * FROM review_item WHERE id = ?", (review_id,)).fetchone()
    if item is None:
        raise ValueError(f"no review item {review_id}")
    if item["status"] != "open":
        raise ValueError(f"review item {review_id} is already {item['status']}")
    rfx_row = _cur(conn).execute("SELECT rfx_id FROM response WHERE id = ?", (item["response_id"],)).fetchone()
    rfx_id = rfx_row[0] if rfx_row else None

    if action == "accept":
        proposed = _loads(item["proposed_value_json"])
        _cur(conn).execute("UPDATE review_item SET status = 'accepted', final_value_json = ? WHERE id = ?",
                     (json.dumps(proposed, default=str), review_id))
        _audit(conn, rfx_id, actor, "accept", item["entity_type"], item["entity_id"], item["field_name"],
               proposed, proposed, note or item["issue"])
        conn.commit()
        return f"Accepted {review_id}. The proposed value stands."

    if item["entity_type"] != "quote" or item["field_name"] != "unit_price":
        raise ValueError("only price overrides are supported; accept this item or correct the source")
    if new_value is None or not (new_value > 0):
        raise ValueError("override needs a price greater than zero")
    quote = _cur(conn).execute("SELECT id, line_item_id FROM quote WHERE id = ?", (item["entity_id"],)).fetchone()
    if quote is None:
        raise ValueError("the quote for this item does not exist, so it cannot be priced")
    nq = _cur(conn).execute("SELECT unit_price_inr_ex_tax FROM normalized_quote WHERE quote_id = ?",
                      (quote["id"],)).fetchone()
    before = float(nq[0]) if nq else None
    steps = [{"kind": "other", "description": f"manual override by {actor}: {note or 'no note'}", "factor": None}]
    if nq:
        _cur(conn).execute("UPDATE normalized_quote SET unit_price_inr_ex_tax = ?, steps_json = ? WHERE quote_id = ?",
                     (str(Decimal(str(new_value))), json.dumps(steps), quote["id"]))
    else:
        uom = _cur(conn).execute("SELECT uom_json FROM line_item WHERE id = ?", (quote["line_item_id"],)).fetchone()
        canonical = json.loads(uom[0])["value"] if uom else "EA"
        _cur(conn).execute(
            "INSERT INTO normalized_quote (id, quote_id, line_item_id, unit_price_inr_ex_tax, canonical_uom, "
            "steps_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
            (f"{quote['id']}:norm", quote["id"], quote["line_item_id"], str(Decimal(str(new_value))),
             canonical, json.dumps(steps), "[]"))
    _cur(conn).execute("UPDATE review_item SET status = 'overridden', final_value_json = ? WHERE id = ?",
                 (json.dumps(new_value), review_id))
    _audit(conn, rfx_id, actor, "override", "quote", quote["id"], "unit_price", before, new_value, note)
    conn.commit()
    return f"Overrode {review_id}: {before} -> {new_value}. Totals recomputed."


def open_reviews(conn: sqlite3.Connection, rfx_id: str) -> List[Dict[str, Any]]:
    rows = _cur(conn).execute(
        "SELECT r.id, r.response_id, r.entity_type, r.entity_id, r.field_name, r.issue, r.reason, "
        "r.proposed_value_json, v.name AS vendor FROM review_item r JOIN response s ON s.id = r.response_id "
        "JOIN vendor v ON v.id = s.vendor_id WHERE s.rfx_id = ? AND r.status = 'open' ORDER BY v.name, r.id",
        (rfx_id,)).fetchall()
    return [dict(r) | {"proposed": _loads(r["proposed_value_json"])} for r in rows]


def audit_trail(conn: sqlite3.Connection, rfx_id: str) -> List[Dict[str, Any]]:
    rows = _cur(conn).execute("SELECT * FROM audit_log WHERE rfx_id = ? ORDER BY id DESC", (rfx_id,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ questionnaire


HARD_REQUIREMENTS = [
    {"id": "HR1", "label": "In-house ECT/BCT testing", "question": "Q04", "rule": "in_house"},
    {"id": "HR2", "label": "ISO 9001 certificate valid today", "question": "Q01", "rule": "iso_valid"},
]
_DATE_RE = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{4}-\d{2}-\d{2})")
_YEAR_RE = re.compile(r"valid (?:to|till|until)\s+(\d{4})\b", re.I)


def answers_by_question(conn: sqlite3.Connection, responses: Dict[str, Optional[str]]) -> Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]:
    out: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {}
    rids = {rid: vid for vid, rid in responses.items() if rid}
    if not rids:
        return out
    marks = ",".join("?" * len(rids))
    for r in _cur(conn).execute(f"SELECT response_id, question_id, answer_json FROM questionnaire_answer "
                          f"WHERE response_id IN ({marks})", list(rids)):
        a = _loads(r[2]) or {}
        out.setdefault(r[1], {})[rids[r[0]]] = (a.get("value"), a.get("confidence"))
    return out


def _parse_date(text: str) -> Optional[date]:
    m = _DATE_RE.search(text)
    if m:
        s = m.group(1)
        for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None


def check_hard_requirement(rule: str, answer: Optional[str], today: Optional[date] = None) -> Tuple[str, str]:
    """('pass' | 'fail' | 'unclear', reason). A rule applied to the extracted answer, not a legal judgement."""
    today = today or date.today()
    if answer is None:
        return "unclear", "not answered"
    t = answer.lower()
    if rule == "in_house":
        if "outsourc" in t or "external" in t or "third-party" in t or "third party" in t:
            return "fail", "testing is outsourced"
        if "in-house" in t or "in house" in t:
            return "pass", "in-house testing stated"
        return "unclear", "answer does not say where testing is done"
    if rule == "iso_valid":
        # judge the ISO 9001 clause only: an answer may also say ISO 14001 is not held
        clauses = [c.strip() for c in re.split(r"[.;\n]", answer) if "9001" in c]
        if not clauses:
            return ("unclear", "no ISO 9001 statement") if "9001" not in t else ("unclear", "no ISO 9001 validity")
        answer = " ".join(clauses)
        t = answer.lower()
        if "not held" in t or "no iso" in t or "not certified" in t:
            return "fail", "ISO 9001 not held"
        d = _parse_date(answer)
        if d is not None:
            return ("pass", f"valid to {d:%d %b %Y}") if d >= today else ("fail", f"expired {d:%d %b %Y}")
        y = _YEAR_RE.search(answer)
        if y:
            return (("pass", f"valid to {y.group(1)}") if int(y.group(1)) > today.year
                    else ("fail", f"expired in {y.group(1)}"))
        if "to follow" in t or "pending" in t:
            return "unclear", "certificate not yet provided"
        return "unclear", "no validity date in the answer"
    return "unclear", f"unknown rule {rule}"


def hard_requirement_table(answers: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]],
                           vendors: Sequence[Tuple[str, str]], today: Optional[date] = None) -> List[Dict[str, Any]]:
    rows = []
    for hr in HARD_REQUIREMENTS:
        row: Dict[str, Any] = {"requirement": hr["label"], "question": hr["question"]}
        for vid, name in vendors:
            ans, _ = answers.get(hr["question"], {}).get(vid, (None, None))
            verdict, reason = check_hard_requirement(hr["rule"], ans, today)
            row[name] = f"{'PASS' if verdict == 'pass' else 'FAIL' if verdict == 'fail' else 'CHECK'}: {reason}"
        rows.append(row)
    return rows


# ---------------------------------------------------------- attached docs


def cert_expiry(path: Path, today: Optional[date] = None) -> Tuple[Optional[date], str]:
    """Read 'Valid until' from a PDF's text. Returns (date or None, 'valid'|'expired'|'unknown')."""
    today = today or date.today()
    try:
        from pypdf import PdfReader

        text = " ".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    except Exception:
        return None, "unknown"
    m = re.search(r"Valid until:\s*(\S+)", text)
    if not m:
        return None, "unknown"
    d = _parse_date(m.group(1))
    if d is None:
        return None, "unknown"
    return d, ("expired" if d < today else "valid")


def attached_docs(inbox_root: Path, rfx_id: str, vendor_id: str, today: Optional[date] = None) -> List[Dict[str, Any]]:
    folder = Path(inbox_root) / rfx_id / vendor_id
    rows = []
    if not folder.exists():
        return rows
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() == ".pdf":
            d, status = cert_expiry(p, today)
            rows.append({"file": p.relative_to(folder).as_posix(), "expiry": d.isoformat() if d else "-",
                         "status": status})
    return rows
