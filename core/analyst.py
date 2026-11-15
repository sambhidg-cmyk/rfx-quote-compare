"""Analyst: a tool-using agent that answers questions over the whole comparison.

The model reads and calls tools. It never does arithmetic itself: totals, shares and savings come
from core.award and from SQL run through run_sql. Every answer should cite the quote ids it used.

Tools: run_sql, get_source, basket_summary, confirmation_list, scenario, make_table, make_chart,
export, write_memo. Session memory is the message history kept in AnalystSession.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core import award as award_mod
from core import compare
from core.compare import Matrix, build_matrix
from core.schema import RFx

MAX_ROWS = 200
MAX_TOOL_CHARS = 12000
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "outbox" / "analysis"


class ToolError(Exception):
    pass


# ------------------------------------------------------------- read-only SQL

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def _authorizer(action, arg1, arg2, dbname, source):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _allow_all(action, arg1, arg2, dbname, source):
    return sqlite3.SQLITE_OK  # restore normal access; passing None does not reliably remove the guard


def run_select(conn: sqlite3.Connection, query: str, limit: int = 100) -> Dict[str, Any]:
    """One SELECT or WITH statement, read-only, with a row limit. Writes and PRAGMAs are denied."""
    q = (query or "").strip().rstrip(";").strip()
    if not q:
        raise ToolError("empty query")
    if ";" in q:
        raise ToolError("only one statement is allowed")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise ToolError("only SELECT or WITH queries are allowed")
    limit = max(1, min(int(limit or 100), MAX_ROWS))
    cur = conn.cursor()
    cur.row_factory = None
    conn.set_authorizer(_authorizer)
    try:
        cur.execute(f"SELECT * FROM ({q}) LIMIT ?", (limit + 1,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.DatabaseError as e:
        raise ToolError(f"SQL error: {e}") from e
    finally:
        conn.set_authorizer(_allow_all)
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows), "truncated": truncated,
            "row_limit": limit}


# ----------------------------------------------------------------- session


@dataclass
class AnalystSession:
    conn: sqlite3.Connection
    rfx: RFx
    responses: Dict[str, Optional[str]]  # vendor_id -> response_id
    names: Dict[str, str]  # vendor_id -> name
    out_dir: Path = DEFAULT_OUT
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    charts: Dict[str, bytes] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)

    def matrix(self) -> Matrix:
        return build_matrix(self.conn, self.rfx, self.responses, self.names)

    def vendor_id(self, name_or_id: str) -> str:
        for vid, name in self.names.items():
            if name_or_id in (vid, name) or name_or_id.lower() == name.lower():
                return vid
        for vid, name in self.names.items():
            if name_or_id.lower() in name.lower():
                return vid
        raise ToolError(f"unknown vendor {name_or_id!r}; known: {sorted(self.names.values())}")


# ------------------------------------------------------------------ tools


def _t_run_sql(s: AnalystSession, a: Dict[str, Any]):
    return run_select(s.conn, a.get("query", ""), a.get("limit", 100))


def _t_get_source(s: AnalystSession, a: Dict[str, Any]):
    qid = a.get("quote_id", "")
    row = s.conn.execute(
        "SELECT q.id, q.response_id, q.line_item_id, q.unit_price_json, q.price_uom_json, q.currency_json, "
        "q.pack_size_json, nq.unit_price_inr_ex_tax, nq.steps_json FROM quote q "
        "LEFT JOIN normalized_quote nq ON nq.quote_id = q.id WHERE q.id = ?", (qid,)).fetchone()
    if row is None:
        raise ToolError(f"no quote {qid}")
    up = json.loads(row[3])
    open_items = s.conn.execute("SELECT id, issue, reason FROM review_item WHERE entity_id = ? AND status = 'open'",
                                (qid,)).fetchall()
    return {
        "quote_id": qid,
        "line_id": row[2],
        "vendor_stated_value": up.get("value"),
        "vendor_stated_uom": json.loads(row[4]).get("value"),
        "vendor_stated_currency": json.loads(row[5]).get("value"),
        "pack_size": json.loads(row[6]).get("value") if row[6] else None,
        "normalized_inr_ex_tax_per_rfx_unit": row[7],
        "normalization_steps": [x["description"] for x in json.loads(row[8] or "[]")],
        "source_file": up.get("source_file"),
        "locator": up.get("locator"),
        "source_snippet": up.get("source_snippet"),
        "confidence": up.get("confidence"),
        "open_review_items": [dict(id=r[0], issue=r[1], reason=r[2]) for r in open_items],
    }


def _t_basket_summary(s: AnalystSession, a: Dict[str, Any]):
    m = s.matrix()
    partial = m.partial_basket()
    return {
        "rfx_lines": len(m.lines),
        "vendors": [{"vendor": name, "coverage": f"{m.coverage(vid)[0]} / {m.coverage(vid)[1]}",
                     "partial_basket_inr": round(partial[vid][0], 2),
                     "lines_priced": partial[vid][1]} for vid, name in m.vendors],
        "like_for_like": award_mod.like_for_like(m),
        "coverage_notes": award_mod.coverage_note(m),
        "basis": award_mod.BASIS_NOTE,
    }


def _t_confirmation_list(s: AnalystSession, a: Dict[str, Any]):
    m = s.matrix()
    rows = []
    for vid, name in m.vendors:
        for l in m.lines:
            c = m.cell(vid, l["id"])
            if c.status == "quoted" and award_mod._is_unconfirmed(c):
                rows.append({"quote_id": c.quote_id, "vendor": name, "line": l["id"],
                             "price": None if c.price is None else round(c.price, 4),
                             "confidence": c.confidence or "unknown", "flagged": c.flagged,
                             "open_review_items": c.review_ids,
                             "stated": c.stated, "source_file": c.source_file, "locator": c.locator})
    return {"count": len(rows), "items": rows}


def _requirement_results(s: AnalystSession) -> Dict[str, Dict[str, str]]:
    """Per vendor id: {requirement id: 'pass' | 'fail' | 'unclear'} from the questionnaire rules."""
    answers = compare.answers_by_question(s.conn, s.responses)
    out: Dict[str, Dict[str, str]] = {}
    for vid in s.names:
        if not s.responses.get(vid):
            continue
        out[vid] = {}
        for hr in compare.HARD_REQUIREMENTS:
            ans, _ = answers.get(hr["question"], {}).get(vid, (None, None))
            verdict, reason = compare.check_hard_requirement(hr["rule"], ans)
            out[vid][hr["id"]] = f"{verdict}: {reason}"
    return out


def _t_hard_requirements(s: AnalystSession, a: Dict[str, Any]):
    res = _requirement_results(s)
    rows = []
    for hr in compare.HARD_REQUIREMENTS:
        row = {"requirement": hr["label"], "question": hr["question"]}
        for vid, name in s.names.items():
            row[name] = res.get(vid, {}).get(hr["id"], "no response")
        rows.append(row)
    passing = [s.names[vid] for vid in res if all(v.startswith("pass") for v in res[vid].values())]
    return {"requirements": rows, "vendors_passing_all": passing,
            "note": "pass/fail/unclear is a rule applied to the extracted answer; unclear is not a pass"}


def _t_scenario(s: AnalystSession, a: Dict[str, Any]):
    m = s.matrix()
    eligible = a.get("eligible_vendors")
    if a.get("require_hard_requirements"):
        res = _requirement_results(s)
        passing = {vid for vid in res if all(v.startswith("pass") for v in res[vid].values())}
        eligible = list(passing) if eligible is None else [x for x in eligible if s.vendor_id(x) in passing]
    excl = a.get("exclude_vendors") or []
    cap = a.get("max_share")
    if cap is not None:
        cap = float(cap)
        cap = cap / 100 if cap > 1 else cap
        if not 0 < cap <= 1:
            raise ToolError("max_share must be between 0 and 100 percent")
    sc = award_mod.Scenario(
        eligible_vendors=None if eligible is None else {s.vendor_id(x) for x in eligible},
        exclude_vendors={s.vendor_id(x) for x in excl},
        max_share=cap,
        include_usd_lines=bool(a.get("include_usd_lines", True)),
        exclude_unconfirmed=bool(a.get("exclude_unconfirmed", False)),
        basis=a.get("basis", "ex_freight"))
    if sc.basis != "ex_freight":
        raise ToolError("landed (freight-inclusive) basis is not available: no response quantifies freight")
    result = award_mod.run_award(m, sc).to_dict()
    result["names"] = dict(s.names)
    result["like_for_like"] = award_mod.like_for_like(m)
    return result


def _t_make_table(s: AnalystSession, a: Dict[str, Any]):
    name = re.sub(r"\W+", "_", a.get("name", "table"))[:40] or "table"
    res = run_select(s.conn, a.get("query", ""), a.get("limit", 100))
    s.tables[name] = {"columns": res["columns"], "rows": res["rows"]}
    return {"table": name, "columns": res["columns"], "row_count": res["row_count"],
            "truncated": res["truncated"]}


def _t_make_chart(s: AnalystSession, a: Dict[str, Any]):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = s.tables.get(a.get("table", ""))
    if t is None:
        raise ToolError(f"no table {a.get('table')!r}; create it with make_table first")
    cols = t["columns"]
    if a.get("x") not in cols or a.get("y") not in cols:
        raise ToolError(f"x and y must be columns of the table: {cols}")
    xi, yi = cols.index(a["x"]), cols.index(a["y"])
    xs = [str(r[xi]) for r in t["rows"]]
    ys = []
    for r in t["rows"]:
        try:
            ys.append(float(r[yi]))
        except (TypeError, ValueError):
            raise ToolError(f"column {a['y']} has non-numeric values; cast it in the SQL") from None
    kind = a.get("kind", "bar")
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if kind == "bar":
        ax.bar(xs, ys, color="#3b6e8f")
        plt.xticks(rotation=30, ha="right")
    elif kind == "line":
        ax.plot(xs, ys, marker="o", color="#3b6e8f")
    elif kind == "scatter":
        ax.scatter(range(len(xs)), ys, color="#3b6e8f")
        ax.set_xticks(range(len(xs)))
        ax.set_xticklabels(xs, rotation=30, ha="right")
    else:
        raise ToolError("kind must be bar, line or scatter")
    ax.set_title(a.get("title", ""))
    ax.set_xlabel(a["x"])
    ax.set_ylabel(a["y"])
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    chart_id = f"chart{len(s.charts) + 1}"
    s.charts[chart_id] = buf.getvalue()
    return {"chart": chart_id, "points": len(xs), "rendered": True}


def _write_table(path: Path, columns, rows, fmt: str) -> None:
    if fmt == "csv":
        import csv

        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(columns)
            w.writerows(rows)
    else:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(columns)
        for r in rows:
            ws.append(r)
        wb.save(path)


def _t_export(s: AnalystSession, a: Dict[str, Any]):
    fmt = a.get("format", "xlsx")
    if fmt not in ("xlsx", "csv"):
        raise ToolError("format must be xlsx or csv")
    t = s.tables.get(a.get("table", ""))
    if t is None:
        raise ToolError("no such table; create it with make_table first")
    s.out_dir.mkdir(parents=True, exist_ok=True)
    path = s.out_dir / f"{a['table']}.{fmt}"
    _write_table(path, t["columns"], t["rows"], fmt)
    s.files.append(str(path))
    return {"file": str(path), "rows": len(t["rows"])}


def _t_write_memo(s: AnalystSession, a: Dict[str, Any]):
    fmt = a.get("format", "docx")
    if fmt not in ("docx", "pdf"):
        raise ToolError("format must be docx or pdf")
    title = a.get("title", "Award recommendation")
    sections = a.get("sections") or []
    if not sections:
        raise ToolError("a memo needs at least one section")
    s.out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"\W+", "_", a.get("filename", "award_memo"))[:60] or "award_memo"
    path = s.out_dir / f"{stem}.{fmt}"
    if fmt == "docx":
        from docx import Document

        doc = Document()
        doc.add_heading(title, level=1)
        doc.add_paragraph(f"RFx {s.rfx.id} · prepared {date.today():%d %b %Y} · draft for review")
        for sec in sections:
            doc.add_heading(sec["heading"], level=2)
            for para in str(sec["body"]).split("\n"):
                if para.strip():
                    doc.add_paragraph(para)
        doc.save(path)
    else:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        from xml.sax.saxutils import escape

        st = getSampleStyleSheet()
        story = [Paragraph(escape(title), st["Title"]),
                 Paragraph(escape(f"RFx {s.rfx.id} · prepared {date.today():%d %b %Y} · draft for review"),
                           st["Normal"]), Spacer(1, 8)]
        for sec in sections:
            story.append(Paragraph(escape(sec["heading"]), st["Heading2"]))
            for para in str(sec["body"]).split("\n"):
                if para.strip():
                    story.append(Paragraph(escape(para), st["BodyText"]))
        SimpleDocTemplate(str(path), pagesize=A4).build(story)
    s.files.append(str(path))
    return {"file": str(path), "sections": len(sections)}


TOOL_SPECS: List[Dict[str, Any]] = [
    {"name": "run_sql", "description": "Read-only SQL over the comparison database (SELECT or WITH only). "
     "Key tables: quote(id, response_id, line_item_id, unit_price_json, price_uom_json, currency_json, "
     "pack_size_json, validity_json, payment_terms_json, freight_terms_json, tax_terms_json, discount_terms_json), "
     "normalized_quote(id, quote_id, line_item_id, unit_price_inr_ex_tax TEXT, canonical_uom, steps_json), "
     "response(id, rfx_id, vendor_id, status), vendor(id, name), line_item(id, rfx_id, qty_json, uom_json, "
     "description_json), questionnaire_answer(response_id, question_id, answer_json), review_item(id, response_id, "
     "entity_type, entity_id, field_name, issue, reason, proposed_value_json, status). *_json columns hold "
     "{value, source_file, locator, source_snippet, confidence}. Use json_extract(col, '$.value').",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"},
                                                       "limit": {"type": "integer", "maximum": MAX_ROWS}},
                      "required": ["query"]}},
    {"name": "get_source", "description": "Source snippet, file, locator, confidence and open review items for one quote.",
     "input_schema": {"type": "object", "properties": {"quote_id": {"type": "string"}}, "required": ["quote_id"]}},
    {"name": "basket_summary", "description": "Per-vendor coverage, partial-basket totals on RFx quantities, "
     "like-for-like totals on lines every vendor priced, and coverage notes.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "confirmation_list", "description": "Every quoted price that is not confirmed (low or medium confidence, "
     "or with an open review item), with its source. Use before recommending a signature.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "hard_requirements", "description": "Hard-requirement results per vendor from the questionnaire "
     "(in-house ECT/BCT testing; ISO 9001 valid today). Use before saying any vendor is qualified.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "scenario", "description": "Run award logic. Lowest price per line among eligible vendors, optional "
     "cap on any vendor's share of award value (percent), optional exclusions, USD-line and unconfirmed-value "
     "filters. Returns totals, per-vendor shares, assignments, unconfirmed values used, and a baseline.",
     "input_schema": {"type": "object", "properties": {
         "eligible_vendors": {"type": "array", "items": {"type": "string"}, "description": "vendor names; omit for all"},
         "require_hard_requirements": {"type": "boolean", "description": "only vendors passing every hard requirement"},
         "exclude_vendors": {"type": "array", "items": {"type": "string"}},
         "max_share": {"type": "number", "description": "cap on any vendor's share, percent e.g. 40"},
         "include_usd_lines": {"type": "boolean"},
         "exclude_unconfirmed": {"type": "boolean"},
         "basis": {"type": "string", "enum": ["ex_freight", "landed"]}}}},
    {"name": "make_table", "description": "Store the result of a read-only query as a named table for charts and exports.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "query": {"type": "string"},
                                                       "limit": {"type": "integer"}},
                      "required": ["name", "query"]}},
    {"name": "make_chart", "description": "Render a bar, line or scatter chart from a stored table.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"}, "kind": {"type": "string", "enum": ["bar", "line", "scatter"]},
         "x": {"type": "string"}, "y": {"type": "string"}, "title": {"type": "string"}},
         "required": ["table", "x", "y"]}},
    {"name": "export", "description": "Write a stored table to xlsx or csv in the outbox.",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"},
                                                       "format": {"type": "string", "enum": ["xlsx", "csv"]}},
                      "required": ["table"]}},
    {"name": "write_memo", "description": "Write an award memo as docx or pdf. Sections are plain text; quote numbers "
     "exactly as the tools returned them.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "format": {"type": "string", "enum": ["docx", "pdf"]},
         "filename": {"type": "string"},
         "sections": {"type": "array", "items": {"type": "object", "properties": {
             "heading": {"type": "string"}, "body": {"type": "string"}}, "required": ["heading", "body"]}}},
         "required": ["title", "format", "sections"]}},
]

HANDLERS = {
    "run_sql": _t_run_sql, "get_source": _t_get_source, "basket_summary": _t_basket_summary,
    "hard_requirements": _t_hard_requirements,
    "confirmation_list": _t_confirmation_list, "scenario": _t_scenario, "make_table": _t_make_table,
    "make_chart": _t_make_chart, "export": _t_export, "write_memo": _t_write_memo,
}


def run_tool(s: AnalystSession, name: str, args: Dict[str, Any]) -> Any:
    h = HANDLERS.get(name)
    if h is None:
        raise ToolError(f"unknown tool {name}")
    return h(s, args)


# ------------------------------------------------------------------ loop

SYSTEM = """You are the buyer's analyst for a corrugated packaging RFx. The buyer asks questions about the
vendor comparison. You answer by calling tools. Rules:
1. Never do arithmetic in your head. Totals, shares, savings and rankings come from scenario, basket_summary
   or run_sql. If a number is not in a tool result, do not state it.
2. Show the numbers you used, briefly, and cite quote ids for any price you mention.
3. Flag uncertainty. Call confirmation_list before recommending an award, and say how many values the answer
   depends on that are unconfirmed (low or medium confidence, or open review). Name them.
4. Push back when a question is ill-posed. If a vendor priced only some lines, a full-basket comparison is unfair:
   say so and give the like-for-like view. If a baseline is not in the data, say it is missing; do not invent it.
5. Freight is not quantified anywhere. A landed comparison cannot be produced; say that when relevant.
5b. Never say a vendor is qualified, compliant or cleared unless hard_requirements says so. Never invent
   questionnaire items; use run_sql on questionnaire_answer only to read answers that exist.
6. Use the conversation so far. If the buyer says "now exclude vendor B", apply it to the previous question's setup.
7. Keep answers short and plain for a procurement buyer. Use tables only when they help.
Vendors: {vendors}. RFx: {rfx}. Vendors without a response: {missing}."""


def _system(s: AnalystSession) -> str:
    missing = [n for vid, n in s.names.items() if not s.responses.get(vid)]
    return SYSTEM.format(vendors=", ".join(s.names.values()), rfx=s.rfx.id, missing=", ".join(missing) or "none")


def _block_dict(block) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return {k: v for k, v in vars(block).items() if v is not None}


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TOOL_CHARS else text[:MAX_TOOL_CHARS] + "\n...[truncated]"


def ask(client, s: AnalystSession, question: str, *, model: str, max_rounds: int = 12) -> Dict[str, Any]:
    """One buyer turn. Runs the tool loop until the model answers. Memory persists in the session."""
    s.messages.append({"role": "user", "content": question})
    calls: List[Dict[str, Any]] = []
    artifacts_before = (len(s.charts), len(s.files), len(s.tables))
    text_parts: List[str] = []
    finished = False
    for _ in range(max_rounds):
        resp = client.messages.create(model=model, max_tokens=4096, system=_system(s), tools=TOOL_SPECS,
                                      messages=s.messages)
        blocks = list(resp.content)
        s.messages.append({"role": "assistant", "content": [_block_dict(b) for b in blocks]})
        text_parts += [b.text for b in blocks if b.type == "text" and b.text]
        uses = [b for b in blocks if b.type == "tool_use"]
        if not uses:
            finished = True
            break
        results = []
        for u in uses:
            args = dict(u.input or {})
            try:
                out = run_tool(s, u.name, args)
                content, err = _clip(json.dumps(out, default=str)), False
            except ToolError as e:
                content, err = str(e), True
            except Exception as e:  # a bug in a tool must not kill the chat
                content, err = f"internal error in {u.name}: {e}", True
            calls.append({"tool": u.name, "args": args, "error": err, "result_chars": len(content)})
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": content, "is_error": err})
        s.messages.append({"role": "user", "content": results})
    if not finished:
        text_parts.append("(stopped after too many tool steps; ask a narrower question)")

    new_charts = list(s.charts)[artifacts_before[0]:]
    turn = {"question": question, "answer": "\n".join(t for t in text_parts if t).strip(), "tool_calls": calls,
            "charts": new_charts, "files": s.files[artifacts_before[1]:],
            "tables": list(s.tables)[artifacts_before[2]:]}
    s.transcript.append(turn)
    return turn
