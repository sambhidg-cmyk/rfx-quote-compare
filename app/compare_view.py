"""Streamlit rendering for the Compare tab. All numbers come from core.compare."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import streamlit as st

from core import compare
from core.compare import CONF_ICON, CONF_WORD, Cell, Matrix
from core.schema import RFx

LOWEST_CSS = "background-color: #d4f5d0; font-weight: 600"


def _cell_text(c: Cell) -> str:
    if c.status == "no_response":
        return "no response"
    if c.status == "not_quoted":
        return "not quoted"
    if c.price is None:
        return "⚠ needs review"
    icon = CONF_ICON.get(c.confidence or "", "⚪")
    flag = " ⚑" if c.flagged else ""
    return f"₹{c.price:,.2f} {icon}{flag}"


def _matrix_frame(m: Matrix):
    rows = []
    for l in m.lines:
        row = {"Line": l["id"], "Description": l["description"], "Qty": f"{float(l['qty']):,.0f}",
               "UOM": l["uom"]}
        for vid, name in m.vendors:
            row[name] = _cell_text(m.cell(vid, l["id"]))
        rows.append(row)
    df = pd.DataFrame(rows)
    css = pd.DataFrame("", index=df.index, columns=df.columns)
    for i, l in enumerate(m.lines):
        for vid, name in m.vendors:
            if m.cell(vid, l["id"]).lowest:
                css.loc[i, name] = LOWEST_CSS
    return df, css


def _totals_frame(m: Matrix, rfx: RFx):
    partial = m.partial_basket()
    lfl = m.like_for_like_basket()
    common = len(m.like_for_like_lines())
    rows = []
    for vid, name in m.vendors:
        quoted, total = m.coverage(vid)
        value, n = partial[vid]
        complete = quoted == total
        status = "complete" if complete else "PARTIAL BASKET"
        rows.append({
            "Vendor": name,
            "Coverage": f"{quoted} / {total} lines",
            "Basket on RFx qty (INR ex-tax)": f"₹{value:,.0f}" if n else "-",
            "Status": status,
            f"Like-for-like ({common} common lines)": f"₹{lfl.get(vid, 0):,.0f}" if common else "-",
        })
    return pd.DataFrame(rows)


def _banner(m: Matrix) -> None:
    t = m.trust_counts()
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        c1.metric("High-confidence values", t["high"])
        c2.metric("Need your review", t["review"])
        c3.metric("Not quoted", t["not_quoted"])
        st.caption("Values = vendor-by-line cells. High-confidence means the snippet was verified against "
                   "the source and the model rated it high, with no open review item.")


def _side_panel(ch, rfx: RFx, m: Matrix, sel: Optional[tuple]) -> None:
    st.markdown("#### Cell detail")
    if sel is None or sel not in m.cells:
        st.info("Click a price in the matrix to see where it came from.")
        return
    vid, lid = sel
    c = m.cell(vid, lid)
    name = dict(m.vendors)[vid]
    line = next(l for l in m.lines if l["id"] == lid)
    st.markdown(f"**{name}** · **{lid}** {line['description']}")
    if c.status != "quoted":
        st.warning("Not quoted by this vendor." if c.status == "not_quoted" else "No response received yet.")
        return
    icon = CONF_ICON.get(c.confidence or "", "⚪")
    st.markdown(f"Confidence: {icon} **{CONF_WORD.get(c.confidence or '', 'unknown')}**"
                + ("  ·  ⚑ open review item" if c.flagged else ""))
    st.markdown("**As the vendor wrote it**")
    st.code(c.stated or "not recorded")
    st.markdown("**Normalized to the RFx unit** (INR, ex-tax, per " + line["uom"] + ")")
    st.write(f"₹{c.price:,.2f}" if c.price is not None else "not converted; see review")
    if c.steps:
        for s in c.steps:
            st.caption(f"• {s}")
    else:
        st.caption("• no conversion needed")
    st.markdown("**Source**")
    st.write(f"`{c.source_file or '-'}` · {c.locator or '-'}")
    if c.snippet:
        st.code(c.snippet, language=None)
    _source_image(ch, rfx, vid, c)


def _source_image(ch, rfx: RFx, vid: str, c: Cell) -> None:
    if not c.source_file:
        return
    path = Path(ch.inbox_dir) / rfx.id / vid / c.source_file
    if not path.exists():
        st.caption("Original file not found in the inbox.")
        return
    if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
        st.image(str(path), caption="Original photo")
    elif path.suffix.lower() == ".pdf":
        import re

        import pypdfium2 as pdfium

        m = re.search(r"p\.(\d+)", c.locator or "")
        page = int(m.group(1)) if m else 1
        pdf = pdfium.PdfDocument(str(path))
        if 1 <= page <= len(pdf):
            img = pdf[page - 1].render(scale=1.4).to_pil()
            st.image(img, caption=f"Original page {page}")
    else:
        st.caption("Text source: the snippet above is the exact text the value came from.")


def _review_queue(ch, rfx: RFx) -> None:
    items = compare.open_reviews(ch.conn, rfx.id)
    if not items:
        st.success("Nothing needs your review.")
    for it in items:
        with st.container(border=True):
            st.markdown(f"**{it['vendor']}** · {it['entity_type']} · `{it['field_name']}`")
            st.write(f"**Uncertain:** {it['issue']}")
            st.write(f"**Why:** {it['reason']}")
            st.write(f"**Proposed value:** {it['proposed']}")
            c1, c2, c3 = st.columns([1, 1, 2])
            if c1.button("Accept", key=f"acc-{it['id']}"):
                _run(lambda: compare.apply_review(ch.conn, it["id"], "accept"))
            default = float(it["proposed"]) if _is_number(it["proposed"]) else 0.0
            new = c3.number_input("Override price (INR ex-tax, RFx unit)", min_value=0.0, value=default,
                                  key=f"ov-val-{it['id']}", step=0.01, format="%.2f")
            if c2.button("Override", key=f"ov-{it['id']}", disabled=it["entity_type"] != "quote"):
                _run(lambda: compare.apply_review(ch.conn, it["id"], "override", new_value=new,
                                                  note="overridden in Compare tab"))
            if st.button("Draft clarification email to vendor", key=f"clar-{it['id']}"):
                _clarification(ch, rfx, it)
    with st.expander("Audit trail"):
        trail = compare.audit_trail(ch.conn, rfx.id)
        if trail:
            st.dataframe(pd.DataFrame(trail)[["at", "actor", "action", "entity_type", "entity_id",
                                              "field_name", "before_json", "after_json", "note"]],
                         width="stretch", hide_index=True)
        else:
            st.caption("No decisions yet.")


def _clarification(ch, rfx: RFx, item) -> None:
    from core.clarify import draft_clarification, save_draft

    vendor = next((v for v in ch.vendors.values() if v.name == item["vendor"]), None)
    if vendor is None:
        st.error("No vendor address on file for this response.")
        return
    draft = draft_clarification(item, rfx, vendor.email, vendor.name)
    path = save_draft(draft, f"{rfx.id}_{item['id']}")
    st.info(f"Draft saved, not sent: {path.name}")
    st.code(f"To: {draft.to}\nSubject: {draft.subject}\n\n{draft.body}", language=None)
    st.download_button("Download draft (.eml)", path.read_bytes(), file_name=path.name,
                       key=f"dl-{item['id']}")


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _run(fn) -> None:
    try:
        msg = fn()
        st.toast(msg)
        st.rerun()
    except ValueError as e:
        st.error(str(e))


def render_compare(ch, rfx: RFx, responses: Dict[str, Optional[str]]) -> None:
    names = {vid: v.name for vid, v in ch.vendors.items()}
    m = compare.build_matrix(ch.conn, rfx, responses, names)
    _banner(m)

    tab_matrix, tab_q, tab_docs, tab_review = st.tabs(
        ["Price matrix", "Questionnaire and requirements", "Documents", "Needs your review"])

    with tab_matrix:
        left, right = st.columns([2.3, 1])
        with left:
            df, css = _matrix_frame(m)
            event = st.dataframe(df.style.apply(lambda _: css, axis=None), width="stretch",
                                 hide_index=True, on_select="rerun", selection_mode="single-cell",
                                 key="cmp_matrix")
            cells = event.selection.cells if event and event.selection else []
            if cells:
                row_pos, col_name = cells[0]
                vid = next((v for v, n in m.vendors if n == col_name), None)
                if vid is not None:
                    st.session_state["cmp_sel"] = (vid, m.lines[row_pos]["id"])
            st.caption("🟢 high  🟡 medium  🔴 low  ·  ⚑ open review  ·  green = lowest price on the line. "
                       "Click any price for its source.")
            st.markdown("**Totals on RFx quantities**")
            st.dataframe(_totals_frame(m, rfx), width="stretch", hide_index=True)
            if any(m.coverage(v)[0] < len(m.lines) for v, _ in m.vendors):
                st.caption("Partial basket = only the lines that vendor priced. "
                           "Like-for-like = only the lines every vendor priced, so totals compare fairly.")
        with right:
            _side_panel(ch, rfx, m, st.session_state.get("cmp_sel"))

    with tab_q:
        answers = compare.answers_by_question(ch.conn, responses)
        st.markdown("**Hard requirements**")
        st.dataframe(pd.DataFrame(compare.hard_requirement_table(answers, m.vendors)),
                     width="stretch", hide_index=True)
        st.caption("PASS / FAIL / CHECK is a rule applied to the extracted answer. Read the answer before acting.")
        st.markdown("**Questionnaire, side by side**")
        rows = []
        for q in rfx.questionnaire:
            row = {"Question": f"{q.id} {q.text}"}
            for vid, name in m.vendors:
                val, conf = answers.get(q.id, {}).get(vid, (None, None))
                row[name] = "—" if val is None else f"{CONF_ICON.get(conf or '', '⚪')} {val}"
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    with tab_docs:
        for vid, name in m.vendors:
            docs = compare.attached_docs(ch.inbox_dir, rfx.id, vid)
            with st.expander(f"{name} · {len(docs)} PDF attachment(s)", expanded=bool(docs)):
                if not docs:
                    st.caption("No PDF attachments received yet.")
                for d in docs:
                    flag = {"expired": "⛔ expired", "valid": "✅ valid",
                            "unknown": "❔ no expiry found"}[d["status"]]
                    st.write(f"{flag} · `{d['file']}` · expiry {d['expiry']}")

    with tab_review:
        _review_queue(ch, rfx)
