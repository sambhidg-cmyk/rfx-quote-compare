"""Award report: savings, risk notes, the Excel comparison workbook, and the award memo.

All figures come from core.award and core.compare. The memo writer (a model) receives a facts object
and may only use numbers that appear in it. Its draft is checked number by number before it is accepted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core import award as award_mod
from core.compare import Matrix
from core.schema import RFx

THIN_COVERAGE = 0.80  # vendors pricing less than this share of lines are flagged
MEMO_MAX_ATTEMPTS = 2


@dataclass
class Report:
    award: award_mod.Award
    savings_vs_highest: float
    highest_total: float  # highest-quote cost of the awarded lines
    savings_pct: float
    last_year_total: Optional[float]  # None: no last-year baseline in the data
    risks: List[Dict[str, Any]]
    excluded: Dict[str, str]  # vendor name -> reason it cannot win lines
    basis_note: str


def _qty(m: Matrix, lid: str) -> float:
    return float(next(l["qty"] for l in m.lines if l["id"] == lid))


def build_report(m: Matrix, a: award_mod.Award, excluded: Optional[Dict[str, str]] = None,
                 last_year_total: Optional[float] = None) -> Report:
    highest_total = 0.0
    for lid, vid in a.assignments.items():
        prices = [m.cell(v, lid).price for v, _ in m.vendors if m.cell(v, lid).price is not None]
        highest_total += max(prices) * _qty(m, lid) if prices else 0.0
    savings = highest_total - a.total
    pct = savings / highest_total if highest_total else 0.0

    risks: List[Dict[str, Any]] = []
    # single-source lines: only one responding vendor priced the line at all
    for l in m.lines:
        offers = [v for v, _ in m.vendors if m.cell(v, l["id"]).price is not None]
        if len(offers) == 1 and l["id"] in a.assignments:
            risks.append({"type": "single_source", "line": l["id"], "vendor": dict(m.vendors)[offers[0]],
                          "detail": "only one vendor priced this line"})
    for vid, name in m.vendors:
        q, t = m.coverage(vid)
        if m.responses.get(vid) and t and q / t < THIN_COVERAGE:
            risks.append({"type": "thin_coverage", "vendor": name,
                          "detail": f"priced {q} of {t} lines ({q / t:.0%})"})
        if not m.responses.get(vid):
            risks.append({"type": "no_response", "vendor": name, "detail": "no response in the system"})
    for item in a.unconfirmed_used:
        risks.append({"type": "unconfirmed_price", "line": item["line"], "vendor": dict(m.vendors)[item["vendor"]],
                      "detail": f"{item['confidence']} confidence" + (", open review item" if item["flagged"] else ""),
                      "quote_id": item["quote_id"]})
    for lid in a.unawarded:
        risks.append({"type": "unawarded", "line": lid, "detail": "no eligible priced offer"})
    if not a.feasible:
        risks.append({"type": "cap_not_met", "detail": "the vendor cap could not be met with these vendors"})
    return Report(award=a, savings_vs_highest=savings, highest_total=highest_total, savings_pct=pct,
                  last_year_total=last_year_total, risks=risks, excluded=excluded or {},
                  basis_note=award_mod.BASIS_NOTE)


# --------------------------------------------------------------- facts + memo


def build_facts(rfx: RFx, m: Matrix, rep: Report, scenario_text: str) -> Dict[str, Any]:
    a = rep.award
    names = dict(m.vendors)
    per_vendor = []
    for vid, _ in m.vendors:
        pv = a.per_vendor.get(vid, {"lines": 0, "value": 0.0, "share": 0.0})
        q, t = m.coverage(vid)
        per_vendor.append({"vendor": names[vid], "lines_awarded": int(pv["lines"]),
                           "award_value_inr": round(pv["value"], 2), "share_pct": round(100 * pv["share"], 1),
                           "lines_priced": q, "lines_in_rfx": t})
    risk_lines = []
    for r in rep.risks:
        risk_lines.append({k: v for k, v in r.items() if k != "quote_id"})
    facts = {
        "rfx_id": rfx.id,
        "rfx_title": rfx.title,
        "date": date.today().isoformat(),
        "scenario": scenario_text,
        "basis": rep.basis_note,
        "total_award_inr_ex_tax": round(a.total, 2),
        "lines_awarded": len(a.assignments),
        "lines_in_rfx": len(m.lines),
        "unawarded_lines": a.unawarded,
        "highest_quote_total_inr_ex_tax": round(rep.highest_total, 2),
        "savings_vs_highest_inr": round(rep.savings_vs_highest, 2),
        "savings_vs_highest_pct": round(100 * rep.savings_pct, 1),
        "last_year_total_inr": None if rep.last_year_total is None else round(rep.last_year_total, 2),
        "vendors": per_vendor,
        "risks": risk_lines,
        "excluded_vendors": rep.excluded,
        "unconfirmed_values_in_award": len(a.unconfirmed_used),
        "feasible": a.feasible,
    }
    return facts


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _values(text: str) -> List[Tuple[str, float]]:
    return [(tok, float(tok.replace(",", ""))) for tok in _NUM.findall(text)]


def allowed_values(facts: Dict[str, Any]) -> Set[float]:
    """Every number that appears in the facts, compared by value (so 1.00 and 1 are the same number)."""
    return {round(v, 6) for _, v in _values(json.dumps(facts, default=str))}


def unknown_numbers(text: str, facts: Dict[str, Any]) -> List[str]:
    """Numbers in the text whose value does not appear in the facts."""
    allowed = allowed_values(facts)
    return [tok for tok, v in _values(text) if round(v, 6) not in allowed]


MEMO_TOOL = {
    "name": "write_memo_sections",
    "description": "Return the award memo as sections. Use only numbers that appear in the facts, exactly as written.",
    "input_schema": {"type": "object", "properties": {
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "heading": {"type": "string"}, "body": {"type": "string"}}, "required": ["heading", "body"]}}},
        "required": ["sections"]},
}

MEMO_SYSTEM = """You write a 1-2 page award recommendation memo for a VP of procurement, from the facts provided.
Sections, in order: Recommendation, Justification, Savings, Risks, Open items.
Rules:
- Use ONLY numbers that appear in the facts, written exactly as they appear. Do not round, convert, add,
  or compute any number. If a figure you want is not in the facts, do not mention it.
- Name every vendor and line exactly as the facts do.
- Say plainly what is unconfirmed and what must be resolved before signing.
- Plain sentences. No marketing language. No invented claims."""


def _template_sections(facts: Dict[str, Any]) -> List[Dict[str, str]]:
    """Deterministic fallback, used only if the model's draft cannot pass the number check."""
    vendors = ", ".join(f"{v['vendor']} ({v['lines_awarded']} lines, INR {v['award_value_inr']:,.2f})"
                        for v in facts["vendors"] if v["lines_awarded"])
    risks = "; ".join(f"{r['type'].replace('_', ' ')}: {r.get('line', r.get('vendor', ''))}"
                      for r in facts["risks"]) or "none recorded"
    return [
        {"heading": "Recommendation", "body": f"Scenario: {facts['scenario']}. Award: {vendors}."},
        {"heading": "Justification", "body": f"Total award INR {facts['total_award_inr_ex_tax']:,.2f} ex-tax, "
                                             f"ex-freight. {facts['basis']}"},
        {"heading": "Savings", "body": f"Savings against the highest quote on the awarded lines: INR "
                                       f"{facts['savings_vs_highest_inr']:,.2f} ({facts['savings_vs_highest_pct']}%)."},
        {"heading": "Risks", "body": risks},
        {"heading": "Open items", "body": f"{facts['unconfirmed_values_in_award']} unconfirmed value(s) in the award. "
                                          "Resolve them before signing."},
    ]


def draft_memo(client, facts: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    """Model drafts the memo; numbers are checked against the facts; falls back to a template."""
    problems: List[str] = []
    for attempt in range(1, MEMO_MAX_ATTEMPTS + 1):
        messages = [{"role": "user", "content": "FACTS (JSON):\n" + json.dumps(facts, indent=1, default=str) +
                     ("\n\nYour previous draft used numbers that are not in the facts: " + "; ".join(problems) +
                      ". Rewrite it using only facts numbers." if problems else "")}]
        resp = client.messages.create(model=model, max_tokens=3000, system=MEMO_SYSTEM,
                                      tools=[MEMO_TOOL], tool_choice={"type": "tool", "name": MEMO_TOOL["name"]},
                                      messages=messages)
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            problems = ["no memo returned"]
            continue
        sections = (block.input or {}).get("sections") or []
        text = "\n".join(f"{s.get('heading', '')}\n{s.get('body', '')}" for s in sections)
        bad = unknown_numbers(text, facts)
        if sections and not bad:
            return {"sections": sections, "source": "model", "attempts": attempt}
        problems = [f"'{n}'" for n in bad[:10]] or ["empty memo"]
    return {"sections": _template_sections(facts), "source": "template", "attempts": MEMO_MAX_ATTEMPTS,
            "problems": problems}


def write_memo_docx(path: Path, rfx: RFx, memo: Dict[str, Any]) -> Path:
    from docx import Document
    from docx.shared import Pt

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    for style in ("Normal",):
        doc.styles[style].font.size = Pt(10.5)
    doc.add_heading(f"Award recommendation: {rfx.title or rfx.id}", level=1)
    note = f"RFx {rfx.id} · {date.today():%d %b %Y} · draft for review"
    if memo.get("source") == "template":
        note += " · drafted from template (model draft failed the number check)"
    doc.add_paragraph(note)
    for sec in memo["sections"]:
        doc.add_heading(sec["heading"], level=2)
        for para in str(sec["body"]).split("\n"):
            if para.strip():
                doc.add_paragraph(para)
    doc.save(path)
    return path


# ---------------------------------------------------------------- excel


def write_workbook(path: Path, rfx: RFx, m: Matrix, rep: Report, review_rows: Sequence[Dict[str, Any]],
                   source_rows: Sequence[Dict[str, Any]]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="D4F5D0")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    # Comparison: prices as values, all totals and minimums as formulas
    ws = wb.active
    ws.title = "Comparison"
    names = [name for _, name in m.vendors]
    ws.append(["Line", "Description", "Qty", "UOM"] + names + ["Lowest", "Lowest vendor"])
    for c in ws[1]:
        c.font = bold
    first_v = 5
    last_v = first_v + len(names) - 1
    lowest_col = get_column_letter(last_v + 1)
    for i, l in enumerate(m.lines, start=2):
        row = [l["id"], l["description"], float(l["qty"]), l["uom"]]
        for vid, _ in m.vendors:
            c = m.cell(vid, l["id"])
            row.append(c.price if c.price is not None else None)
        ws.append(row)
        rng = f"{get_column_letter(first_v)}{i}:{get_column_letter(last_v)}{i}"
        ws.cell(row=i, column=last_v + 1, value=f'=IF(COUNT({rng})=0,"",MIN({rng}))')
        ws.cell(row=i, column=last_v + 2,
                value=f'=IF(COUNT({rng})=0,"",INDEX(${get_column_letter(first_v)}$1:${get_column_letter(last_v)}$1,'
                      f'MATCH(MIN({rng}),{rng},0)))')
    last_line_row = 1 + len(m.lines)
    tot = last_line_row + 2
    ws.cell(row=tot, column=2, value="Partial basket (INR ex-tax, RFx qty)").font = bold
    ws.cell(row=tot + 1, column=2, value="Lines priced").font = bold
    for j in range(len(names)):
        col = get_column_letter(first_v + j)
        ws.cell(row=tot, column=first_v + j,
                value=f"=SUMPRODUCT({col}2:{col}{last_line_row},$C$2:$C${last_line_row})")
        ws.cell(row=tot + 1, column=first_v + j, value=f"=COUNT({col}2:{col}{last_line_row})")
    # highlight the lowest price per line in the comparison grid
    lowest_vals = {}
    for i, l in enumerate(m.lines, start=2):
        prices = [m.cell(vid, l["id"]).price for vid, _ in m.vendors if m.cell(vid, l["id"]).price is not None]
        if prices:
            lowest_vals[i] = min(prices)
    for i, lo in lowest_vals.items():
        for j, (vid, _) in enumerate(m.vendors):
            p = m.cell(vid, m.lines[i - 2]["id"]).price
            if p is not None and abs(p - lo) < 1e-9:
                ws.cell(row=i, column=first_v + j).fill = fill
    ws.column_dimensions["B"].width = 48
    for j in range(len(names) + 3):
        ws.column_dimensions[get_column_letter(j + 1)].width = 18

    # Award: assignments, line totals and savings as formulas
    wa = wb.create_sheet("Award")
    wa.append(["Line", "Vendor awarded", "Unit price (INR ex-tax)", "Qty", "Line total", "Highest quote",
               "Savings vs highest", "Confidence"])
    for c in wa[1]:
        c.font = bold
    for i, l in enumerate(m.lines, start=2):
        lid = l["id"]
        if lid not in rep.award.assignments:
            wa.append([lid, "not awarded", None, float(l["qty"]), None, None, None, None])
            continue
        vid = rep.award.assignments[lid]
        c = m.cell(vid, lid)
        prices = [m.cell(v, lid).price for v, _ in m.vendors if m.cell(v, lid).price is not None]
        wa.append([lid, dict(m.vendors)[vid], c.price, float(l["qty"]), f"=C{i}*D{i}",
                   max(prices) if prices else None, f"=(F{i}-C{i})*D{i}",
                   c.confidence or "unknown"])
    n = 1 + len(m.lines)
    wa.append([])
    wa.append(["Total", None, None, None, f"=SUM(E2:E{n})", None, f"=SUM(G2:G{n})"])
    tot_row = wa.max_row
    wa.cell(row=tot_row, column=1).font = bold
    wa.append([])
    wa.append(["Vendor", "Lines awarded", "Award value", "Share of total"])
    for c in wa[wa.max_row]:
        c.font = bold
    for vid, name in m.vendors:
        r = wa.max_row + 1
        wa.append([name, f'=COUNTIF($B$2:$B${n},A{r})', f'=SUMIF($B$2:$B${n},A{r},$E$2:$E${n})',
                   f"=IF($E${tot_row}=0,0,C{r}/$E${tot_row})"])
        wa.cell(row=r, column=4).number_format = "0.0%"
    for col, w in zip("ABCDEFGH", [10, 40, 22, 10, 18, 16, 18, 14]):
        wa.column_dimensions[col].width = w

    # Sources and review items: values, as recorded
    wsrc = wb.create_sheet("Sources")
    wsrc.append(["Quote id", "Vendor", "Line", "Stated value", "Stated unit", "Currency", "Normalized (INR ex-tax)",
                 "Confidence", "Open review", "Source file", "Locator", "Snippet", "Steps"])
    for c in wsrc[1]:
        c.font = bold
    for r in source_rows:
        wsrc.append([r.get("quote_id"), r.get("vendor"), r.get("line"), r.get("stated_value"), r.get("stated_uom"),
                     r.get("currency"), r.get("normalized"), r.get("confidence"), r.get("open_review"),
                     r.get("source_file"), r.get("locator"), r.get("snippet"), r.get("steps")])
    for col, w in zip("ABCDEFGHIJKLM", [30, 28, 8, 14, 16, 10, 20, 12, 12, 30, 22, 50, 50]):
        wsrc.column_dimensions[col].width = w

    wr = wb.create_sheet("Review items")
    wr.append(["Review id", "Vendor", "Entity", "Field", "Issue", "Reason", "Proposed value", "Status"])
    for c in wr[1]:
        c.font = bold
    for r in review_rows:
        wr.append([r.get("id"), r.get("vendor"), r.get("entity_type"), r.get("field_name"), r.get("issue"),
                   r.get("reason"), r.get("proposed"), r.get("status", "open")])
    for col, w in zip("ABCDEFGH", [30, 28, 18, 14, 40, 40, 16, 12]):
        wr.column_dimensions[col].width = w

    wb.save(path)
    return path


# ------------------------------------------------------- inputs for the Award tab


def eligible_by_requirements(conn, responses: Dict[str, Optional[str]], names: Dict[str, str]):
    """Vendors that pass every hard requirement, and the reason each other vendor is excluded."""
    from core import compare

    answers = compare.answers_by_question(conn, responses)
    passing: Set[str] = set()
    excluded: Dict[str, str] = {}
    for vid, name in names.items():
        if not responses.get(vid):
            excluded[name] = "no response"
            continue
        reasons = []
        for hr in compare.HARD_REQUIREMENTS:
            ans, _ = answers.get(hr["question"], {}).get(vid, (None, None))
            verdict, reason = compare.check_hard_requirement(hr["rule"], ans)
            if verdict != "pass":
                reasons.append(f"{hr['label']}: {reason}")
        if reasons:
            excluded[name] = "; ".join(reasons)
        else:
            passing.add(vid)
    return passing, excluded


def source_rows(conn, rfx_id: str) -> List[Dict[str, Any]]:
    """One row per priced quote, with its provenance, for the Sources sheet."""
    cur = conn.cursor()
    cur.row_factory = None
    rows = cur.execute(
        "SELECT q.id, v.name, q.line_item_id, q.unit_price_json, q.price_uom_json, q.currency_json, "
        "nq.unit_price_inr_ex_tax, nq.steps_json, "
        "(SELECT COUNT(*) FROM review_item r WHERE r.entity_id = q.id AND r.status = 'open') "
        "FROM quote q JOIN response s ON s.id = q.response_id JOIN vendor v ON v.id = s.vendor_id "
        "LEFT JOIN normalized_quote nq ON nq.quote_id = q.id WHERE s.rfx_id = ? ORDER BY v.name, q.line_item_id",
        (rfx_id,)).fetchall()
    out = []
    for qid, vname, lid, up, uom, cur_j, norm, steps, open_n in rows:
        up = json.loads(up)
        out.append({"quote_id": qid, "vendor": vname, "line": lid, "stated_value": up.get("value"),
                    "stated_uom": json.loads(uom).get("value"), "currency": json.loads(cur_j).get("value"),
                    "normalized": None if norm is None else float(norm), "confidence": up.get("confidence"),
                    "open_review": open_n, "source_file": up.get("source_file"), "locator": up.get("locator"),
                    "snippet": up.get("source_snippet"),
                    "steps": "; ".join(x["description"] for x in json.loads(steps or "[]"))})
    return out
