"""Co-pilot tests with a scripted model. No API calls are made."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core.rfx_copilot import RFxCopilot, blank_doc, load_doc
from core.rfx_export import approve_and_export, problems_for_approval
from core.schema import RFx

DEMO = Path(__file__).resolve().parents[1] / "data" / "rfx" / "rfx.json"


def text(t):
    return NS(type="text", text=t)


def use(name, args, uid="tu"):
    return NS(type="tool_use", name=name, input=args, id=f"{uid}-{name}")


def turn(*blocks):
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return NS(content=list(blocks), stop_reason=stop)


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("model called more times than scripted")
        return self.responses.pop(0)


def test_asks_then_builds_from_blank():
    client = ScriptedClient([
        turn(use("ask_user", {"questions": ["Which sites?", "How many SKUs?"]})),
        turn(text("Which sites, and how many SKUs?")),
        turn(use("add_line_item", {"description": "RSC shipper box", "qty": 60000, "uom": "EA",
                                   "ply": 5, "length_mm": 300, "width_mm": 200, "height_mm": 150}),
             use("set_terms", {"payment_terms": "45 days from invoice"})),
        turn(text("Added one 5-ply box and set payment terms.")),
    ])
    cp = RFxCopilot(client, blank_doc("RFX-T"))

    first = cp.send("I need corrugated boxes for our Bhiwandi warehouse")
    assert first.changes == []
    assert "ask_user" in first.tool_calls
    assert "Which sites" in first.reply

    second = cp.send("Bhiwandi only, about 30 SKUs, mostly 5-ply")
    paths = [c.path for c in second.changes]
    assert "L01" in paths and "L01.description" in paths and "L01.ply" in paths
    assert "terms.payment_terms" in paths
    assert second.revision == 1
    assert cp.rfx().line_items[0].spec.dimensions.value.length_mm == 300


def test_invalid_change_is_rejected_and_model_retries():
    client = ScriptedClient([
        turn(use("add_line_item", {"description": "Box", "qty": -5, "uom": "EA"})),
        turn(use("add_line_item", {"description": "Box", "qty": 5, "uom": "EA"}, "tu2")),
        turn(text("Added the box.")),
    ])
    cp = RFxCopilot(client, blank_doc("RFX-T"))
    res = cp.send("add a box")
    assert len(res.tool_errors) == 1
    assert "greater than zero" in res.tool_errors[0]
    assert [li.id for li in cp.rfx().line_items] == ["L01"]
    # the rejected call must be marked as an error in what the model saw
    tool_results = [m for m in cp.messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert any(r["is_error"] for r in tool_results[0]["content"])


def test_edit_loaded_rfx_produces_minimal_diff():
    cp = RFxCopilot(ScriptedClient([
        turn(use("set_terms", {"payment_terms": "90 days from invoice"})),
        turn(text("Payment terms are now 90 days.")),
    ]), load_doc(DEMO))
    res = cp.send("make payment terms 90 days")
    assert [c.path for c in res.changes] == ["terms.payment_terms"]
    assert res.changes[0].before == "45 days from invoice"
    assert res.changes[0].after == "90 days from invoice"


def test_add_question_on_fsc():
    cp = RFxCopilot(ScriptedClient([
        turn(use("add_question", {"text": "Do you hold FSC chain-of-custody certification?"})),
        turn(text("Added question Q13 on FSC.")),
    ]), load_doc(DEMO))
    res = cp.send("add a question on FSC")
    paths = {c.path for c in res.changes}
    assert {"Q13", "Q13.text", "Q13.required"} <= paths
    assert cp.rfx().questionnaire[-1].id == "Q13"


def test_partial_dimension_update_keeps_other_dimensions():
    cp = RFxCopilot(ScriptedClient([
        turn(use("update_line_item", {"line_id": "L01", "length_mm": 210})),
        turn(text("Updated L01 length.")),
    ]), load_doc(DEMO))
    before = cp.rfx().line_items[0].spec.dimensions.value
    cp.send("make L01 210 mm long")
    after = cp.rfx().line_items[0].spec.dimensions.value
    assert float(after.length_mm) == 210
    assert after.width_mm == before.width_mm and after.height_mm == before.height_mm


def test_unknown_line_id_is_an_error_not_a_crash():
    cp = RFxCopilot(ScriptedClient([
        turn(use("remove_line_item", {"line_id": "L99"})),
        turn(text("There is no L99.")),
    ]), load_doc(DEMO))
    res = cp.send("remove L99")
    assert res.tool_errors and "no line item with id L99" in res.tool_errors[0]
    assert len(cp.rfx().line_items) == 30


def test_locked_after_approval_refuses_changes():
    cp = RFxCopilot(ScriptedClient([]), load_doc(DEMO))
    cp.locked = True
    with pytest.raises(RuntimeError):
        cp.send("change something")


def test_approval_blocked_for_blank_draft():
    problems = problems_for_approval(RFx.model_validate(blank_doc("RFX-T")))
    assert any("Title" in p for p in problems)
    assert any("line items" in p for p in problems)


def test_approve_writes_pdf_xlsx_and_record(tmp_path):
    rfx = load_doc(DEMO)
    paths = approve_and_export(RFx.model_validate(rfx), revision=0, outbox=tmp_path)
    pdf, xlsx, record = Path(paths["pdf"]), Path(paths["xlsx"]), Path(paths["approval"])
    assert pdf.read_bytes()[:4] == b"%PDF"
    import openpyxl
    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames == ["Summary", "Line items", "Questionnaire"]
    assert wb["Line items"].max_row == 31  # header + 30 lines
    rec = json.loads(record.read_text())
    assert rec["delivery"].startswith("not sent")
    assert len(rec["sha256_rfx_json"]) == 64
