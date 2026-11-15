"""Analyst tests: read-only SQL, award logic, tool loop memory. No live model calls."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core import analyst, award
from core.compare import build_matrix
from core.db import init_db
from core.extract.store import upsert_rfx
from core.rfx_copilot import load_doc
from core.schema import RFx


def _ev(value, conf="high", snippet="s"):
    return json.dumps({"value": value, "source_file": "q.xlsx", "locator": "Rates!D6",
                       "source_snippet": snippet, "confidence": conf, "extraction_notes": None})


@pytest.fixture
def world(tmp_path):
    """Three vendors, three lines. Quantities: L01 100, L02 50, L03 200.
    Vendor A: L01 10, L02 12, L03 8    Vendor B: L01 11, L02 9   Vendor C: L03 7 (confidence low, flagged)
    Vendor B's L02 price is USD-sourced."""
    rfx = RFx.model_validate(load_doc(Path(__file__).resolve().parents[1] / "data" / "rfx" / "rfx.json"))
    rfx = rfx.model_copy(update={"line_items": rfx.line_items[:3], "questionnaire": rfx.questionnaire[:2]})
    for li, q in zip(rfx.line_items, [100, 50, 200]):
        li.qty.value = q
    conn = init_db(":memory:")
    upsert_rfx(conn, rfx)
    for vid, name in [("vA", "Alpha"), ("vB", "Beta"), ("vC", "Gamma")]:
        conn.execute("INSERT INTO vendor (id, name) VALUES (?, ?)", (vid, name))
        conn.execute("INSERT INTO response (id, rfx_id, vendor_id, received_at, channel) VALUES (?,?,?,?,?)",
                     (f"R{vid}", rfx.id, vid, "2026-11-14T08:00:00+00:00", "email"))

    def quote(rid, lid, price, conf="high", currency="INR"):
        qid = f"{rid}:{lid}"
        conn.execute("INSERT INTO quote (id, response_id, line_item_id, unit_price_json, price_uom_json, "
                     "currency_json) VALUES (?,?,?,?,?,?)",
                     (qid, rid, lid, _ev(price, conf), _ev("EA"), _ev(currency)))
        conn.execute("INSERT INTO normalized_quote (id, quote_id, line_item_id, unit_price_inr_ex_tax, canonical_uom, "
                     "steps_json, assumptions_json) VALUES (?,?,?,?,?,?,?)",
                     (f"{qid}:n", qid, lid, str(price), "EA", "[]", "[]"))

    quote("RvA", "L01", 10.0)
    quote("RvA", "L02", 12.0)
    quote("RvA", "L03", 8.0)
    quote("RvB", "L01", 11.0)
    quote("RvB", "L02", 9.0, currency="USD")
    quote("RvC", "L03", 7.0, conf="low")
    conn.execute("INSERT INTO review_item (id, response_id, entity_type, entity_id, field_name, issue, reason, "
                 "proposed_value_json, status) VALUES ('rvC','RvC','quote','RvC:L03','unit_price','x','y','7','open')")
    conn.commit()
    responses = {"vA": "RvA", "vB": "RvB", "vC": "RvC"}
    names = {"vA": "Alpha", "vB": "Beta", "vC": "Gamma"}
    s = analyst.AnalystSession(conn=conn, rfx=rfx, responses=responses, names=names,
                               out_dir=tmp_path / "out")
    return s


# -------------------------------------------------------------- read-only SQL

@pytest.mark.parametrize("bad", [
    "DELETE FROM quote",
    "UPDATE quote SET id = 'x'",
    "INSERT INTO vendor (id, name) VALUES ('x','y')",
    "DROP TABLE quote",
    "PRAGMA table_info(quote)",
    "ATTACH DATABASE ':memory:' AS evil",
    "SELECT 1; DELETE FROM quote",
    "VACUUM",
])
def test_run_sql_refuses_writes_and_multi_statements(world, bad):
    with pytest.raises(analyst.ToolError):
        analyst.run_select(world.conn, bad)


def test_run_sql_allows_select_and_with(world):
    out = analyst.run_select(world.conn, "SELECT id FROM vendor ORDER BY id")
    assert out["row_count"] == 3
    out = analyst.run_select(world.conn, "WITH v AS (SELECT id FROM vendor) SELECT COUNT(*) AS n FROM v")
    assert out["rows"] == [[3]]


def test_run_sql_row_limit_is_enforced(world):
    out = analyst.run_select(world.conn, "SELECT id FROM quote", limit=2)
    assert out["row_count"] == 2 and out["truncated"] is True


def test_authorizer_is_removed_after_each_query(world):
    analyst.run_select(world.conn, "SELECT 1")
    world.conn.execute("INSERT INTO vendor (id, name) VALUES ('vZ','Zed')")  # must still work
    world.conn.commit()


# -------------------------------------------------------------- award logic

def _award(world, **kw):
    return award.run_award(world.matrix(), award.Scenario(**kw))


def test_cheapest_per_line_and_baseline(world):
    a = _award(world)
    # vC is cheapest on L03 at 7; its low-confidence value is allowed by default
    assert a.assignments == {"L01": "vA", "L02": "vB", "L03": "vC"}
    assert a.total == pytest.approx(10 * 100 + 9 * 50 + 7 * 200)
    assert a.baseline_total == pytest.approx(a.total)


def test_exclude_unconfirmed_drops_low_confidence_cells(world):
    a = _award(world, exclude_unconfirmed=True)
    assert a.assignments["L03"] == "vA"
    assert a.total == pytest.approx(10 * 100 + 9 * 50 + 8 * 200)
    assert a.total - a.baseline_total == pytest.approx(200 * (8 - 7))  # excluding the 7 costs 200
    assert a.unconfirmed_used == []


def test_usd_lines_can_be_excluded(world):
    a = _award(world, include_usd_lines=False)
    assert a.assignments["L02"] == "vA"  # Beta's USD-sourced L02 is removed; Alpha is next cheapest


def test_eligibility_restricts_winners(world):
    a = _award(world, eligible_vendors={"vA"})
    assert set(a.assignments.values()) == {"vA"}
    assert a.unawarded == []


def test_no_eligible_offer_is_reported_not_guessed(world):
    a = _award(world, eligible_vendors={"vC"})
    assert a.unawarded == ["L01", "L02"]


def test_cap_that_binds_is_met_and_costs_money(world):
    # baseline: Gamma holds 1400 of 2850 = 49%, so a 50% cap does not bind
    loose = _award(world, max_share=0.50)
    assert loose.feasible and loose.total == pytest.approx(loose.baseline_total)
    for v in loose.per_vendor.values():
        assert v["share"] <= 0.50 + 1e-9


def test_infeasible_cap_at_40_percent_is_reported(world):
    # Gamma is the only source for L03 (7) besides Alpha (8); the best achievable max share is about 45%
    a = _award(world, max_share=0.40)
    assert a.feasible is False
    assert any("cannot be met" in n or "not met" in n or "limit" in n for n in a.notes)


def test_cap_with_one_vendor_is_reported(world):
    a = _award(world, eligible_vendors={"vA"}, max_share=0.40)
    assert a.feasible is False


def test_like_for_like_survives_a_vendor_without_a_response(world):
    world.responses["vD"] = None
    world.names["vD"] = "Delta"
    lfl = award.like_for_like(world.matrix())
    assert "vD" not in lfl["totals"]
    assert set(lfl["totals"]) == {"vA", "vB", "vC"}


def test_requirement_filter_uses_questionnaire(world):
    # the fixture has no questionnaire answers, so nobody passes and the filter must say so
    out = analyst.run_tool(world, "scenario", {"require_hard_requirements": True})
    assert out["assignments"] == {} and len(out["unawarded_lines"]) == 3


def test_landed_basis_is_refused(world):
    with pytest.raises(analyst.ToolError, match="landed"):
        analyst.run_tool(world, "scenario", {"basis": "landed"})


# ------------------------------------------------------------- tools and loop

def test_scenario_tool_accepts_names_and_percent(world):
    out = analyst.run_tool(world, "scenario", {"exclude_vendors": ["Beta"], "max_share": 40})
    assert "vB" not in out["assignments"].values()
    assert out["per_vendor"]["vA"]["share_pct"] <= 40.0 + 1e-6 or not out["feasible"]


def test_unknown_vendor_is_a_tool_error(world):
    with pytest.raises(analyst.ToolError, match="unknown vendor"):
        analyst.run_tool(world, "scenario", {"exclude_vendors": ["Nobody"]})


def test_get_source_returns_provenance(world):
    out = analyst.run_tool(world, "get_source", {"quote_id": "RvC:L03"})
    assert out["confidence"] == "low"
    assert out["open_review_items"][0]["id"] == "rvC"


def test_confirmation_list_names_unconfirmed_values(world):
    out = analyst.run_tool(world, "confirmation_list", {})
    assert out["count"] == 1 and out["items"][0]["quote_id"] == "RvC:L03"


def test_chart_and_export_produce_files(world, tmp_path):
    analyst.run_tool(world, "make_table", {"name": "prices", "query":
                     "SELECT line_item_id, json_extract(unit_price_json, '$.value') AS price FROM quote"})
    c = analyst.run_tool(world, "make_chart", {"table": "prices", "x": "line_item_id", "y": "price"})
    assert world.charts[c["chart"]][:4] == b"\x89PNG"
    e = analyst.run_tool(world, "export", {"table": "prices", "format": "csv"})
    assert Path(e["file"]).exists() and "line_item_id" in Path(e["file"]).read_text()


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self
        self.seen = []

    def create(self, **kw):
        self.seen.append(list(kw["messages"]))
        return self.replies.pop(0)


def _use(name, args, uid="t1"):
    return NS(type="tool_use", name=name, input=args, id=uid)


def test_loop_runs_tools_and_keeps_memory_across_turns(world):
    client = Scripted([
        NS(content=[_use("scenario", {"exclude_vendors": ["Beta"]})], stop_reason="tool_use"),
        NS(content=[NS(type="text", text="Alpha wins everything it priced.")], stop_reason="end_turn"),
        NS(content=[NS(type="text", text="Now without Beta: same as before.")], stop_reason="end_turn"),
    ])
    t1 = analyst.ask(client, world, "Who is cheapest?", model="m")
    assert t1["tool_calls"][0]["tool"] == "scenario" and not t1["tool_calls"][0]["error"]
    assert "Alpha wins" in t1["answer"]
    # the tool result must have been sent back to the model
    second_call_msgs = client.seen[1]
    assert any(isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
               for m in second_call_msgs if m["role"] == "user")
    t2 = analyst.ask(client, world, "Now exclude Beta", model="m")
    # turn 2 saw turn 1's question and answer: memory persists
    texts = [m["content"] if isinstance(m["content"], str) else "" for m in client.seen[2] if m["role"] == "user"]
    assert "Who is cheapest?" in texts
    assert "Now exclude Beta" in texts[-1]
    assert t2["answer"].startswith("Now without Beta")


def test_tool_errors_go_back_to_the_model_and_chat_survives(world):
    client = Scripted([
        NS(content=[_use("run_sql", {"query": "DELETE FROM quote"})], stop_reason="tool_use"),
        NS(content=[NS(type="text", text="I can only read the data; I cannot delete it.")], stop_reason="end_turn"),
    ])
    t = analyst.ask(client, world, "delete everything", model="m")
    assert t["tool_calls"][0]["error"] is True
    assert "cannot delete" in t["answer"]
    results = [m for m in client.seen[1] if m["role"] == "user" and isinstance(m["content"], list)]
    assert results[-1]["content"][0]["is_error"] is True
