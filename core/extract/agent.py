"""Extraction agent: one vendor response at a time.

Every call sees all of the vendor's files, so a line that says "same as last year" can be
resolved against an attached rate sheet. Lines are sent in batches to keep each call focused;
the commercial terms and questionnaire go in a final call. The model answers only through the
record_extraction tool. Its output is validated by pydantic and every snippet is checked against
the source text. One retry with the errors is allowed. Raw output is cached on disk.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from core.extract import cache
from core.extract.models import EXTRACT_TOOL, BatchOut, CommercialOut, LineOut, QAOut
from core.extract.readers import SourceDoc, folder_digest, load_vendor_docs
from core.extract.verify import check_batch
from core.schema import RFx

PROMPT_VERSION = "extract-v2"
from core.llm import default_model  # noqa: E402

DEFAULT_MODEL = default_model()
DEFAULT_BATCH_SIZE = 10
MAX_TOKENS = 8000

SYSTEM_PROMPT = """You extract quotation data from one vendor's files for a procurement team.
You see every file the vendor sent. Text files are dumps where each line carries a locator label,
for example [Rates!E9], [p.2 line 14], [para 12], [table 1 row 3], [line 11]. Image files are shown
visually and have no text labels.

Rules:
1. Never invent. If the files do not state a value, set it to null. A line the vendor did not price,
   or marked as no bid, is status not_quoted.
2. Every non-null value needs source_file (the exact file name from the FILE header), locator (the
   label of the line it came from, without brackets) and snippet: a verbatim, single-line copy of the
   text that states the value. Copy characters exactly, including units and punctuation like "/-".
3. Report prices exactly as the vendor states them: the number, the unit it applies to (price_uom:
   EA = per piece, KG = per kg, PACK = per pack, SQM = per square metre, LOT = per lot, OTHER = none
   of these), and pack_size when the price is per pack (for example per 100 pieces: price_uom PACK,
   pack_size 100). Do not convert units or currency; that happens later.
   Unit words map as follows: piece, pc, pcs, nos, each, unit, roll, reel, box, set, and per-item wording
   are EA. "per 100 pcs", "per 100 nos" and "per hundred" are PACK with pack_size 100. Sq m, sqm and
   square metre are SQM. Kg is KG. Use OTHER only when none of these fits.
   Currency: report the currency the vendor writes, e.g. "Rs", "Rs.", "INR", "₹", "USD", "$"; these are normalised later.
4. Some prices are not stated on the line itself. A line may say "same as last year", "rest as before",
   or give a price for a whole category, such as "5-ply rolls: Rs 42/kg". Resolve these only from the
   files: look up the referenced line or category, use that value, cite that file and locator, and say
   how you resolved it in notes. If the reference is ambiguous or missing, use not_quoted and explain.
5. Confidence: high when the snippet states the value unambiguously; med when you interpreted a unit,
   a header, or a referenced sheet; low when unsure. Values read from images are med at most.
6. Commercial terms (freight, GST, validity, discount rules including footnotes, MOQ, lead time,
   payment terms) go in commercial, with the same provenance fields. A premium, surcharge, or
   "available at +4%" is not a discount: report it only under notes of the relevant line, not as discount. Questionnaire answers go in
   questionnaire with the same provenance. Use not_answered when the files do not answer a question.
7. Answer only by calling record_extraction."""


class ExtractionError(Exception):
    """A batch could not produce schema-valid output even after the retry."""


@dataclass
class Batch:
    label: str
    kind: str  # "lines" or "commercial"
    line_ids: List[str] = field(default_factory=list)


@dataclass
class BatchReport:
    label: str
    cache_hit: bool
    attempts: int
    unresolved_snippets: List[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    folder: str
    model: str
    lines: Dict[str, LineOut] = field(default_factory=dict)
    commercial: Optional[CommercialOut] = None
    questionnaire: Dict[str, QAOut] = field(default_factory=dict)
    checks: List[dict] = field(default_factory=list)
    batches: List[BatchReport] = field(default_factory=list)
    docs: Dict[str, SourceDoc] = field(default_factory=dict)
    empty: bool = False  # nothing usable in the response; no model call was made
    unreadable: List[Dict[str, str]] = field(default_factory=list)  # [{"file", "error"}]


def plan_batches(rfx: RFx, batch_size: int) -> List[Batch]:
    ids = [li.id for li in rfx.line_items]
    batches = [Batch(label=f"lines {ids[i]}..{ids[min(i + batch_size, len(ids)) - 1]}", kind="lines",
                     line_ids=ids[i:i + batch_size]) for i in range(0, len(ids), batch_size)]
    batches.append(Batch(label="commercial and questionnaire", kind="commercial"))
    return batches


def _spec_text(li) -> str:
    parts = []
    s = li.spec
    if s.ply:
        parts.append(f"{s.ply.value}-ply")
    if s.gsm:
        parts.append(f"{s.gsm.value} GSM")
    if s.dimensions and s.dimensions.value:
        d = s.dimensions.value
        dims = [str(int(x)) for x in (d.length_mm, d.width_mm, d.height_mm) if x is not None]
        parts.append("x".join(dims) + " mm")
    if s.printing:
        parts.append(f"printed ({s.printing.value})")
    return ", ".join(parts) or "-"


def _doc_blocks(docs: Sequence[SourceDoc]) -> List[dict]:
    blocks: List[dict] = []
    for d in docs:
        header = f"=== FILE: {d.name} ==="
        body = d.text
        if d.notes:
            body += "\n[reader notes] " + "; ".join(d.notes)
        if body.strip():
            blocks.append({"type": "text", "text": f"{header}\n{body}"})
        for img in d.images:
            blocks.append({"type": "text", "text": f"=== IMAGE: {img.label} (visual only) ==="})
            blocks.append(img.to_block())
    return blocks


def _instructions(batch: Batch, rfx: RFx) -> str:
    if batch.kind == "lines":
        lines = [{"line_id": li.id, "description": li.description.value, "spec": _spec_text(li),
                  "annual_qty": str(li.qty.value), "rfx_uom": li.uom.value.value}
                 for li in rfx.line_items if li.id in batch.line_ids]
        return ("Extract prices for ONLY these RFx lines. Output one entry per line_id, in this order. "
                "Leave commercial and questionnaire empty.\n"
                + json.dumps(lines, indent=1))
    questions = [{"question_id": q.id, "question": q.text} for q in rfx.questionnaire]
    return ("Extract commercial terms and questionnaire answers only. Leave lines empty.\n"
            "commercial: fill each field that the files state.\n"
            "questionnaire: one entry per question_id below.\n" + json.dumps(questions, indent=1))


def _block_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return {k: v for k, v in vars(block).items() if v is not None}


def _run_batch(client, model: str, docs_list: Sequence[SourceDoc], docs: Dict[str, SourceDoc],
               batch: Batch, rfx: RFx) -> Tuple[dict, BatchOut, int, List[str]]:
    content = _doc_blocks(docs_list) + [{"type": "text", "text": _instructions(batch, rfx)}]
    messages: List[dict] = [{"role": "user", "content": content}]
    attempt = 0
    while True:
        attempt += 1
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            tools=[EXTRACT_TOOL], tool_choice={"type": "tool", "name": EXTRACT_TOOL["name"]},
            messages=messages)
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block is None:
            raise ExtractionError(f"{batch.label}: model did not call record_extraction")
        raw = dict(tool_block.input)
        schema_error, out, snippet_errors = None, None, []
        try:
            out = BatchOut.model_validate(raw)
            snippet_errors, _ = check_batch(out, docs)
        except ValidationError as e:
            schema_error = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5])
        problems = ([f"schema: {schema_error}"] if schema_error else []) + snippet_errors
        if problems and attempt == 1:
            messages.append({"role": "assistant", "content": [_block_dict(b) for b in resp.content]})
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_block.id, "is_error": True,
                "content": ("Your record_extraction call had these problems:\n- " + "\n- ".join(problems) +
                            "\nCall record_extraction again with the complete corrected output for this "
                            "batch. Copy snippets exactly from the source text, or set the value to null.")}]})
            continue
        if schema_error:
            raise ExtractionError(f"{batch.label}: schema invalid after retry: {schema_error}")
        return raw, out, attempt, snippet_errors


def extract_response(client, folder: Path, rfx: RFx, *, model: str = DEFAULT_MODEL,
                     batch_size: int = DEFAULT_BATCH_SIZE, force: bool = False,
                     cache_dir: Path = cache.DEFAULT_CACHE_DIR,
                     progress: Optional[Callable[[int, int, str], None]] = None) -> ExtractionResult:
    folder = Path(folder)
    docs_list = load_vendor_docs(folder)
    docs = {d.name: d for d in docs_list}
    result = ExtractionResult(folder=str(folder), model=model, docs=docs)
    result.unreadable = [{"file": d.name, "error": d.error} for d in docs_list if d.error]
    usable = [d for d in docs_list if d.usable]
    if not usable:
        result.empty = True
        return result  # no model call for an empty or fully unreadable response
    docs_list = usable
    docs = {d.name: d for d in usable}
    result.docs = docs

    rfx_part = {"lines": [li.model_dump(mode="json") for li in rfx.line_items],
                "questions": [q.model_dump(mode="json") for q in rfx.questionnaire]}
    root = [folder_digest(folder), rfx_part, model, PROMPT_VERSION, batch_size]

    batches = plan_batches(rfx, batch_size)
    for i, batch in enumerate(batches, start=1):
        key = cache.make_key(root, batch.kind, batch.line_ids)
        cached = None if force else cache.load(cache_dir, key)
        if cached is not None:
            raw, attempts, out = cached["raw"], cached["attempts"], BatchOut.model_validate(cached["raw"])
            snippet_errors, _ = check_batch(out, docs)
            hit = True
        else:
            raw, out, attempts, snippet_errors = _run_batch(client, model, docs_list, docs, batch, rfx)
            cache.save(cache_dir, key, {"raw": raw, "attempts": attempts, "model": model,
                                        "prompt_version": PROMPT_VERSION, "batch": batch.label})
            hit = False
        checks = check_batch(out, docs)[1]
        result.checks.extend(checks)
        result.batches.append(BatchReport(batch.label, hit, attempts, snippet_errors))
        for line in out.lines:
            result.lines[line.line_id] = line
        if out.commercial is not None:
            result.commercial = out.commercial
        for qa in out.questionnaire:
            result.questionnaire[qa.question_id] = qa
        if progress:
            progress(i, len(batches), batch.label + (" (cached)" if hit else ""))
    return result
