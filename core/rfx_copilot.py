"""Conversational RFx builder (the co-pilot).

The buyer describes the need in plain words. The model either asks for missing
information (ask_user) or edits the RFx through typed tools.

The model never writes the RFx directly. Each tool call is applied to a copy of the
RFx, validated against core.schema.RFx, and only then committed. A failed call returns
an error to the model, which can retry. The buyer sees a diff of every committed change.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from core.schema import RFx

MODEL = os.environ.get("COPILOT_MODEL", "claude-sonnet-5-5")
MAX_ROUNDS = 8
UOMS = ["EA", "KG", "PACK", "SQM"]
INCOTERMS = ["EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"]
_MISSING = object()

SYSTEM_PROMPT = """You are the RFx co-pilot for a buyer's corrugated packaging procurement. \
The buyer describes what they need in plain words. Your job is to turn that into a complete \
RFx by calling tools.

How to work:
- If key information is missing, ask first with ask_user (at most 3 short questions per call). \
Essentials: scope and sites, the item list or number of SKUs, per-item dimensions, ply, GSM and \
printing where relevant, annual quantities, commercial terms, and questionnaire topics. \
Do not invent quantities or specifications; ask for them.
- When you have enough detail, create or change items with the tools. Change only what the buyer \
asked for. Do not remove or rewrite existing items unless asked.
- Units: EA = per piece, KG = per kg, PACK = per 100 pieces, SQM = per square metre.
- If you make an assumption to fill a gap, say so in your reply so the buyer can correct it.
- Keep replies short: after changes, summarise what changed in one to three sentences.
- The current RFx is given below as JSON. Treat it as the source of truth.
"""


class ToolError(Exception):
    """Raised by a tool handler when the requested change is invalid."""


@dataclass
class Change:
    path: str
    before: Any
    after: Any

    def to_row(self) -> Dict[str, Any]:
        return {"path": self.path, "before": self.before, "after": self.after}


@dataclass
class TurnResult:
    reply: str
    changes: List[Change]
    tool_calls: List[str] = field(default_factory=list)
    tool_errors: List[str] = field(default_factory=list)
    revision: int = 0


# --------------------------------------------------------------------- tools

_PROPS_LINE = {
    "description": {"type": "string"},
    "qty": {"type": "number", "exclusiveMinimum": 0},
    "uom": {"type": "string", "enum": UOMS},
    "ply": {"type": "integer", "minimum": 1, "maximum": 13},
    "gsm": {"type": "integer", "minimum": 1},
    "length_mm": {"type": "number", "exclusiveMinimum": 0},
    "width_mm": {"type": "number", "exclusiveMinimum": 0},
    "height_mm": {"type": "number", "exclusiveMinimum": 0},
    "printing": {"type": "string", "description": "e.g. '2-colour flexo', or 'plain'"},
}

TOOLS: List[Dict[str, Any]] = [
    {"name": "ask_user",
     "description": "Ask the buyer short questions about missing information. Use at most 3 per call.",
     "input_schema": {"type": "object", "properties": {
         "questions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}},
         "required": ["questions"]}},
    {"name": "set_header",
     "description": "Set the RFx title and/or scope statement.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"}, "scope": {"type": "string"}}}},
    {"name": "set_terms",
     "description": "Set commercial terms. Pass only the terms to change. Validity is free text.",
     "input_schema": {"type": "object", "properties": {
         "payment_terms": {"type": "string"},
         "delivery_terms": {"type": "string"},
         "validity": {"type": "string"},
         "incoterms": {"type": "string", "enum": INCOTERMS}}}},
    {"name": "add_line_item",
     "description": "Add a line item. Returns its new id.",
     "input_schema": {"type": "object", "properties": _PROPS_LINE,
                      "required": ["description", "qty", "uom"]}},
    {"name": "update_line_item",
     "description": "Change fields of an existing line item. Pass only the fields to change.",
     "input_schema": {"type": "object",
                      "properties": {"line_id": {"type": "string"}, **_PROPS_LINE},
                      "required": ["line_id"]}},
    {"name": "remove_line_item",
     "description": "Remove a line item by id.",
     "input_schema": {"type": "object", "properties": {"line_id": {"type": "string"}},
                      "required": ["line_id"]}},
    {"name": "add_question",
     "description": "Add a questionnaire question for vendors. Returns its new id.",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string"}, "required": {"type": "boolean"}},
         "required": ["text"]}},
    {"name": "update_question",
     "description": "Change the text or required flag of a question.",
     "input_schema": {"type": "object", "properties": {
         "question_id": {"type": "string"}, "text": {"type": "string"},
         "required": {"type": "boolean"}},
         "required": ["question_id"]}},
    {"name": "remove_question",
     "description": "Remove a questionnaire question by id.",
     "input_schema": {"type": "object", "properties": {"question_id": {"type": "string"}},
                      "required": ["question_id"]}},
]


def _ev(value: Any, turn: int, locator: str) -> Dict[str, Any]:
    """Wrap a buyer-entered value with provenance, matching core.schema.ExtractedValue."""
    return {"value": value, "source_file": f"buyer_chat/turn-{turn}", "locator": locator,
            "confidence": "high", "source_snippet": None,
            "extraction_notes": "entered via RFx co-pilot"}


def _next_id(items: List[Dict[str, Any]], prefix: str) -> str:
    nums = [int(i["id"][len(prefix):]) for i in items
            if i["id"].startswith(prefix) and i["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:02d}"


def _find(items: List[Dict[str, Any]], item_id: str, kind: str) -> Dict[str, Any]:
    for it in items:
        if it["id"] == item_id:
            return it
    raise ToolError(f"no {kind} with id {item_id}")


def _positive(qty: Any) -> Decimal:
    try:
        value = Decimal(str(qty))
    except InvalidOperation as e:
        raise ToolError(f"qty is not a number: {qty!r}") from e
    if value <= 0:
        raise ToolError("qty must be greater than zero")
    return value


def _h_set_header(cand, a, turn):
    if not any(k in a for k in ("title", "scope")):
        raise ToolError("nothing to change: pass title and/or scope")
    if "title" in a:
        cand["title"] = a["title"]
    if "scope" in a:
        cand["scope"] = a["scope"]
    return "Header updated."


def _h_set_terms(cand, a, turn):
    keys = [k for k in ("payment_terms", "delivery_terms", "validity", "incoterms") if a.get(k) is not None]
    if not keys:
        raise ToolError("nothing to change: pass at least one term")
    for k in keys:
        cand["terms"][k] = _ev(a[k], turn, f"terms.{k}")
    return "Terms updated: " + ", ".join(f"{k} = {a[k]}" for k in keys) + "."


def _dims(a: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = {k: a[k] for k in ("length_mm", "width_mm", "height_mm") if a.get(k) is not None}
    return d or None


def _h_add_line_item(cand, a, turn):
    qty = _positive(a["qty"])
    lid = _next_id(cand["line_items"], "L")
    spec: Dict[str, Any] = {}
    for k in ("ply", "gsm", "printing"):
        if a.get(k) is not None:
            spec[k] = _ev(a[k], turn, f"{lid}.spec.{k}")
    dims = _dims(a)
    if dims:
        spec["dimensions"] = _ev(dims, turn, f"{lid}.spec.dimensions")
    cand["line_items"].append({
        "id": lid,
        "description": _ev(a["description"], turn, f"{lid}.description"),
        "spec": spec,
        "qty": _ev(str(qty), turn, f"{lid}.qty"),
        "uom": _ev(a["uom"], turn, f"{lid}.uom"),
    })
    return f"Added {lid}: {a['description']}, {qty} {a['uom']}."


def _h_update_line_item(cand, a, turn):
    li = _find(cand["line_items"], a["line_id"], "line item")
    lid = li["id"]
    changed = []
    for k in ("description", "uom"):
        if k in a:
            li[k] = _ev(a[k], turn, f"{lid}.{k}")
            changed.append(k)
    if "qty" in a:
        li["qty"] = _ev(str(_positive(a["qty"])), turn, f"{lid}.qty")
        changed.append("qty")
    for k in ("ply", "gsm", "printing"):
        if k in a:
            li["spec"][k] = _ev(a[k], turn, f"{lid}.spec.{k}") if a[k] is not None else None
            changed.append(k)
    dims_in = {k: a[k] for k in ("length_mm", "width_mm", "height_mm") if k in a}
    if dims_in:
        current = dict((li["spec"].get("dimensions") or {}).get("value") or {})
        for k, v in dims_in.items():
            current[k] = v
        li["spec"]["dimensions"] = _ev({k: v for k, v in current.items() if v is not None},
                                       turn, f"{lid}.spec.dimensions")
        changed.append("dimensions")
    if not changed:
        raise ToolError("nothing to change")
    return f"Updated {lid}: {', '.join(changed)}."


def _h_remove_line_item(cand, a, turn):
    _find(cand["line_items"], a["line_id"], "line item")
    cand["line_items"] = [li for li in cand["line_items"] if li["id"] != a["line_id"]]
    return f"Removed {a['line_id']}."


def _h_add_question(cand, a, turn):
    qid = _next_id(cand["questionnaire"], "Q")
    cand["questionnaire"].append({"id": qid, "text": a["text"], "required": bool(a.get("required", True))})
    return f"Added {qid}: {a['text']}"


def _h_update_question(cand, a, turn):
    q = _find(cand["questionnaire"], a["question_id"], "question")
    if "text" not in a and "required" not in a:
        raise ToolError("nothing to change")
    if "text" in a:
        q["text"] = a["text"]
    if "required" in a:
        q["required"] = bool(a["required"])
    return f"Updated {q['id']}."


def _h_remove_question(cand, a, turn):
    _find(cand["questionnaire"], a["question_id"], "question")
    cand["questionnaire"] = [q for q in cand["questionnaire"] if q["id"] != a["question_id"]]
    return f"Removed {a['question_id']}."


HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any], int], str]] = {
    "set_header": _h_set_header,
    "set_terms": _h_set_terms,
    "add_line_item": _h_add_line_item,
    "update_line_item": _h_update_line_item,
    "remove_line_item": _h_remove_line_item,
    "add_question": _h_add_question,
    "update_question": _h_update_question,
    "remove_question": _h_remove_question,
}


# ---------------------------------------------------------- flatten and diff


def _unwrap(v: Any) -> Any:
    if isinstance(v, dict) and "value" in v and "confidence" in v:
        return v["value"]
    return v


def flatten(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Plain key/value view of the RFx for diffing. Presence markers (e.g. 'L31' = 'line')
    make added and removed items show up as one row each."""
    out: Dict[str, Any] = {"title": doc.get("title"), "scope": doc.get("scope"),
                           "category": doc.get("category")}
    for k, v in (doc.get("terms") or {}).items():
        out[f"terms.{k}"] = _unwrap(v)
    for li in doc.get("line_items", []):
        base = li["id"]
        out[base] = "line item"
        out[f"{base}.description"] = _unwrap(li["description"])
        out[f"{base}.qty"] = _unwrap(li["qty"])
        out[f"{base}.uom"] = _unwrap(li["uom"])
        for k, v in (li.get("spec") or {}).items():
            val = _unwrap(v) if v else None
            if isinstance(val, dict):
                for dk, dv in val.items():
                    out[f"{base}.{k}.{dk}"] = dv
            else:
                out[f"{base}.{k}"] = val
    for q in doc.get("questionnaire", []):
        out[q["id"]] = "question"
        out[f"{q['id']}.text"] = q["text"]
        out[f"{q['id']}.required"] = q["required"]
    return out


def diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[Change]:
    changes = []
    for key in sorted(set(before) | set(after)):
        b = before.get(key, _MISSING)
        a = after.get(key, _MISSING)
        if b != a:
            changes.append(Change(key, None if b is _MISSING else b, None if a is _MISSING else a))
    return changes


def summarize(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": doc.get("title"),
        "scope": doc.get("scope"),
        "terms": {k: _unwrap(v) for k, v in (doc.get("terms") or {}).items() if v},
        "line_items": [{
            "id": li["id"],
            "description": _unwrap(li["description"]),
            "qty": _unwrap(li["qty"]),
            "uom": _unwrap(li["uom"]),
            "spec": {k: _unwrap(v) for k, v in li["spec"].items() if v},
        } for li in doc.get("line_items", [])],
        "questionnaire": doc.get("questionnaire", []),
    }


def _to_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return {k: v for k, v in vars(block).items() if v is not None}


def _short_validation_error(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(p) for p in err["loc"])
    return f"Change rejected by schema validation at {loc}: {err['msg']}"


# ------------------------------------------------------------------ copilot


def blank_doc(rfx_id: Optional[str] = None) -> Dict[str, Any]:
    return {"id": rfx_id or f"RFX-{datetime.now():%Y}-DRAFT-{uuid.uuid4().hex[:6]}",
            "title": "", "scope": "", "category": "corrugated_packaging",
            "terms": {}, "line_items": [], "questionnaire": []}


def load_doc(path: Path) -> Dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    RFx.model_validate(doc)  # fail early if the file is not a valid RFx
    return doc


class RFxCopilot:
    def __init__(self, client: Any, doc: Optional[Dict[str, Any]] = None, model: str = MODEL):
        self.client = client
        self.model = model
        self.doc = copy.deepcopy(doc) if doc is not None else blank_doc()
        RFx.model_validate(self.doc)
        self.messages: List[Dict[str, Any]] = []
        self.turn = 0
        self.revision = 0
        self.locked = False
        self.log: List[Dict[str, Any]] = []

    def rfx(self) -> RFx:
        return RFx.model_validate(self.doc)

    def _system(self) -> str:
        return SYSTEM_PROMPT + "\nCurrent RFx:\n" + json.dumps(summarize(self.doc), indent=1, default=str)

    def _apply(self, name: str, args: Dict[str, Any]) -> str:
        if name == "ask_user":
            return "Delivered to the buyer. Wait for their reply before making changes that depend on it."
        handler = HANDLERS.get(name)
        if handler is None:
            raise ToolError(f"unknown tool {name}")
        cand = copy.deepcopy(self.doc)
        message = handler(cand, args, self.turn)
        try:
            RFx.model_validate(cand)
        except ValidationError as e:
            raise ToolError(_short_validation_error(e)) from e
        self.doc = cand
        return message

    def send(self, text: str) -> TurnResult:
        if self.locked:
            raise RuntimeError("This RFx is approved and locked. Start a new draft to make changes.")
        self.turn += 1
        before = flatten(self.doc)
        self.messages.append({"role": "user", "content": text})

        tool_calls: List[str] = []
        errors: List[str] = []
        reply_parts: List[str] = []
        finished = False
        for _ in range(MAX_ROUNDS):
            resp = self.client.messages.create(
                model=self.model, max_tokens=2048, system=self._system(),
                tools=TOOLS, messages=self.messages)
            blocks = list(resp.content)
            self.messages.append({"role": "assistant", "content": [_to_dict(b) for b in blocks]})
            reply_parts += [b.text for b in blocks if b.type == "text"]
            uses = [b for b in blocks if b.type == "tool_use"]
            if not uses:
                finished = True
                break
            results = []
            for u in uses:
                tool_calls.append(u.name)
                try:
                    out, is_error = self._apply(u.name, dict(u.input)), False
                except ToolError as e:
                    out, is_error = str(e), True
                    errors.append(f"{u.name}: {e}")
                results.append({"type": "tool_result", "tool_use_id": u.id,
                                "content": out, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
        if not finished:
            reply_parts.append("(stopped after too many tool rounds; please rephrase or split the request)")

        changes = diff(before, flatten(self.doc))
        if changes:
            self.revision += 1
        reply = "\n".join(p for p in reply_parts if p).strip()
        self.log.append({"turn": self.turn, "user": text, "reply": reply,
                         "changes": [c.to_row() for c in changes], "tool_calls": tool_calls,
                         "tool_errors": errors})
        return TurnResult(reply=reply, changes=changes, tool_calls=tool_calls,
                          tool_errors=errors, revision=self.revision)
