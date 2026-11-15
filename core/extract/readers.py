"""File router and readers. Each file becomes a SourceDoc: a text dump where every line carries a
locator label (sheet!cell, page and line, paragraph, line number), plus any images to send to
the model's vision input.

Text dumps use the same locator conventions the dataset uses, so a model citing "Rates!E9" or
"para 12" can be checked against the source text.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from openpyxl import load_workbook
from docx import Document
from pypdf import PdfReader

TEXT_SUFFIXES = {".txt", ".eml", ".md", ".csv"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
GARBLED_RATIO = 0.02  # share of replacement / control characters that marks extracted text as garbled
MIN_PAGE_CHARS = 30  # pages with less text than this are rendered as images


@dataclass
class ImagePart:
    label: str
    media_type: str
    data: bytes

    def to_block(self) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": self.media_type,
                                            "data": base64.b64encode(self.data).decode("ascii")}}


@dataclass
class SourceDoc:
    name: str  # path relative to the vendor folder, e.g. "quote.xlsx" or "certs/iso.pdf"
    text: str = ""  # dump with locator labels, may be empty
    images: List[ImagePart] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # reader remarks, e.g. "page 2 sent as image"
    error: Optional[str] = None  # set when the file could not be read at all

    @property
    def has_images(self) -> bool:
        return bool(self.images)

    @property
    def usable(self) -> bool:
        return self.error is None and bool(self.text.strip() or self.images)


def _label_lines(prefix: str, text: str) -> List[str]:
    return [f"[{prefix} line {i}] {ln}" for i, ln in enumerate(text.splitlines(), start=1) if ln.strip()]


def read_xlsx(path: Path, name: str) -> SourceDoc:
    wb = load_workbook(path, data_only=True)
    out: List[str] = []
    for ws in wb.worksheets:
        hidden_cols = sorted(k for k, d in ws.column_dimensions.items() if d.hidden)
        hidden_rows = sorted(k for k, d in ws.row_dimensions.items() if d.hidden)
        merged = sorted(str(r) for r in ws.merged_cells.ranges)
        out.append(f"## sheet '{ws.title}' state={ws.sheet_state} merged={merged or 'none'} "
                   f"hidden_columns={hidden_cols or 'none'} hidden_rows={hidden_rows or 'none'}")
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None and cell.comment is None:
                    continue
                tag = f"{ws.title}!{cell.coordinate}"
                if cell.column_letter in hidden_cols:
                    tag += " (hidden column)"
                if cell.value is not None:
                    out.append(f"[{tag}] {cell.value}")
                if cell.comment is not None:
                    out.append(f"[{tag} comment] {cell.comment.text.strip()}")
    return SourceDoc(name=name, text="\n".join(out))


def read_pdf(path: Path, name: str) -> SourceDoc:
    import pypdfium2 as pdfium

    reader = PdfReader(str(path))
    text_lines: List[str] = []
    images: List[ImagePart] = []
    notes: List[str] = []
    pdf = None
    for page_no, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        garbled = len(raw.strip()) < MIN_PAGE_CHARS or (
            raw and sum(ch in "�\x00" for ch in raw) / len(raw) > GARBLED_RATIO)
        if garbled:
            if pdf is None:
                pdf = pdfium.PdfDocument(str(path))
            bitmap = pdf[page_no - 1].render(scale=1.6).to_pil()
            import io

            buf = io.BytesIO()
            bitmap.save(buf, format="PNG")
            images.append(ImagePart(label=f"{name} p.{page_no}", media_type="image/png", data=buf.getvalue()))
            notes.append(f"page {page_no} has no usable text layer and was sent as an image")
            continue
        text_lines += [f"[p.{page_no} line {i}] {ln}" for i, ln in enumerate(raw.splitlines(), start=1)
                       if ln.strip()]
    return SourceDoc(name=name, text="\n".join(text_lines), images=images, notes=notes)


def read_docx(path: Path, name: str) -> SourceDoc:
    doc = Document(str(path))
    lines: List[str] = []
    for idx, p in enumerate(doc.paragraphs, start=1):  # same numbering as the generator's locators
        if p.text.strip():
            lines.append(f"[para {idx}] {p.text}")
    for t_idx, table in enumerate(doc.tables, start=1):
        for r_idx, row in enumerate(table.rows, start=1):
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(f"[table {t_idx} row {r_idx}] " + " | ".join(cells))
    return SourceDoc(name=name, text="\n".join(lines))


def read_text(path: Path, name: str) -> SourceDoc:
    return SourceDoc(name=name, text="\n".join(_label_lines("line", path.read_text(encoding="utf-8"))))


def read_image(path: Path, name: str) -> SourceDoc:
    media = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    part = ImagePart(label=name, media_type=media, data=path.read_bytes())
    return SourceDoc(name=name, images=[part])


def route(path: Path, name: str) -> Optional[SourceDoc]:
    """Pick a reader by file type. Returns None for files the pipeline does not read."""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return read_xlsx(path, name)
    if suffix == ".pdf":
        return read_pdf(path, name)
    if suffix == ".docx":
        return read_docx(path, name)
    if suffix in TEXT_SUFFIXES:
        return read_text(path, name)
    if suffix in IMAGE_SUFFIXES:
        return read_image(path, name)
    return None


def load_vendor_docs(folder: Path) -> List[SourceDoc]:
    """Every readable file in the folder. A file that fails to parse is kept as an error entry, so the
    pipeline can ask the vendor for it again instead of silently dropping it."""
    docs = []
    for p in sorted(f for f in folder.rglob("*") if f.is_file()):
        name = p.relative_to(folder).as_posix()
        try:
            doc = route(p, name)
        except Exception as e:  # corrupt or password-protected file, unexpected format
            docs.append(SourceDoc(name=name, error=f"{type(e).__name__}: {str(e)[:160]}"))
            continue
        if doc is not None:
            docs.append(doc)
    return docs


def folder_digest(folder: Path) -> str:
    """Hash of every file in the folder (names and bytes), used as the cache key root."""
    h = hashlib.sha256()
    for p in sorted(f for f in folder.rglob("*") if f.is_file()):
        h.update(p.relative_to(folder).as_posix().encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()
