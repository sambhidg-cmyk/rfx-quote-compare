"""Approval checks and export of an RFx to PDF and Excel in an outbox folder.

Nothing here sends email or calls an external service. "Approve and send" means the
RFx is frozen, written to data/outbox/, and recorded in an approval file. Delivery to
vendors is a separate step.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from core.schema import RFx

BODY = ParagraphStyle("body", parent=getSampleStyleSheet()["BodyText"], fontSize=9, leading=12)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10)


def problems_for_approval(rfx: RFx) -> List[str]:
    """Blocking problems. An empty list means the RFx can be approved."""
    problems = []
    if not rfx.title.strip():
        problems.append("Title is empty. Ask the co-pilot to set one.")
    if not rfx.line_items:
        problems.append("No line items.")
    if rfx.terms.payment_terms is None:
        problems.append("Payment terms are not set.")
    if rfx.terms.delivery_terms is None:
        problems.append("Delivery terms are not set.")
    return problems


def _rows_lines(rfx: RFx) -> List[List[str]]:
    rows = []
    for li in rfx.line_items:
        spec = li.spec
        dims = spec.dimensions.value if spec.dimensions else None
        dim_text = ""
        if dims:
            dim_text = "x".join(str(int(x)) for x in
                                (dims.length_mm, dims.width_mm, dims.height_mm) if x is not None) + " mm"
        rows.append([
            li.id,
            li.description.value,
            ", ".join(p for p in [
                f"{spec.ply.value}-ply" if spec.ply else "",
                f"{spec.gsm.value} GSM" if spec.gsm else "",
                dim_text,
                f"printed ({spec.printing.value})" if spec.printing else "",
            ] if p),
            str(li.qty.value.normalize()),
            li.uom.value.value,
        ])
    return rows


def _terms_rows(rfx: RFx) -> List[List[str]]:
    t = rfx.terms
    out = []
    for label, ev in [("Payment terms", t.payment_terms), ("Delivery terms", t.delivery_terms),
                      ("Validity", t.validity)]:
        out.append([label, ev.value if ev else "-"])
    out.append(["Incoterms", t.incoterms.value.value if t.incoterms else "-"])
    return out


def write_pdf(rfx: RFx, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    story: List[Any] = [
        Paragraph(escape(rfx.title or "Request for Quotation"), styles["Title"]),
        Paragraph(f"RFx ID: <b>{escape(rfx.id)}</b> &nbsp;&nbsp; Category: {escape(rfx.category)}", BODY),
        Spacer(1, 6),
    ]
    if rfx.scope:
        story += [Paragraph("<b>Scope</b>", BODY), Paragraph(escape(rfx.scope), BODY), Spacer(1, 6)]
    story += [Paragraph("<b>Commercial terms</b>", BODY),
              _table([["Term", "Requirement"]] + _terms_rows(rfx), [45 * mm, 120 * mm]), Spacer(1, 8),
              Paragraph("<b>Line items</b>", BODY),
              _table([["ID", "Description", "Specification", "Annual qty", "UOM"]] + _rows_lines(rfx),
                     [12 * mm, 60 * mm, 60 * mm, 22 * mm, 16 * mm]), Spacer(1, 8)]
    if rfx.questionnaire:
        story += [Paragraph("<b>Questionnaire</b>", BODY),
                  _table([["ID", "Question", "Required"]] +
                         [[q.id, q.text, "yes" if q.required else "no"] for q in rfx.questionnaire],
                         [12 * mm, 140 * mm, 20 * mm])]
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=rfx.title or rfx.id)
    doc.build(story)
    return path


def _table(rows: List[List[str]], widths: List[float]) -> Table:
    data = [[Paragraph(escape(str(c)), CELL) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def write_xlsx(rfx: RFx, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = rfx.title or "Request for Quotation"
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"RFx ID", rfx.id])
    ws.append(["Category", rfx.category])
    ws.append(["Scope", rfx.scope])
    ws.append([])
    ws.append(["Term", "Requirement"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for r in _terms_rows(rfx):
        ws.append(r)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 90

    ls = wb.create_sheet("Line items")
    ls.append(["ID", "Description", "Specification", "Annual qty", "UOM"])
    for c in ls[1]:
        c.font = Font(bold=True)
    for r in _rows_lines(rfx):
        ls.append(r)
    for col, w in zip("ABCDE", [8, 55, 55, 14, 8]):
        ls.column_dimensions[col].width = w

    qs = wb.create_sheet("Questionnaire")
    qs.append(["ID", "Question", "Required"])
    for c in qs[1]:
        c.font = Font(bold=True)
    for q in rfx.questionnaire:
        qs.append([q.id, q.text, "yes" if q.required else "no"])
    qs.column_dimensions["B"].width = 90
    wb.save(path)
    return path


def approve_and_export(rfx: RFx, revision: int, outbox: Path) -> Dict[str, str]:
    """Write PDF, Excel and an approval record for this RFx revision.

    Raises ValueError if the RFx has blocking problems.
    """
    problems = problems_for_approval(rfx)
    if problems:
        raise ValueError("; ".join(problems))
    outbox.mkdir(parents=True, exist_ok=True)
    stem = f"{rfx.id}_r{revision:03d}"
    pdf = write_pdf(rfx, outbox / f"{stem}.pdf")
    xlsx = write_xlsx(rfx, outbox / f"{stem}.xlsx")
    payload = rfx.model_dump_json(indent=2)
    record = {
        "rfx_id": rfx.id,
        "revision": revision,
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approved_by": "buyer (co-pilot session)",
        "sha256_rfx_json": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "files": [pdf.name, xlsx.name],
        "delivery": "not sent: written to outbox only",
    }
    rec_path = outbox / f"{stem}.approval.json"
    rec_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    (outbox / f"{stem}.rfx.json").write_text(payload + "\n", encoding="utf-8")
    return {"pdf": str(pdf), "xlsx": str(xlsx), "approval": str(rec_path)}
