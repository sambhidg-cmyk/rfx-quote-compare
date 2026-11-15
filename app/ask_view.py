"""Streamlit 'Ask' tab: a chat with the analyst over the whole comparison."""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from core import analyst

ANALYST_MODEL = os.environ.get("ANALYST_MODEL", "gemini-3.1-flash-lite-preview")


def render_ask(conn, rfx, responses, names, client) -> None:
    if "analyst" not in st.session_state or st.session_state.get("analyst_rfx") != rfx.id:
        st.session_state.analyst = analyst.AnalystSession(conn=conn, rfx=rfx, responses=responses, names=names)
        st.session_state.analyst_rfx = rfx.id
    session: analyst.AnalystSession = st.session_state.analyst
    session.responses = responses  # refresh in case replies arrived since the last question

    st.caption("Ask about the comparison. Answers come from tool calls over the stored data: "
               "numbers are computed, sources are cited, and unconfirmed values are flagged. "
               "Each answer shows the tool calls it made.")
    if client is None:
        st.warning("No model key is set for the analyst (GEMINI_API_KEY). The chat is disabled.")

    for i, turn in enumerate(session.transcript):
        with st.chat_message("user"):
            st.markdown(turn["question"])
        with st.chat_message("assistant"):
            st.markdown(turn["answer"] or "_(no answer text)_")
            for ch in turn["charts"]:
                st.image(session.charts[ch], caption=ch)
            for f in turn["files"]:
                p = Path(f)
                if p.exists():
                    st.download_button(f"Download {p.name}", p.read_bytes(), file_name=p.name, key=f"dl-{i}-{p.name}")
            with st.expander(f"Tool calls ({len(turn['tool_calls'])})"):
                for c in turn["tool_calls"]:
                    st.markdown(f"**{c['tool']}**{' — error' if c['error'] else ''}")
                    st.code(str(c["args"])[:600], language="json")

    prompt = st.chat_input("Ask about the comparison, e.g. 'who is cheapest like-for-like?'",
                           disabled=client is None)
    if prompt:
        try:
            with st.spinner("Analysing..."):
                analyst.ask(client, session, prompt, model=ANALYST_MODEL)
        except Exception as e:  # keep the chat usable if the model call fails
            session.transcript.append({"question": prompt, "answer": f"Error talking to the model: {e}",
                                       "tool_calls": [], "charts": [], "files": [], "tables": []})
        st.rerun()
