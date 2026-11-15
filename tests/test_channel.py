"""Simulated channel tests. Everything runs in tmp_path; the real data folders are not touched."""

import json
from email import message_from_bytes, policy
from pathlib import Path

import pytest

from core.channel import SimulatedChannel, Vendor, STATUS_NOT_SENT
from core.db import init_db
from core.rfx_copilot import blank_doc, load_doc
from core.schema import RFx

DEMO = Path(__file__).resolve().parents[1] / "data" / "rfx" / "rfx.json"


@pytest.fixture
def setup(tmp_path):
    responses = tmp_path / "responses"
    for vid, name in [("vendor_a", "quote.xlsx"), ("vendor_d", "rate_card_photo.jpg")]:
        d = responses / vid
        d.mkdir(parents=True)
        (d / name).write_bytes(b"fake-bytes-" + vid.encode())
    (responses / "vendor_a" / "certs").mkdir()
    (responses / "vendor_a" / "certs" / "iso.pdf").write_bytes(b"%PDF-1.4 fake")

    vendors = [Vendor("vendor_a", "Vardhan", "rfq@vardhan.example", "email"),
               Vendor("vendor_d", "Ganesh", "quotes@ganesh.example", "whatsapp")]
    conn = init_db(":memory:")
    ch = SimulatedChannel(conn, tmp_path, responses, vendors)
    return ch, RFx.model_validate(load_doc(DEMO)), tmp_path


def test_send_writes_one_eml_per_vendor_with_attachments(setup):
    ch, rfx, base = setup
    sent = ch.send_rfx(rfx, ["vendor_a", "vendor_d"])
    assert len(sent) == 2
    for vid in ["vendor_a", "vendor_d"]:
        eml = base / "outbox" / "sent" / rfx.id / f"{vid}.eml"
        msg = message_from_bytes(eml.read_bytes(), policy=policy.default)
        assert msg["X-Simulated"] == "true"
        assert "SIMULATED CHANNEL" in msg.get_body().get_content()
        names = [p.get_filename() for p in msg.iter_attachments()]
        assert f"{rfx.id}.pdf" in names and f"{rfx.id}.xlsx" in names
    log = [json.loads(line) for line in (base / "outbox" / "channel_log.jsonl").read_text().splitlines()]
    assert {entry["vendor_id"] for entry in log} == {"vendor_a", "vendor_d"}
    assert all(entry["simulated"] for entry in log)


def test_send_refuses_unapproved_rfx(setup):
    ch, _, _ = setup
    with pytest.raises(ValueError, match="not ready to send"):
        ch.send_rfx(RFx.model_validate(blank_doc("RFX-X")), ["vendor_a"])


def test_send_refuses_unknown_vendor(setup):
    ch, rfx, _ = setup
    with pytest.raises(ValueError, match="unknown"):
        ch.send_rfx(rfx, ["vendor_zzz"])


def test_status_progression(setup):
    ch, rfx, _ = setup
    statuses = {r["vendor_id"]: r["status"] for r in ch.vendor_status(rfx.id)}
    assert statuses == {"vendor_a": STATUS_NOT_SENT, "vendor_d": STATUS_NOT_SENT}

    ch.send_rfx(rfx, ["vendor_a", "vendor_d"])
    statuses = {r["vendor_id"]: r["status"] for r in ch.vendor_status(rfx.id)}
    assert statuses == {"vendor_a": "sent", "vendor_d": "sent"}

    ch.simulate_replies(rfx.id)
    rows = {r["vendor_id"]: r for r in ch.vendor_status(rfx.id)}
    assert rows["vendor_a"]["status"] == "received"
    assert rows["vendor_a"]["files"] == 2
    assert rows["vendor_d"]["channel"] == "whatsapp"

    ch.set_response_status(rows["vendor_a"]["response_id"], "needs_review")
    rows = {r["vendor_id"]: r for r in ch.vendor_status(rfx.id)}
    assert rows["vendor_a"]["status"] == "needs_review"
    assert rows["vendor_d"]["status"] == "received"


def test_simulate_moves_files_to_inbox_and_keeps_dataset(setup):
    ch, rfx, base = setup
    ch.send_rfx(rfx, ["vendor_a"])
    ch.simulate_replies(rfx.id)
    assert (base / "inbox" / rfx.id / "vendor_a" / "quote.xlsx").exists()
    assert (base / "inbox" / rfx.id / "vendor_a" / "certs" / "iso.pdf").exists()
    # the source responses folder is copied from, not emptied
    assert (base / "responses" / "vendor_a" / "quote.xlsx").exists()


def test_simulate_is_idempotent(setup):
    ch, rfx, _ = setup
    ch.send_rfx(rfx, ["vendor_a"])
    first = ch.simulate_replies(rfx.id)
    second = ch.simulate_replies(rfx.id)
    assert first[0]["result"] == "received"
    assert second[0]["result"].startswith("skipped")
    count = ch.conn.execute("SELECT COUNT(*) FROM response").fetchone()[0]
    assert count == 1


def test_simulate_requires_a_send_first(setup):
    ch, rfx, _ = setup
    with pytest.raises(ValueError, match="nothing has been sent"):
        ch.simulate_replies(rfx.id)


def test_invalid_status_rejected(setup):
    ch, rfx, _ = setup
    ch.send_rfx(rfx, ["vendor_a"])
    ch.simulate_replies(rfx.id)
    rid = ch.vendor_status(rfx.id)[0]["response_id"]
    with pytest.raises(ValueError, match="status must be one of"):
        ch.set_response_status(rid, "done")
