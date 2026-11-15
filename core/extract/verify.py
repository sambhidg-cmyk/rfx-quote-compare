"""Snippet verification. A value is only trusted if its quoted snippet appears in the named source.

- verified:    snippet found (whitespace and case normalised) in the source file's text dump.
- visual_only: the file has no matching text but is sent as an image, so the model read it by eye.
               Cannot be checked programmatically, so confidence is capped at med.
- not_found:   the file has text and the snippet is not in it. Treated as a fabrication attempt.
- no_source:   the named file does not exist in the vendor folder.
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Tuple

from core.extract.models import BatchOut, CommercialOut, FieldOut, LineOut, QAOut
from core.extract.readers import SourceDoc

CONF_RANK = {"low": 0, "med": 1, "high": 2}


def norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def verify_snippet(snippet: str, source_name: Optional[str], docs: Dict[str, SourceDoc]) -> str:
    doc = docs.get(source_name or "")
    if doc is None:
        return "no_source"
    if doc.text and norm(snippet) in norm(doc.text):
        return "verified"
    if doc.has_images:
        return "visual_only"
    return "not_found"


def cap_confidence(conf: str, cap: str) -> str:
    return conf if CONF_RANK[conf] <= CONF_RANK[cap] else cap


def iter_fields(out: BatchOut) -> Iterator[Tuple[str, FieldOut]]:
    """Every FieldOut in a batch, with a dotted path for reporting."""
    for line in out.lines:
        for name in ("unit_price", "price_uom", "pack_size", "currency"):
            f = getattr(line, name)
            if f is not None and f.value is not None:
                yield f"{line.line_id}.{name}", f
    if out.commercial is not None:
        for name, f in out.commercial:
            if f is not None and f.value is not None:
                yield f"commercial.{name}", f
    for qa in out.questionnaire:
        if qa.answer is not None and qa.answer.value is not None:
            yield f"{qa.question_id}.answer", qa.answer


def check_batch(out: BatchOut, docs: Dict[str, SourceDoc]) -> Tuple[List[str], List[dict]]:
    """Verify every value in a batch.

    Returns (errors, checks). Errors are snippet problems that justify a retry (no_source and
    not_found). checks records the outcome of every value, and visual_only confidence is capped
    as a side effect.
    """
    errors: List[str] = []
    checks: List[dict] = []
    for path, f in iter_fields(out):
        status = verify_snippet(f.snippet or "", f.source_file, docs)
        checks.append({"path": path, "status": status, "source_file": f.source_file,
                       "locator": f.locator, "value": f.value})
        if status in ("no_source", "not_found"):
            errors.append(f"{path}: snippet {f.snippet!r} was not found in {f.source_file!r}"
                          if status == "not_found" else f"{path}: source file {f.source_file!r} does not exist")
            if status == "not_found":
                f.confidence = "low"  # kept only if the retry also fails; always sent to review
        elif status == "visual_only":
            f.confidence = cap_confidence(f.confidence, "med")
    return errors, checks
