#!/usr/bin/env python3
"""Generate a fictional corrugated-packaging RFx and five messy vendor responses.

Everything is synthetic: buyer, vendors, certificates and test reports are invented.
Certificates and test reports carry a SPECIMEN watermark and a synthetic-data footer.

Outputs (all under data/):
  rfx/rfx.json                  the RFx, validated as core.schema.RFx
  responses/vendor_{a..e}/      what each vendor sent: xlsx, pdf, docx, jpg, txt, certs
  ground_truth.json             EVALUATION ONLY. See the comment inside the file and
                                tests/eval/test_ground_truth_isolation.py. Nothing under
                                app/ or core/ may read it.

Run from the project root:   python3 scripts/generate_dataset.py
Deterministic: the same SEED always produces the same files.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import textwrap
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import numpy as np
from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.schema import (  # noqa: E402
    RFx,
    Confidence,
    Dimensions,
    ExtractedValue,
    Incoterm,
    LineItem,
    LineItemSpec,
    Question,
    Terms,
    Uom,
)

DATA = ROOT / "data"
RESP = DATA / "responses"
SEED = 20261114
USD_INR = 83.50  # fixed rate used to build ground truth for USD-quoted lines
TODAY = date(2026, 11, 14)
RFX_ID = "RFX-2026-PKG-017"
BUYER = "Kiranaplus Commerce Pvt. Ltd."
SYNTH_FOOTER = "SYNTHETIC TEST DATA - not a real document"

FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

PRIOR_YEAR_FACTOR = 0.94
VENDOR_FACTOR = {"vendor_a": 1.00, "vendor_b": 1.02, "vendor_c": 0.975, "vendor_d": 1.04}
VENDOR_NAMES = {
    "vendor_a": "Vardhan Packaging Works Pvt. Ltd.",
    "vendor_b": "Bhiwandi Corrugated Industries LLP",
    "vendor_c": "Sahyadri Boxes & Sheets",
    "vendor_d": "Ganesh Kraft Traders",
    "vendor_e": "Nakoda Paper Packs",
}
FREIGHT = {"vendor_a": "not_stated", "vendor_b": "extra", "vendor_c": "included",
           "vendor_d": "not_stated", "vendor_e": "extra"}  # A and D never mention freight
D_PER_100 = {"L01", "L03", "L05", "L07", "L09", "L12"}  # EA lines Vendor D quotes per 100 pcs
C_NO_BID = {"L22", "L28", "L29"}  # Vendor C quotes 27 of 30
C_APPROX = {"L02", "L11", "L16"}  # Vendor C writes "around Rs X" for these
E_EMAIL_RATES = {"L18": 38.0, "L19": 42.0}  # 3-ply and 5-ply rolls, per kg, in the email
UNIT_TXT = {"EA": "piece", "KG": "kg", "PACK": "100 pcs", "SQM": "sq m"}
UNIT_DISPLAY = {"EA": "per pc", "KG": "per kg", "PACK": "per 100 pcs", "SQM": "per sq m"}


@dataclass(frozen=True)
class Line:
    id: str
    desc: str
    ply: Optional[int]
    gsm: Optional[int]
    dims: Optional[Tuple[Optional[int], Optional[int], Optional[int]]]
    printing: Optional[str]
    uom: str  # EA | KG | PACK (per 100 pcs) | SQM
    qty: int
    base: float  # canonical INR ex-tax per uom
    imported: bool = False


LINES: List[Line] = [
    Line("L01", "RSC shipper box", 3, 150, (200, 150, 100), None, "EA", 180000, 14.20),
    Line("L02", "RSC shipper box", 3, 150, (250, 200, 150), None, "EA", 150000, 19.80),
    Line("L03", "RSC shipper box", 3, 150, (300, 225, 150), "1-colour flexo", "EA", 96000, 25.40),
    Line("L04", "Mailer box", 3, 150, (320, 240, 80), "2-colour flexo", "EA", 60000, 27.90),
    Line("L05", "Die-cut mailer", 3, 120, (180, 130, 60), None, "EA", 220000, 9.60),
    Line("L06", "Heavy-duty box", 5, 180, (400, 300, 300), None, "EA", 42000, 52.00),
    Line("L07", "Heavy-duty box", 5, 180, (450, 350, 250), "2-colour flexo", "EA", 36000, 61.50),
    Line("L08", "Box", 5, 180, (350, 250, 200), None, "EA", 54000, 41.00),
    Line("L09", "Heavy-duty box", 7, 220, (600, 400, 400), None, "EA", 9000, 118.00),
    Line("L10", "Heavy-duty box", 7, 220, (550, 450, 350), "2-colour flexo", "EA", 7200, 132.50),
    Line("L11", "Box", 5, 180, (500, 400, 150), "2-colour flexo", "EA", 22000, 58.00),
    Line("L12", "RSC shipper box", 3, 120, (280, 200, 100), None, "EA", 85000, 21.00),
    Line("L13", "Cell partition insert (per 100 pcs)", 3, 120, (280, 190, None), None, "PACK", 1500, 310.00),
    Line("L14", "6-cell divider (per 100 pcs)", 5, 180, (400, 300, None), None, "PACK", 600, 520.00),
    Line("L15", "Corrugated sheet (per sq m)", 3, 150, None, None, "SQM", 180000, 26.50),
    Line("L16", "Corrugated sheet (per sq m)", 5, 180, None, None, "SQM", 90000, 43.00),
    Line("L17", "Corrugated sheet, pre-printed (per sq m)", 3, 150, None, "1-colour pre-print", "SQM", 40000, 34.00),
    Line("L18", "Corrugated roll 1200 mm (per kg)", 3, 150, None, None, "KG", 110000, 38.00),
    Line("L19", "Corrugated roll 1200 mm (per kg)", 5, 180, None, None, "KG", 85000, 42.00),
    Line("L20", "Corrugated roll 1200 mm (per kg)", 7, 220, None, None, "KG", 18000, 46.50),
    Line("L21", "L-type edge protector 40x40x1.5 mm (per 100 pcs)", None, None, None, None, "PACK", 2200, 48.00),
    Line("L22", "Corner board 150x150x5 mm (per 100 pcs)", 5, 180, None, None, "PACK", 900, 95.00),
    Line("L23", "Machine-grade stretch film 500 mm x 23 micron (per kg)", None, None, None, None, "KG", 14000, 192.00),
    Line("L24", "Hand stretch roll 300 mm x 17 micron (per kg)", None, None, None, None, "KG", 3000, 205.00),
    Line("L25", "BOPP tape 48 mm x 100 m, clear", None, None, None, None, "EA", 60000, 92.00, True),
    Line("L26", "BOPP tape 48 mm x 100 m, printed logo", None, None, None, None, "EA", 25000, 118.00, True),
    Line("L27", "Pallet 1200x1000 mm, heat-treated wood, 4-way", None, None, (1200, 1000, None), None, "EA", 1800, 690.00),
    Line("L28", "Pallet 1100x1100 mm, plastic, export grade", None, None, (1100, 1100, None), None, "EA", 400, 1450.00),
    Line("L29", "Heavy-duty box, plain", 7, 220, (500, 350, 300), None, "EA", 9500, 96.00),
    Line("L30", "Corrugated sheet, high-BF, imported (per sq m)", 3, 150, None, None, "SQM", 22000, 39.00, True),
]
LINE_BY_ID = {ln.id: ln for ln in LINES}

QUESTIONS: List[Tuple[str, str]] = [
    ("Q01", "Quality and environmental certifications: ISO 9001 and ISO 14001 (certificate number and validity)"),
    ("Q02", "FSC chain-of-custody certification"),
    ("Q03", "BIS registration relevant to corrugated packaging (registration number)"),
    ("Q04", "In-house testing for ECT, BCT and Cobb (list equipment and standards)"),
    ("Q05", "Monthly production capacity (tonnes of corrugated board)"),
    ("Q06", "Number of plants and their locations"),
    ("Q07", "Standard payment terms (days from invoice)"),
    ("Q08", "Lead time for a first purchase order (days from PO)"),
    ("Q09", "Three reference customers in FMCG or e-commerce"),
    ("Q10", "Sample policy (number of free samples, lead time, cost recovery)"),
    ("Q11", "Transit insurance (who arranges it, cover limit)"),
    ("Q12", "ESG: recycled-fibre content (%) and renewable share of plant power"),
]

# Answers in each vendor's own words. None = not answered.
ANSWERS: Dict[str, List[Optional[str]]] = {
    "vendor_a": [
        "ISO 9001:2015 cert VPW-QMS-4471, valid to 31-Mar-2027. ISO 14001:2015 cert VPW-EMS-2290, valid to 31-Mar-2027.",
        "Yes. FSC chain-of-custody licence DEMO-FSC-1182, valid to 30-Jun-2027.",
        "Yes. BIS registration DEMO-BIS-55521.",
        "Yes. In-house ECT, BCT and Cobb test lab at Plant 1. Equipment list attached.",
        "1,450 tonnes per month.",
        "Two plants: Bhiwandi (Maharashtra) and Sanand (Gujarat).",
        "45 days from invoice.",
        "12 days from PO.",
        "Ojas Foods; Trendmart Online; Fabrica Snacks.",
        "10 free samples per SKU, 5-day lead time. Cost recovered on bulk order.",
        "Vendor arranges transit insurance; cover up to 110% of invoice value.",
        "70% recycled fibre content. 40% of plant power from on-site solar.",
    ],
    "vendor_b": [
        "ISO 9001:2015 cert BCI-QMS-0093, valid to 31-Aug-2026. Renewal audit scheduled. ISO 14001:2015 valid to 15-Jan-2027.",
        "Yes. FSC CoC valid to 28-Feb-2027.",
        "Not held. Application filed Oct-2026.",
        "In-house ECT and BCT testing. Cobb testing outsourced.",
        "900 tonnes per month.",
        "One plant (Bhiwandi).",
        "30 days from invoice.",
        "10 days from PO.",
        "References on request: Fabrica Snacks, Ojas Foods.",
        "5 free samples; additional samples charged at cost.",
        "Buyer arranges transit insurance.",
        "55% recycled content. No renewable power data.",
    ],
    "vendor_c": [
        "ISO 9001:2015 cert SBS-44-8810, valid to 31-May-2027. No ISO 14001.",
        "No FSC CoC. FSC-mix board available at a 4% premium.",
        "Yes. BIS registration SBS/BIS/0021.",
        "ECT and BCT testing is outsourced to an external NABL-accredited lab (report attached). No in-house test equipment.",
        "620 tonnes per month.",
        "One plant (Pune).",
        "60 days from invoice.",
        "15 days from PO.",
        "Mahavir Organics; Bluecart Retail; Tiffin Express.",
        "No free samples. Samples at cost, Rs 350 per box size.",
        "Freight and transit insurance included in delivered price.",
        "35% recycled content. No renewable power data.",
    ],
    "vendor_d": [
        "ISO 9001:2015 cert GKT-QMS-7712, valid to 31-Jul-2027. ISO 14001 not held.",
        "FSC not held.",
        "BIS not held.",
        "In-house BCT and ECT press, Cobb tester. Report attached.",
        "700 tonnes per month.",
        "Two plants: Bhiwandi and Vapi.",
        "30 days.",
        "14 days.",
        "Two references, names on request.",
        "Up to 3 free samples per item, 2-day turnaround.",
        "Buyer arranges insurance.",
        "60% recycled content. Solar on two plant roofs.",
    ],
    "vendor_e": [
        "ISO 9001 yes, certificate to follow.",
        "ISO 14001 not yet.",
        "FSC yes, chain of custody.",
        "BIS yes, will confirm the number.",
        "Yes, in-house ECT, BCT and Cobb testing.",
        "About 500 MT a month.",
        "One plant, Bhiwandi.",
        "30 days net.",
        "10 days from PO.",
        "Bluecart Retail and Fabrica Snacks.",
        "Samples on request; first two free.",
        None,
    ],
}


# ----------------------------------------------------------------- helpers


def _rng(*parts) -> random.Random:
    return random.Random("|".join([str(SEED), *map(str, parts)]))


def spec_str(ln: Line) -> str:
    parts = []
    if ln.ply:
        parts.append(f"{ln.ply}-ply")
    if ln.gsm:
        parts.append(f"{ln.gsm} GSM")
    if ln.dims:
        parts.append("x".join(str(x) for x in ln.dims if x) + " mm")
    if ln.printing:
        parts.append(f"printed ({ln.printing})")
    return ", ".join(parts) if parts else "-"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    except OSError:
        return ImageFont.load_default()


def _register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("Arial", FONT_REG))
    pdfmetrics.registerFont(TTFont("Arial-Bold", FONT_BOLD))


BODY = ParagraphStyle("body", fontName="Arial", fontSize=9, leading=12)
SMALL = ParagraphStyle("small", fontName="Arial", fontSize=7.5, leading=9.5, textColor=colors.HexColor("#444444"))
H1 = ParagraphStyle("h1", fontName="Arial-Bold", fontSize=14, leading=18, spaceAfter=6)
H2 = ParagraphStyle("h2", fontName="Arial-Bold", fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=4)
CELL = ParagraphStyle("cell", fontName="Arial", fontSize=8, leading=10)


def _footer(canvas, doc_) -> None:
    canvas.saveState()
    canvas.setFont("Arial", 6.5)
    canvas.setFillColor(colors.grey)
    canvas.drawCentredString(A4[0] / 2, 8 * mm, f"{SYNTH_FOOTER}  |  page {doc_.page}")
    canvas.restoreState()


def _pdf(path: Path, story, on_page=None, top=18 * mm) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=top, bottomMargin=16 * mm)

    def first(c, d):
        _footer(c, d)
        if on_page:
            on_page(c, d)

    doc.build(story, onFirstPage=first, onLaterPages=first)


def _table(rows, widths, header=True) -> Table:
    data = [[Paragraph(escape(str(c)), CELL) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
             ("VALIGN", (0, 0), (-1, -1), "TOP")]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")))
    t.setStyle(TableStyle(style))
    return t


def make_certificate(path: Path, title: str, vendor: str, cert_no: str, body: List[str],
                     valid_to: str) -> None:
    def watermark(c, d):
        c.saveState()
        c.setFillColorRGB(0.85, 0.2, 0.2)
        c.setFillAlpha(0.13)
        c.setFont("Arial-Bold", 58)
        c.translate(A4[0] / 2, A4[1] / 2)
        c.rotate(35)
        c.drawCentredString(0, 0, "SPECIMEN")
        c.restoreState()

    story = [Paragraph(escape(title), H1),
             Paragraph(f"Certificate no: <b>{escape(cert_no)}</b>", BODY),
             Paragraph(f"Issued to: <b>{escape(vendor)}</b>", BODY),
             Spacer(1, 6)]
    story += [Paragraph(escape(b), BODY) for b in body]
    story += [Spacer(1, 10),
              Paragraph(f"Valid until: <b>{valid_to}</b>", BODY),
              Paragraph("Issuing body: Demo Certification Services (fictional). "
                        "Not issued by any accredited certification body.", SMALL)]
    _pdf(path, story, on_page=watermark, top=30 * mm)


# ------------------------------------------------------------ RFx


def ev(value, locator: str, conf: Confidence = Confidence.HIGH):
    return dict(value=value, source_file="rfx/rfx.json", locator=locator, confidence=conf)


def build_rfx() -> RFx:
    items = []
    for ln in LINES:
        dims = None
        if ln.dims:
            l, w, h = ln.dims
            dims = Dimensions(length_mm=Decimal(l) if l else None,
                              width_mm=Decimal(w) if w else None,
                              height_mm=Decimal(h) if h else None)
        spec = LineItemSpec(
            ply=ExtractedValue[int](**ev(ln.ply, f"line_items[{ln.id}].ply")) if ln.ply else None,
            gsm=ExtractedValue[int](**ev(ln.gsm, f"line_items[{ln.id}].gsm")) if ln.gsm else None,
            dimensions=ExtractedValue[Dimensions](**ev(dims, f"line_items[{ln.id}].dimensions"))
            if dims else None,
            printing=ExtractedValue[str](**ev(ln.printing, f"line_items[{ln.id}].printing"))
            if ln.printing else None,
        )
        items.append(LineItem(
            id=ln.id,
            description=ExtractedValue[str](**ev(ln.desc, f"line_items[{ln.id}].description")),
            spec=spec,
            qty=ExtractedValue[Decimal](**ev(Decimal(ln.qty), f"line_items[{ln.id}].qty")),
            uom=ExtractedValue[Uom](**ev(Uom(ln.uom), f"line_items[{ln.id}].uom")),
        ))
    terms = Terms(
        payment_terms=ExtractedValue[str](**ev("45 days from invoice", "terms.payment")),
        delivery_terms=ExtractedValue[str](**ev("Within 21 days of PO, to Mumbai and Bhiwandi warehouses", "terms.delivery")),
        validity=ExtractedValue[str](**ev("60 days from RFx date", "terms.validity")),
        incoterms=ExtractedValue[Incoterm](**ev(Incoterm.DAP, "terms.incoterms")),
    )
    questionnaire = [Question(id=qid, text=text, required=True) for qid, text in QUESTIONS]
    return RFx(id=RFX_ID, title=f"Corrugated packaging supply FY 2027 - {BUYER}",
               category="corrugated_packaging", terms=terms, line_items=items,
               questionnaire=questionnaire)


# ------------------------------------------------------ canonical truth


def canon_truth(vendor: str, ln: Line) -> float:
    noise = 1 + _rng(vendor, ln.id, "price").gauss(0, 0.02)
    return round(ln.base * VENDOR_FACTOR[vendor] * noise, 2)


def prior_year_value(ln: Line) -> float:
    noise = 1 + _rng("vendor_e", ln.id, "prior").gauss(0, 0.02)
    return round(ln.base * PRIOR_YEAR_FACTOR * noise, 2)


def make_offer(vendor: str, ln: Line) -> dict:
    o = dict(line_id=ln.id, status="quoted", canonical_uom=ln.uom, stated_value=None,
             stated_text=None, stated_uom=f"per {UNIT_TXT[ln.uom]}", stated_currency="INR",
             stated_pack_size=None, canonical=None, freight=FREIGHT[vendor], discount=None,
             prior_year_reference=False, traps=[], expected_confidence="high",
             source_file=None, locator=None)

    if vendor == "vendor_c" and ln.id in C_NO_BID:
        o.update(status="no_bid", traps=["no_bid"])
        return o

    if vendor == "vendor_e":
        if ln.id in E_EMAIL_RATES:
            value = E_EMAIL_RATES[ln.id]
            o.update(stated_value=value, canonical=value, stated_uom="per kg (email)",
                     traps=["rate_applies_only_to_kg_rolls_of_this_ply"])
        else:
            value = prior_year_value(ln)
            o.update(stated_value=value, canonical=value, prior_year_reference=True,
                     expected_confidence="med",
                     traps=["same_as_last_year_resolve_via_attached_rate_sheet"])
        return o

    value = canon_truth(vendor, ln)
    o.update(stated_value=value, canonical=value)

    if vendor == "vendor_d":
        if ln.uom == "EA" and ln.id in D_PER_100:
            stated = round(value * 100, 2)
            o.update(stated_value=stated, stated_uom="per 100 pcs", stated_pack_size=100,
                     canonical=round(stated / 100, 4),
                     traps=["quoted_per_100_pcs_for_per_piece_line"])
        if ln.imported:
            usd = round(o["canonical"] / USD_INR, 2)
            usd_unit = "roll" if ln.uom == "EA" else UNIT_TXT[ln.uom]
            o.update(stated_value=usd, stated_uom=f"USD per {usd_unit}", stated_currency="USD",
                     canonical=round(usd * USD_INR, 2),
                     traps=o["traps"] + ["usd_price_convert_at_fixed_rate"])
        o["expected_confidence"] = "med"  # phone photo

    if vendor == "vendor_a" and _rng("vendor_a", ln.id, "text").random() < 0.12:
        o.update(stated_value=value, stated_text=f"{value:.2f}/-", expected_confidence="med",
                 traps=["price_typed_as_text_with_suffix"])

    if vendor == "vendor_b":
        o.update(discount="3% volume discount on annual orders above Rs 25,00,000 (footnote)",
                 traps=["discount_buried_in_footnote"])

    if vendor == "vendor_c" and ln.id in C_APPROX:
        o.update(stated_text=f"around Rs {round(value)}", expected_confidence="med",
                 traps=["approximate_price_wording"])

    return o


def build_offers() -> Dict[str, Dict[str, dict]]:
    return {v: {ln.id: make_offer(v, ln) for ln in LINES} for v in VENDOR_NAMES}


# ------------------------------------------------------ vendor renderers


def render_vendor_a(offers: dict) -> None:
    """Excel that ignores the template: merged headers, units in headers, two sheets,
    a hidden notes column, and rows in the vendor's own order."""
    out = RESP / "vendor_a"
    out.mkdir(parents=True, exist_ok=True)
    order = LINES[:]
    _rng("vendor_a", "order").shuffle(order)

    wb = Workbook()
    ws = wb.active
    ws.title = "Rates"
    ws.merge_cells("A1:I1")
    ws["A1"] = "VARDHAN PACKAGING WORKS PVT. LTD. - PRICE BID"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A2:I2")
    ws["A2"] = f"Ref: VPW/26-27/0412    Date: 12-Nov-2026    Against: {RFX_ID}"
    ws.merge_cells("A4:C4")
    ws["A4"] = "Item"
    ws.merge_cells("D4:G4")
    ws["D4"] = "Rates (INR, excl. GST)"
    ws.merge_cells("H4:I4")
    ws["H4"] = "Other"
    headers = ["Sr", "Description", "Spec", "Rate (₹/Nos)", "Rate (₹/100 Nos)", "Rate (₹/Kg)",
               "Rate (₹/Sq.m)", "GST %", "Annual value (₹)", "Internal notes"]
    for col, h in enumerate(headers, start=1):
        ws.cell(row=5, column=col, value=h).font = Font(bold=True)
    notes = ["MOQ 2000 nos", "old list 2025 ref", "check freight", "", "rate valid 30 days", ""]
    col_for = {"EA": "D", "PACK": "E", "KG": "F", "SQM": "G"}
    for i, ln in enumerate(order, start=1):
        r = 5 + i
        o = offers["vendor_a"][ln.id]
        note = notes[_rng("vendor_a", ln.id, "note").randrange(len(notes))]
        ws.cell(row=r, column=1, value=i)
        ws.cell(row=r, column=2, value=ln.desc)
        ws.cell(row=r, column=3, value=spec_str(ln))
        cell_value = o["stated_text"] or o["stated_value"]
        ws[f"{col_for[ln.uom]}{r}"] = cell_value
        ws.cell(row=r, column=8, value=12)
        ws.cell(row=r, column=9, value=round(o["canonical"] * ln.qty, 2))
        ws.cell(row=r, column=10, value=note)
        o["source_file"] = "responses/vendor_a/quote.xlsx"
        o["locator"] = f"Rates!{col_for[ln.uom]}{r}"
        o["sr"] = i
    ws.column_dimensions["J"].hidden = True
    for col, width in zip("ABCDEFGHIJ", [5, 42, 30, 14, 16, 14, 14, 8, 18, 22]):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=6, max_row=5 + len(order)):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=False)

    cs = wb.create_sheet("Compliance")
    cs["A1"] = "Compliance and questionnaire"
    cs["A1"].font = Font(bold=True, size=12)
    cs.append([])
    cs.append(["No", "Question", "Answer"])
    for c in cs[3]:
        c.font = Font(bold=True)
    for qid, text in QUESTIONS:
        ans = ANSWERS["vendor_a"][int(qid[1:]) - 1]
        cs.append([qid, text, ans])
    cs.column_dimensions["B"].width = 60
    cs.column_dimensions["C"].width = 90
    wb.save(out / "quote.xlsx")


def render_vendor_b(offers: dict) -> None:
    """Letterhead PDF: price table split over two pages, volume discount in a footnote,
    questionnaire answers in an annexure."""
    out = RESP / "vendor_b"
    rows = [(ln, offers["vendor_b"][ln.id]) for ln in LINES]

    def letterhead(c, d):
        c.saveState()
        c.setFillColor(colors.HexColor("#1F3B63"))
        c.rect(0, A4[1] - 22 * mm, A4[0], 22 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Arial-Bold", 15)
        c.drawString(18 * mm, A4[1] - 12 * mm, "BHIWANDI CORRUGATED INDUSTRIES LLP")
        c.setFont("Arial", 8)
        c.drawString(18 * mm, A4[1] - 17 * mm, "Plot 14, MIDC Industrial Area, Bhiwandi, Thane 421302  |  Manufacturers of corrugated packaging")
        c.restoreState()

    def page_rows(subset):
        data = [["Sr", "Item", "Spec", "Unit", "Rate (Rs.)*"]]
        for ln, o in subset:
            data.append([str(int(ln.id[1:])), ln.desc, spec_str(ln), UNIT_DISPLAY[ln.uom],
                         f"{o['stated_value']:,.2f}"])
        return data

    story = [Spacer(1, 4),
             Paragraph("Quotation No. BCI/Q/26/1187 &nbsp;&nbsp; Date: 12.11.2026", BODY),
             Paragraph(f"To: Procurement, {escape(BUYER)}", BODY),
             Paragraph(f"Sub: Response to {RFX_ID}, corrugated packaging", BODY),
             Spacer(1, 8),
             _table(page_rows(rows[:15]), [10 * mm, 62 * mm, 52 * mm, 22 * mm, 24 * mm]),
             PageBreak(),
             _table(page_rows(rows[15:]), [10 * mm, 62 * mm, 52 * mm, 22 * mm, 24 * mm]),
             Spacer(1, 8),
             Paragraph("* Volume discount: 3% on total annual orders above Rs. 25,00,000 across all items, "
                       "credited on invoice.", SMALL),
             Paragraph("Prices are exclusive of GST (applicable rate extra). Freight extra, at actuals from our "
                       "Bhiwandi dispatch. Validity: 30 days.", SMALL),
             PageBreak(),
             Paragraph("Annexure II: Compliance responses", H2)]
    story.append(_table([["No", "Question", "Answer"]] +
                        [[qid, text, ANSWERS["vendor_b"][i]] for i, (qid, text) in enumerate(QUESTIONS)],
                        [12 * mm, 70 * mm, 100 * mm]))
    _pdf(out / "quote_letterhead.pdf", story, on_page=letterhead, top=30 * mm)

    for ln, o in rows:
        page = 1 if int(ln.id[1:]) <= 15 else 2
        o["source_file"] = "responses/vendor_b/quote_letterhead.pdf"
        o["locator"] = f"p.{page} row Sr {int(ln.id[1:])}"
        o["sr"] = int(ln.id[1:])

    certs = {"iso_9001_2015.pdf": ("ISO 9001:2015 Certificate of Registration", "BCI-QMS-0093",
                                   ["Quality management system for manufacture of corrugated boxes and sheets."],
                                   "31-Aug-2026")}
    for name, (title, no, body, valid) in certs.items():
        make_certificate(out / "certs" / name, title, VENDOR_NAMES["vendor_b"], no, body, valid)
    make_certificate(out / "certs" / "fsc_coc.pdf", "FSC Chain of Custody Certificate",
                     VENDOR_NAMES["vendor_b"], "DEMO-FSC-B-2281",
                     ["Chain-of-custody certification for FSC-labelled corrugated packaging."], "28-Feb-2027")


def render_vendor_c(offers: dict) -> None:
    """Word doc with commercials written as prose; 27 of 30 lines quoted."""
    out = RESP / "vendor_c"
    out.mkdir(parents=True, exist_ok=True)
    doc = Document()

    def add(text: str, style: Optional[str] = None) -> int:
        """Add a paragraph and return its 1-based index among all body paragraphs
        (headings included), which is what the locator refers to."""
        doc.add_paragraph(text, style=style)
        return len(doc.paragraphs)

    doc.add_heading("Sahyadri Boxes & Sheets: Commercial Proposal", level=1)
    add("Ref SBS/PRO/2026/0309, dated 12 November 2026, in response to " + RFX_ID + ".")
    add("Dear Kiranaplus procurement team,")
    add("We are pleased to submit our rates below. All rates are in Indian Rupees, exclusive of GST, "
        "and delivered to your Mumbai warehouse, so freight is included. Our validity is 45 days from today.")
    doc.add_heading("Commercial offer", level=2)
    groups = [("Shipper and mailer boxes", [f"L{i:02d}" for i in range(1, 13)]),
              ("Dividers and inserts", ["L13", "L14"]),
              ("Corrugated sheets", ["L15", "L16", "L17", "L30"]),
              ("Corrugated rolls", ["L18", "L19", "L20"]),
              ("Edge protection", ["L21", "L22"]),
              ("Stretch film and tape", ["L23", "L24", "L25", "L26"]),
              ("Pallets and heavy boxes", ["L27", "L28", "L29"])]
    for title, ids in groups:
        doc.add_heading(title, level=3)
        for lid in ids:
            ln = LINE_BY_ID[lid]
            o = offers["vendor_c"][lid]
            unit = "per roll" if ln.imported and ln.uom == "EA" else UNIT_DISPLAY[ln.uom]
            if o["status"] == "no_bid":
                o["source_file"] = "responses/vendor_c/quote.docx"
                o["locator"] = f"para {add(f'{lid}, {ln.desc}: we regret that we are unable to quote for this item in the current cycle.')}"
                continue
            if o["stated_text"]:
                txt = f"{lid}, {ln.desc}: we offer {o['stated_text']} {unit}."
            else:
                txt = f"{lid}, {ln.desc}: we offer ₹{o['stated_value']:,.2f} {unit}."
            o["source_file"] = "responses/vendor_c/quote.docx"
            o["locator"] = f"para {add(txt)}"
            o["sr"] = int(lid[1:])
    doc.add_heading("Questionnaire responses", level=2)
    for i, (qid, text) in enumerate(QUESTIONS):
        ans = ANSWERS["vendor_c"][i]
        add(f"{qid} {text}: {ans}")
    add("Please contact our sales desk for any clarification. We look forward to your order.")
    for p in doc.paragraphs:
        for r in p.runs:
            r.font.size = Pt(10.5)
    out.mkdir(parents=True, exist_ok=True)
    doc.save(out / "quote.docx")

    make_certificate(out / "certs" / "iso_9001_2015.pdf", "ISO 9001:2015 Certificate of Registration",
                     VENDOR_NAMES["vendor_c"], "SBS-44-8810",
                     ["Quality management system for manufacture of corrugated boxes."], "31-May-2027")
    make_certificate(out / "certs" / "bis_registration.pdf", "BIS Registration Letter (synthetic)",
                     VENDOR_NAMES["vendor_c"], "SBS/BIS/0021",
                     ["Registration for corrugated fibreboard boxes, manufacturing plant at Pune."], "31-Mar-2028")
    make_certificate(out / "certs" / "external_lab_test_report.pdf", "Third-party test report: ECT and BCT",
                     VENDOR_NAMES["vendor_c"], "TR-EXT-2026-0917",
                     ["Tests performed by an external NABL-accredited laboratory (fictional) on 3-ply board.",
                      "ECT (edge crush): 6.1 kN/m. BCT: see table in report body."], "n/a")


def render_vendor_d(offers: dict) -> None:
    """Phone photo of a printed rate card: skew, rotation, blur, noise and a shadow."""
    out = RESP / "vendor_d"
    out.mkdir(parents=True, exist_ok=True)
    W, H = 1500, 2400
    paper, ink = (246, 244, 236), (25, 25, 30)
    img = Image.new("RGB", (W, H), paper)
    d = ImageDraw.Draw(img)
    d.text((110, 60), "GANESH KRAFT TRADERS", font=_font(46, True), fill=ink)
    d.text((110, 124), "Rate card, FY 2026-27 (valid 30 days)", font=_font(28), fill=ink)
    d.line((70, 175, W - 70, 175), fill=ink, width=3)
    hf = _font(24, True)
    d.text((110, 190), "No.", font=hf, fill=ink)
    d.text((200, 190), "Item", font=hf, fill=ink)
    d.text((1000, 190), "Rate (Rs.)", font=hf, fill=ink)
    d.text((1230, 190), "Unit", font=hf, fill=ink)
    d.line((70, 232, W - 70, 232), fill=ink, width=2)
    body, y = _font(23), 244
    for ln in LINES:
        o = offers["vendor_d"][ln.id]
        n = int(ln.id[1:])
        if o["stated_currency"] == "USD":
            rate = f"USD {o['stated_value']:.2f}"
            unit = "per roll" if ln.uom == "EA" else UNIT_DISPLAY[ln.uom]
        else:
            rate = f"{o['stated_value']:,.2f}"
            unit = "per 100 pcs" if o["stated_pack_size"] == 100 else UNIT_DISPLAY[ln.uom]
        d.text((110, y), f"{n:02d}", font=body, fill=ink)
        d.text((200, y), ln.desc[:60], font=body, fill=ink)
        d.text((1000, y), rate, font=body, fill=ink)
        d.text((1230, y), unit, font=body, fill=ink)
        o["source_file"] = "responses/vendor_d/rate_card_photo.jpg"
        o["locator"] = f"photo, rate card row {n:02d}"
        o["sr"] = n
        y += 44
    d.line((70, y + 6, W - 70, y + 6), fill=ink, width=2)
    y += 36
    d.text((110, y), "COMPLIANCE", font=_font(28, True), fill=ink)
    y += 48
    qf = _font(21)
    for i, (qid, _) in enumerate(QUESTIONS):
        for line in textwrap.wrap(f"{qid}: {ANSWERS['vendor_d'][i]}", 88):
            d.text((110, y), line, font=qf, fill=ink)
            y += 30
    img = _photograph(img)
    img.save(out / "rate_card_photo.jpg", quality=70)


def _perspective_coeffs(out_pts, in_pts) -> List[float]:
    A, b = [], []
    for (x, y), (u, v) in zip(out_pts, in_pts):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    return np.linalg.solve(np.array(A, float), np.array(b, float)).tolist()


def _photograph(img: Image.Image) -> Image.Image:
    """Make a clean render look like an angled phone photo taken under uneven light."""
    bg = (120, 118, 112)
    img = img.rotate(3.2, resample=Image.BICUBIC, expand=True, fillcolor=bg)
    w, h = img.size
    out_pts = [(0, 0), (w, 0), (w, h), (0, h)]
    in_pts = [(0.05 * w, 0.03 * h), (0.97 * w, 0.0), (0.99 * w, 0.98 * h), (0.0, h)]
    img = img.transform((w, h), Image.PERSPECTIVE, _perspective_coeffs(out_pts, in_pts),
                        Image.BICUBIC, fillcolor=bg)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    arr = np.asarray(img).astype(np.float32)
    gradient = 1.0 - 0.28 * np.clip(0.6 * xx / w + 0.4 * yy / h - 0.2, 0, 1)
    band = 1.0 - 0.15 * np.exp(-(((xx - 0.2 * w + 0.3 * yy) / (0.08 * w)) ** 2))
    arr *= (gradient * band)[..., None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.3))
    noise = np.random.default_rng(SEED).normal(0, 7, (h, w, 3))
    arr = np.clip(np.asarray(img).astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def render_vendor_e(offers: dict) -> None:
    """One plain-text email. Most prices say 'same as last year', resolvable only by
    combining the email with last year's rate sheet attached to it."""
    out = RESP / "vendor_e"
    (out / "attachments").mkdir(parents=True, exist_ok=True)

    body = [
        "From: Rahul Nakoda <sales@nakodapaperpacks.example>",
        "To: procurement@kiranaplus.example",
        f"Subject: Re: {RFX_ID} pricing",
        "Date: Thu, 12 Nov 2026 16:42:10 +0530",
        "",
        "Hi procurement team,",
        "",
        f"Please find our response to {RFX_ID} below.",
        "",
        "Rates for rolls (excluding GST, freight extra):",
        "- 5-ply rolls: ₹42/kg",
        "- 3-ply rolls: ₹38/kg",
        "",
        "Rest same as last year. Our FY 2025-26 rate sheet is attached.",
        "Freight is extra, billed at actuals from Bhiwandi dispatch. Validity 30 days, payment 30 days net.",
        "",
        "Answers to your questionnaire:",
    ]
    email_rates_start = body.index("- 5-ply rolls: ₹42/kg") + 1  # 1-based line of first rate line
    qa_lines: List[Tuple[int, str]] = []
    for i, (qid, _) in enumerate(QUESTIONS):
        ans = ANSWERS["vendor_e"][i]
        if ans is None:
            continue
        body.append(f"{i + 1}) {ans}")
        qa_lines.append((i, f"{i + 1}) {ans}"))
    body += ["", "Thanks and regards,", "Rahul Nakoda", "Nakoda Paper Packs", "+91 00000 00000 (placeholder)"]
    (out / "email.txt").write_text("\n".join(body) + "\n", encoding="utf-8")

    for lid in E_EMAIL_RATES:
        ln = LINE_BY_ID[lid]
        o = offers["vendor_e"][lid]
        o["source_file"] = "responses/vendor_e/email.txt"
        # Line 1-based index of the rate line for this ply.
        o["locator"] = f"email body, line {email_rates_start + (0 if lid == 'L19' else 1)}"
        o["sr"] = int(lid[1:])

    rows = [["Sr", "Item", "Unit", "Rate (Rs.) FY 2025-26"]]
    for ln in LINES:
        rows.append([str(int(ln.id[1:])), ln.desc, UNIT_DISPLAY[ln.uom], f"{prior_year_value(ln):,.2f}"])
    story = [Paragraph("Nakoda Paper Packs: Rate sheet FY 2025-26", H1),
             Paragraph("Valid 1 April 2025 to 31 March 2026. Excluding GST, freight extra.", BODY),
             Spacer(1, 8),
             _table(rows, [12 * mm, 100 * mm, 32 * mm, 40 * mm])]
    _pdf(out / "attachments" / "rate_sheet_fy2025_26.pdf", story)

    for ln in LINES:
        o = offers["vendor_e"][ln.id]
        if ln.id in E_EMAIL_RATES:
            continue
        o["source_file"] = "responses/vendor_e/attachments/rate_sheet_fy2025_26.pdf"
        o["locator"] = f"rate_sheet p.1 row {int(ln.id[1:])}"
        o["sr"] = int(ln.id[1:])


def render_all_vendors(offers: dict) -> None:
    render_vendor_a(offers)
    render_vendor_b(offers)
    render_vendor_c(offers)
    render_vendor_d(offers)
    render_vendor_e(offers)
    # Vendor certificates that the responses refer to. Vendor E attaches none.
    make_certificate(RESP / "vendor_a/certs/iso_9001_2015.pdf", "ISO 9001:2015 Certificate of Registration",
                     VENDOR_NAMES["vendor_a"], "VPW-QMS-4471",
                     ["Quality management system for corrugated packaging manufacture."], "31-Mar-2027")
    make_certificate(RESP / "vendor_a/certs/iso_14001_2015.pdf", "ISO 14001:2015 Certificate of Registration",
                     VENDOR_NAMES["vendor_a"], "VPW-EMS-2290",
                     ["Environmental management system for corrugated packaging manufacture."], "31-Mar-2027")
    make_certificate(RESP / "vendor_a/certs/fsc_coc.pdf", "FSC Chain of Custody Certificate",
                     VENDOR_NAMES["vendor_a"], "DEMO-FSC-1182",
                     ["Chain-of-custody certification for FSC-labelled corrugated packaging."], "30-Jun-2027")
    make_certificate(RESP / "vendor_d/certs/iso_9001_2015.pdf", "ISO 9001:2015 Certificate of Registration",
                     VENDOR_NAMES["vendor_d"], "GKT-QMS-7712",
                     ["Quality management system for corrugated box manufacture."], "31-Jul-2027")
    make_certificate(RESP / "vendor_d/certs/bct_test_report_inhouse.pdf", "In-house test report: BCT",
                     VENDOR_NAMES["vendor_d"], "GKT-TR-0061",
                     ["Box compression test (BCT) on 5-ply sample, in-house press."], "n/a")


# ------------------------------------------------------- ground truth


def build_ground_truth(offers: dict) -> dict:
    vendors = {}
    for v, name in VENDOR_NAMES.items():
        if v == "vendor_e":
            lines_gt = {}
            for ln in LINES:
                o = offers[v][ln.id]
                lines_gt[ln.id] = _gt_line(ln, o)
            qgt = {qid: {"expected_answer": ANSWERS[v][i],
                         "expected_confidence": "low" if i in (0, 1, 2, 3) else "med",
                         "flags": ["certificate_not_provided"] if i == 0 else
                         ["bis_number_not_confirmed"] if i == 3 else
                         ["not_answered"] if ANSWERS[v][i] is None else []}
                   for i, (qid, _) in enumerate(QUESTIONS)}
            vendors[v] = {"name": name, "hard_requirement_failed": [], "flags": [
                "iso_9001_certificate_to_follow_not_attached", "iso_14001_not_held",
                "freight_extra", "prices_from_prior_year_rate_sheet_for_most_lines"],
                "lines": lines_gt, "questionnaire": qgt}
            continue

        lines_gt = {ln.id: _gt_line(ln, offers[v][ln.id]) for ln in LINES}
        qgt = {}
        for i, (qid, _) in enumerate(QUESTIONS):
            flags, conf = [], "high"
            if v == "vendor_b" and qid == "Q01":
                flags.append("iso_9001_expired_2026-08-31")
            if v == "vendor_c" and qid == "Q04":
                flags.append("hard_requirement_failed_no_inhouse_ECT_BCT")
            if v == "vendor_d" and qid in ("Q01", "Q04"):
                conf = "med"
            qgt[qid] = {"expected_answer": ANSWERS[v][i], "expected_confidence": conf, "flags": flags}
        hard = ["Q04 in-house ECT/BCT testing"] if v == "vendor_c" else []
        flags = {"vendor_a": ["excel_ignores_template", "hidden_notes_column", "two_sheets"],
                 "vendor_b": ["iso_9001_expired_2026-08-31", "discount_in_footnote", "freight_extra"],
                 "vendor_c": ["no_bid_lines_L22_L28_L29", "prose_commercials", "hard_requirement_failed"],
                 "vendor_d": ["phone_photo_skew_noise", "per_100_pcs_lines", "usd_import_lines"]}[v]
        vendors[v] = {"name": name, "hard_requirement_failed": hard, "flags": flags,
                      "lines": lines_gt, "questionnaire": qgt}

    return {
        "_comment": ("EVALUATION ONLY. Ground truth for tests/eval. Nothing under app/ or core/ may read or "
                     "import this file; tests/eval/test_ground_truth_isolation.py enforces that. "
                     "canonical_unit_price_inr_ex_tax is INR per canonical UOM, excluding GST, with freight "
                     "NOT applied. For no_bid lines it is null. Generated by scripts/generate_dataset.py."),
        "seed": SEED,
        "rfx_id": RFX_ID,
        "usd_inr_rate_used": USD_INR,
        "vendors": vendors,
    }


def _gt_line(ln: Line, o: dict) -> dict:
    return {
        "status": o["status"],
        "canonical_uom": ln.uom,
        "canonical_unit_price_inr_ex_tax": o["canonical"],
        "stated_value": o["stated_value"],
        "stated_text": o["stated_text"],
        "stated_uom": o["stated_uom"] if o["status"] == "quoted" else None,
        "stated_currency": o["stated_currency"] if o["status"] == "quoted" else None,
        "stated_pack_size": o["stated_pack_size"],
        "source_file": o["source_file"],
        "locator": o["locator"],
        "expected_confidence": o["expected_confidence"],
        "freight_status": o["freight"],
        "discount": o["discount"],
        "prior_year_reference": o["prior_year_reference"],
        "traps": o["traps"],
    }


# ------------------------------------------------------------- main


def _reset() -> None:
    for sub in (DATA / "rfx", RESP):
        if sub.exists():
            shutil.rmtree(sub)
    (DATA / "rfx").mkdir(parents=True, exist_ok=True)
    RESP.mkdir(parents=True, exist_ok=True)


def main() -> None:
    _register_fonts()
    _reset()

    rfx = build_rfx()
    (DATA / "rfx" / "rfx.json").write_text(rfx.model_dump_json(indent=2) + "\n", encoding="utf-8")

    offers = build_offers()
    render_all_vendors(offers)

    gt = build_ground_truth(offers)
    (DATA / "ground_truth.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"RFx {RFX_ID}: {len(LINES)} line items, {len(QUESTIONS)} questions")
    print(f"Wrote data/rfx/rfx.json, data/ground_truth.json and {len(VENDOR_NAMES)} vendor folders")
    for path in sorted(p for p in DATA.rglob("*") if p.is_file() and "/.git" not in str(p)):
        print(f"  {path.relative_to(DATA)}  {path.stat().st_size:>9,} bytes")


if __name__ == "__main__":
    main()
