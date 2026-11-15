"""Streamlit front end. Run from the project root:

    streamlit run app/streamlit_app.py

Chat needs ANTHROPIC_API_KEY (and optionally COPILOT_MODEL). Without a key the page still
loads the demo RFx, shows it, and can approve it.
"""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from core.channel import STATUS_NOT_SENT, open_default_channel  # noqa: E402
from core.extract import run_extraction  # noqa: E402
from app.compare_view import render_compare  # noqa: E402
from app.ask_view import render_ask  # noqa: E402
from app.award_view import render_award  # noqa: E402
from core.rfx_copilot import RFxCopilot, blank_doc, load_doc  # noqa: E402
from core.rfx_export import (  # noqa: E402
    _rows_lines,
    _terms_rows,
    approve_and_export,
    problems_for_approval,
)

DEMO_RFX = ROOT / "data" / "rfx" / "rfx.json"
OUTBOX = ROOT / "data" / "outbox"


def make_client():
    """Chat co-pilot client (Anthropic). None when no key is set."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from anthropic import Anthropic

    return Anthropic()


def make_extract_client():
    """Extraction client, from EXTRACT_PROVIDER (default gemini). None when its key is missing."""
    from core.llm import make_client as make_llm_client

    try:
        return make_llm_client()
    except RuntimeError:
        return None


def start_copilot(doc, note: str = "") -> None:
    st.session_state.copilot = RFxCopilot(make_client(), doc)
    st.session_state.transcript = [("assistant", note, [])] if note else []
    st.session_state.last = None
    st.session_state.approval = None


@st.cache_resource
def get_channel():
    return open_default_channel(check_same_thread=False)


def live_channel():
    """The cached channel, checked on every use. If its connection went stale (for example the database
    file was replaced underneath it), drop the cache and reconnect instead of showing a disk I/O error."""
    ch = get_channel()
    try:
        ch.conn.execute("SELECT 1").fetchone()
    except sqlite3.OperationalError:
        get_channel.clear()
        ch = get_channel()
    return ch


def diff_rows(changes):
    return [{"path": c.path, "before": "" if c.before is None else str(c.before),
             "after": "" if c.after is None else str(c.after)} for c in changes]


st.set_page_config(page_title="RFx builder", layout="wide")
if "copilot" not in st.session_state:
    start_copilot(blank_doc())

cp: RFxCopilot = st.session_state.copilot
tab_create, tab_inbox, tab_compare, tab_award, tab_ask = st.tabs(
    ["Create RFx", "Inbox", "Compare", "Award", "Ask"])

with tab_create:
    b1, b2, _ = st.columns([1, 1, 2])
    if b1.button("Load pre-generated 30-line RFx"):
        start_copilot(load_doc(DEMO_RFX), "Loaded the 30-line corrugated packaging RFx. Tell me what to change.")
        st.rerun()
    if b2.button("Start blank draft"):
        start_copilot(blank_doc(), "New blank draft. Describe what you need.")
        st.rerun()

    if st.button("Load demo (one click)", type="primary", key="demo_btn",
                 help="Resets state and walks Create RFx, Send, Receive, Extract, Compare, Ask. Uses caches; "
                      "every live model call is shown."):
        from core.demo import run_demo

        model_name = os.environ.get("EXTRACT_MODEL") or "gemini-3.1-flash-lite-preview"
        analyst_name = os.environ.get("ANALYST_MODEL") or "gemini-3.1-flash-lite-preview"
        with st.status("Running the demo", expanded=True) as status:
            for ev in run_demo(provider=None, model=model_name, analyst_model=analyst_name):
                icon = {"running": "⏳", "done": "✅", "cached": "💾", "failed": "❌", "skipped": "⏭️"}[ev.status]
                st.write(f"{icon} **{ev.stage}** — {ev.detail}")
            status.update(label="Demo ready", state="complete")
        start_copilot(load_doc(DEMO_RFX), "Demo loaded: the RFx is approved, sent, received and extracted. "
                                          "Open Compare, Award or Ask.")
        outbox = sorted((ROOT / "data" / "outbox").glob("RFX-2026-PKG-017_r000.pdf"))
        if outbox:
            stem = outbox[0].with_suffix("")
            st.session_state.approval = {"pdf": str(outbox[0]), "xlsx": str(stem.with_suffix(".xlsx")),
                                         "approval": str(stem.with_suffix(".approval.json"))}
        st.session_state.copilot.locked = True
        st.rerun()

    if cp.client is None:
        st.warning("ANTHROPIC_API_KEY is not set, so chat is disabled. You can still load, review and approve an RFx.")

    left, right = st.columns([1, 1.25])

    with left:
        st.subheader("Chat")
        with st.container(height=480):
            for role, text, changes in st.session_state.transcript:
                with st.chat_message(role):
                    st.markdown(text or "_(no reply)_")
                    if changes:
                        st.caption(f"{len(changes)} change(s) applied")
        prompt = st.chat_input(
            "Describe the need, or ask for a change (e.g. 'make payment terms 45 days')",
            disabled=cp.client is None or cp.locked)
        if prompt:
            st.session_state.transcript.append(("user", prompt, []))
            try:
                with st.spinner("Working on it..."):
                    res = cp.send(prompt)
                st.session_state.transcript.append(("assistant", res.reply, res.changes))
                st.session_state.last = res
            except Exception as e:  # keep the page usable if the API call fails
                st.session_state.transcript.append(("assistant", f"Error talking to the model: {e}", []))
            st.rerun()

    with right:
        rfx = cp.rfx()
        st.subheader(f"{rfx.id}  ·  revision {cp.revision}")
        st.markdown(f"**{rfx.title or '(no title yet)'}**")
        st.write(rfx.scope or "_No scope yet._")
        st.markdown("**Commercial terms**")
        st.table([{"Term": r[0], "Requirement": r[1]} for r in _terms_rows(rfx)])
        st.markdown(f"**Line items ({len(rfx.line_items)})**")
        st.dataframe([{"ID": r[0], "Description": r[1], "Specification": r[2], "Qty": r[3], "UOM": r[4]}
                      for r in _rows_lines(rfx)], height=300, width="stretch", hide_index=True)
        st.markdown(f"**Questionnaire ({len(rfx.questionnaire)})**")
        st.dataframe([{"ID": q.id, "Question": q.text, "Required": q.required} for q in rfx.questionnaire],
                     height=220, width="stretch", hide_index=True)

        last = st.session_state.get("last")
        if last and last.changes:
            st.markdown("**Changes in the last reply**")
            st.dataframe(diff_rows(last.changes), width="stretch", hide_index=True)
        if last and last.tool_errors:
            st.warning("Some changes were rejected and the model was asked to retry:\n" +
                       "\n".join(f"- {e}" for e in last.tool_errors))
        if cp.log:
            with st.expander("Full change log"):
                for entry in cp.log:
                    if entry["changes"]:
                        st.markdown(f"**Turn {entry['turn']}:** {entry['user']}")
                        st.dataframe(entry["changes"], width="stretch", hide_index=True)

    st.divider()
    st.subheader("Approve")
    problems = problems_for_approval(rfx)
    if problems:
        st.info("Before approving: " + " ".join(problems))
    if st.session_state.approval:
        st.success("Approved and written to data/outbox/. Nothing was emailed; delivery to vendors is a separate step.")
        for label, key in [("Download PDF", "pdf"), ("Download Excel", "xlsx")]:
            path = Path(st.session_state.approval[key])
            st.download_button(label, path.read_bytes(), file_name=path.name)
        st.code("\n".join(st.session_state.approval.values()))
    elif st.button("Approve and send", type="primary", disabled=bool(problems) or cp.locked):
        try:
            st.session_state.approval = approve_and_export(rfx, cp.revision, OUTBOX)
            cp.locked = True
            st.rerun()
        except ValueError as e:
            st.error(str(e))

with tab_inbox:
    ch = live_channel()
    rfx_id = cp.doc["id"]
    st.info("SIMULATED EMAIL CHANNEL. Messages are written to data/outbox/sent/ and nothing is sent "
            "over a network. Vendor replies are copied from the local dataset when you press the button.")

    st.subheader("Send")
    approved = bool(st.session_state.get("approval")) and cp.locked
    if not approved:
        st.warning("Approve an RFx in the Create RFx tab before sending it to vendors.")
    else:
        names = {v.id: v.name for v in ch.vendors.values()}
        chosen = st.multiselect("Send to", options=list(names), default=list(names),
                                format_func=lambda k: names[k])
        if st.button("Send to selected vendors (simulated)", type="primary", disabled=not chosen):
            try:
                sent = ch.send_rfx(cp.rfx(), chosen)
                st.success(f"Wrote {len(sent)} simulated message(s) to data/outbox/sent/{rfx_id}/.")
            except ValueError as e:
                st.error(str(e))

    st.subheader("Status by vendor")
    statuses = ch.vendor_status(rfx_id)
    st.dataframe([{"Vendor": s["vendor"], "Status": s["status"], "Sent": s["sent_at"],
                   "Received": s["received_at"], "Channel": s["channel"], "Files": s["files"]}
                  for s in statuses], width="stretch", hide_index=True)

    st.subheader("Inbox (simulated replies)")
    st.caption("Files in data/responses/<vendor>/ stand in for replies that arrive. Pressing the button "
               "copies them into data/inbox/ and registers one unprocessed response per vendor.")
    file_rows = []
    for v in ch.vendors.values():
        folder = ch.responses_dir / v.id
        if folder.exists():
            for p in sorted(f for f in folder.rglob("*") if f.is_file()):
                file_rows.append({"Vendor": v.name, "File": p.relative_to(folder).as_posix(),
                                  "Bytes": p.stat().st_size})
    st.dataframe(file_rows, width="stretch", hide_index=True)

    any_sent = any(s["status"] != STATUS_NOT_SENT for s in statuses)
    if st.button("Simulate vendor replies arriving", disabled=not any_sent):
        try:
            results = ch.simulate_replies(rfx_id)
            st.session_state.sim_results = results
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    if st.session_state.get("sim_results"):
        st.markdown("**Last simulation**")
        st.table(st.session_state.sim_results)

    st.subheader("Extract responses")
    st.caption("Reads every file a vendor sent and records quotes, answers and terms, with a source "
               "snippet for each value. Uses the model, so it needs ANTHROPIC_API_KEY. Raw model output "
               "is cached on disk, so reruns are fast unless you force a re-extract.")
    received = [s for s in statuses if s["response_id"]]
    force = st.checkbox("Force re-extract (ignore the cache)")
    if not received:
        st.info("No responses to extract yet. Simulate vendor replies first.")
    extract_client = make_extract_client()
    if extract_client is None:
        st.warning("No extraction model key is set (GEMINI_API_KEY for the default provider).")
    if st.button("Extract all received responses", type="primary",
                 disabled=not received or extract_client is None):
        rfx_obj = cp.rfx()
        bar = st.progress(0.0, text="Starting...")
        rows = []
        for i, s_row in enumerate(received):
            folder = ch.inbox_dir / rfx_id / s_row["vendor_id"]

            def cb(done, total, label, i=i, s_row=s_row):
                bar.progress((i + done / total) / len(received),
                             text=f"{s_row['vendor']}: {label} ({done}/{total})")

            try:
                _, summary = run_extraction(ch.conn, s_row["response_id"], folder, rfx_obj, extract_client,
                                            force=force, progress=cb)
                rows.append({"Vendor": s_row["vendor"], **summary})
            except Exception as e:  # one vendor failing should not stop the rest
                rows.append({"Vendor": s_row["vendor"], "status": "error", "error": str(e)[:200]})
        bar.progress(1.0, text="Done")
        st.session_state.extract_summary = rows
        st.rerun()
    if st.session_state.get("extract_summary"):
        st.markdown("**Last extraction**")
        st.dataframe(st.session_state.extract_summary, width="stretch", hide_index=True)

with tab_compare:
    ch_cmp = live_channel()
    rfx_cmp = cp.rfx()
    responses_cmp = {s_["vendor_id"]: s_["response_id"] or None for s_ in ch_cmp.vendor_status(cp.doc["id"])}
    if not any(responses_cmp.values()):
        st.info("No vendor responses yet. Send the RFx, simulate replies, then extract them in the Inbox tab.")
    else:
        render_compare(ch_cmp, rfx_cmp, responses_cmp)

with tab_ask:
    from core.llm import make_client as make_llm_client

    try:
        analyst_client = make_llm_client()
    except RuntimeError:
        analyst_client = None
    ch_ask = live_channel()
    rfx_ask = cp.rfx()
    responses_ask = {s_["vendor_id"]: s_["response_id"] or None for s_ in ch_ask.vendor_status(cp.doc["id"])}
    render_ask(ch_ask.conn, rfx_ask, responses_ask, {vid: v.name for vid, v in ch_ask.vendors.items()},
               analyst_client)

with tab_award:
    from core.llm import make_client as make_llm_client

    try:
        memo_client = make_llm_client()
    except RuntimeError:
        memo_client = None
    ch_aw = live_channel()
    rfx_aw = cp.rfx()
    responses_aw = {s_["vendor_id"]: s_["response_id"] or None for s_ in ch_aw.vendor_status(cp.doc["id"])}
    if not any(responses_aw.values()):
        st.info("No vendor responses yet. Send the RFx, simulate replies, then extract them in the Inbox tab.")
    else:
        render_award(ch_aw, rfx_aw, responses_aw, memo_client)
