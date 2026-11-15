"""SIMULATED email channel. Nothing here talks to SMTP, IMAP, WhatsApp or any network service.

Outbound: ``send_rfx`` writes one .eml file per vendor, with the RFx PDF and Excel attached,
to ``data/outbox/sent/<rfx_id>/``. Each send is recorded in the ``outbound_message`` table
and appended to ``data/outbox/channel_log.jsonl``.

Inbound: ``simulate_replies`` copies each vendor's files from ``data/responses/<vendor>/``
into ``data/inbox/<rfx_id>/<vendor>/`` and registers one unprocessed ``response`` per vendor
with status ``received``. The copy keeps the generated dataset intact, so a demo can be
replayed. A vendor can be simulated once per RFx.

Status per vendor, derived from the database:
  not sent -> sent -> received -> processing -> extracted -> needs_review
Only ``sent`` and ``received`` are reached by this module. The later states are set through
``set_response_status`` by the extraction and review steps, which do not exist yet.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from core.db import RESPONSE_STATUSES, init_db
from core.rfx_export import problems_for_approval, write_pdf, write_xlsx
from core.schema import RFx

ROOT = Path(__file__).resolve().parent.parent
SENDER = "RFx Desk (simulated) <rfx-desk@kiranaplus.example>"
BANNER = ("SIMULATED CHANNEL: this message was written to a local folder. "
          "No email was sent over any network.")
STATUS_NOT_SENT = "not sent"


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    email: str
    inbound_channel: str  # email | whatsapp | portal ...


def load_vendors(path: Union[str, Path]) -> List[Vendor]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Vendor(**r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SimulatedChannel:
    def __init__(self, conn: sqlite3.Connection, base_dir: Union[str, Path],
                 responses_dir: Union[str, Path], vendors: Sequence[Vendor]):
        conn.row_factory = sqlite3.Row  # read columns by name
        self.conn = conn
        self.base = Path(base_dir)
        self.responses_dir = Path(responses_dir)
        self.vendors: Dict[str, Vendor] = {v.id: v for v in vendors}
        self.sent_dir = self.base / "outbox" / "sent"
        self.log_path = self.base / "outbox" / "channel_log.jsonl"
        self.inbox_dir = self.base / "inbox"
        self._ensure_vendors()

    # ------------------------------------------------------------ setup

    def _ensure_vendors(self) -> None:
        for v in self.vendors.values():
            self.conn.execute(
                "INSERT INTO vendor (id, name, email) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, email = excluded.email",
                (v.id, v.name, v.email))
        self.conn.commit()

    def _upsert_rfx(self, rfx: RFx) -> None:
        t = rfx.terms
        self.conn.execute(
            "INSERT INTO rfx (id, title, category, payment_terms_json, delivery_terms_json, "
            "validity_json, incoterms_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, category = excluded.category, "
            "payment_terms_json = excluded.payment_terms_json, "
            "delivery_terms_json = excluded.delivery_terms_json, "
            "validity_json = excluded.validity_json, incoterms_json = excluded.incoterms_json",
            (rfx.id, rfx.title, rfx.category,
             t.payment_terms.model_dump_json() if t.payment_terms else None,
             t.delivery_terms.model_dump_json() if t.delivery_terms else None,
             t.validity.model_dump_json() if t.validity else None,
             t.incoterms.model_dump_json() if t.incoterms else None))

    # --------------------------------------------------------- outbound

    def send_rfx(self, rfx: RFx, vendor_ids: Sequence[str]) -> List[dict]:
        """Write one simulated message per vendor. The RFx must be approved (checked here)."""
        problems = problems_for_approval(rfx)
        if problems:
            raise ValueError("RFx is not ready to send: " + "; ".join(problems))
        unknown = [v for v in vendor_ids if v not in self.vendors]
        if unknown or not vendor_ids:
            raise ValueError(f"unknown or empty vendor selection: {unknown or 'none selected'}")

        folder = self.sent_dir / rfx.id
        folder.mkdir(parents=True, exist_ok=True)
        pdf_path = write_pdf(rfx, folder / f"{rfx.id}.pdf")
        xlsx_path = write_xlsx(rfx, folder / f"{rfx.id}.xlsx")
        self._upsert_rfx(rfx)

        sent = []
        for vid in vendor_ids:
            v = self.vendors[vid]
            msg = EmailMessage()
            msg["From"] = SENDER
            msg["To"] = v.email
            msg["Subject"] = f"RFx {rfx.id}: {rfx.title}"
            msg["X-Simulated"] = "true"
            msg.set_content(
                f"Dear {v.name},\n\n"
                f"Please find attached RFx {rfx.id}. Reply with your quotation and answers to the "
                f"questionnaire, with your certificates attached.\n\n"
                f"{BANNER}\n")
            msg.add_attachment(pdf_path.read_bytes(), maintype="application", subtype="pdf",
                               filename=pdf_path.name)
            msg.add_attachment(xlsx_path.read_bytes(), maintype="application",
                               subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               filename=xlsx_path.name)
            path = folder / f"{vid}.eml"
            path.write_bytes(bytes(msg))
            sent_at = _now()
            rel = path.relative_to(self.base).as_posix()
            self.conn.execute(
                "INSERT INTO outbound_message (rfx_id, vendor_id, to_address, sent_at, path, simulated) "
                "VALUES (?, ?, ?, ?, ?, 1)", (rfx.id, vid, v.email, sent_at, rel))
            record = {"event": "sent", "simulated": True, "rfx_id": rfx.id, "vendor_id": vid,
                      "to": v.email, "sent_at": sent_at, "path": rel}
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            sent.append(record)
        self.conn.commit()
        return sent

    # ---------------------------------------------------------- inbound

    def simulate_replies(self, rfx_id: str) -> List[dict]:
        """Move each sent vendor's files into the inbox and register an unprocessed Response."""
        sent_vendors = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT vendor_id FROM outbound_message WHERE rfx_id = ? ORDER BY vendor_id",
            (rfx_id,))]
        if not sent_vendors:
            raise ValueError(f"nothing has been sent for {rfx_id} yet")

        results = []
        for vid in sent_vendors:
            if self._latest_response(rfx_id, vid):
                results.append({"vendor_id": vid, "result": "skipped: a response already exists"})
                continue
            src = self.responses_dir / vid
            files = sorted(p for p in src.rglob("*") if p.is_file()) if src.exists() else []
            if not files:
                results.append({"vendor_id": vid, "result": "skipped: no files in responses folder"})
                continue

            dest = self.inbox_dir / rfx_id / vid
            response_id = f"{rfx_id}:{vid}:{uuid.uuid4().hex[:6]}"
            channel = self.vendors[vid].inbound_channel
            received_at = _now()
            self.conn.execute(
                "INSERT INTO response (id, rfx_id, vendor_id, received_at, channel, status) "
                "VALUES (?, ?, ?, ?, ?, 'received')",
                (response_id, rfx_id, vid, received_at, channel))
            for f in files:
                target = dest / f.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f.read_bytes())
                mime = mimetypes.guess_type(target.name)[0]
                self.conn.execute(
                    "INSERT INTO raw_file (response_id, path, sha256, mime_type, size_bytes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (response_id, target.relative_to(self.base).as_posix(), _sha256(target),
                     mime, target.stat().st_size))
            results.append({"vendor_id": vid, "result": "received", "response_id": response_id,
                            "files": len(files), "channel": channel})
        self.conn.commit()
        return results

    # ----------------------------------------------------------- status

    def _latest_outbound(self, rfx_id: str, vid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT sent_at, to_address FROM outbound_message WHERE rfx_id = ? AND vendor_id = ? "
            "ORDER BY sent_at DESC, id DESC LIMIT 1", (rfx_id, vid))
        return cur.fetchone()

    def _latest_response(self, rfx_id: str, vid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT id, received_at, status, channel FROM response WHERE rfx_id = ? AND vendor_id = ? "
            "ORDER BY received_at DESC LIMIT 1", (rfx_id, vid))
        return cur.fetchone()

    def vendor_status(self, rfx_id: str) -> List[dict]:
        rows = []
        for vid, v in self.vendors.items():
            out = self._latest_outbound(rfx_id, vid)
            resp = self._latest_response(rfx_id, vid)
            if resp:
                status = resp["status"]
            elif out:
                status = "sent"
            else:
                status = STATUS_NOT_SENT
            n_files = 0
            if resp:
                n_files = self.conn.execute("SELECT COUNT(*) FROM raw_file WHERE response_id = ?",
                                            (resp["id"],)).fetchone()[0]
            rows.append({
                "vendor_id": vid,
                "vendor": v.name,
                "status": status,
                "sent_at": out["sent_at"] if out else "",
                "received_at": resp["received_at"] if resp else "",
                "channel": resp["channel"] if resp else "",
                "files": n_files,
                "response_id": resp["id"] if resp else "",
            })
        return rows

    def set_response_status(self, response_id: str, status: str) -> None:
        if status not in RESPONSE_STATUSES:
            raise ValueError(f"status must be one of {RESPONSE_STATUSES}")
        cur = self.conn.execute("UPDATE response SET status = ? WHERE id = ?", (status, response_id))
        if cur.rowcount != 1:
            raise ValueError(f"no response {response_id}")
        self.conn.commit()


def open_default_channel(db_path: Optional[Union[str, Path]] = None,
                         check_same_thread: bool = True) -> SimulatedChannel:
    """Channel wired to the project's data folders and the default database."""
    import os

    path = db_path or os.environ.get("DATABASE_PATH") or ROOT / "data" / "rfx.db"
    conn = init_db(path, check_same_thread=check_same_thread)
    return SimulatedChannel(conn, ROOT / "data", ROOT / "data" / "responses",
                            load_vendors(ROOT / "data" / "vendors.json"))
