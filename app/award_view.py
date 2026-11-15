"""Streamlit 'Award' tab: scenario builder, results, risks, and exports."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from core import award as award_mod
from core import award_report as ar
from core import compare
from core.schema import RFx

OUT = Path(__file__).resolve().parents[1] / "data" / "outbox" / "analysis"


def _scenario_text(qual: bool, cap: float, usd: bool, unconf: bool, excluded: set) -> str:
    parts = []
    parts.append("only vendors passing all hard requirements" if qual else "all responding vendors")
    if cap:
        parts.append(f"cap {cap:.0f}% per vendor")
    if excluded:
        parts.append("excluding " + ", ".join(sorted(excluded)))
    if not usd:
        parts.append("USD-sourced lines excluded")
    if unconf:
        parts.append("unconfirmed values excluded")
    return "; ".join(parts)


def render_award(ch, rfx: RFx, responses, client) -> None:
    names = {vid: v.name for vid, v in ch.vendors.items()}
    m = compare.build_matrix(ch.conn, rfx, responses, names)

    st.caption("Build an award scenario from explicit rules. Every figure below is computed from the stored "
               "quotes; the memo is drafted from these same numbers.")
    with st.form("award_rules"):
        c1, c2, c3 = st.columns(3)
        qual = c1.checkbox("Only vendors passing the hard requirements", value=True)
        cap = c2.number_input("Cap any vendor's share (%, 0 = no cap)", min_value=0.0, max_value=100.0,
                              value=0.0, step=5.0)
        basis = c3.radio("Basis", ["Ex-freight", "Landed (freight included)"], index=0,
                         help="Landed is unavailable: no response quantifies freight.")
        d1, d2, d3 = st.columns(3)
        usd = d1.checkbox("Include USD-sourced lines", value=True)
        unconf = d2.checkbox("Exclude unconfirmed values", value=False)
        excl = d3.multiselect("Exclude vendors", options=list(names.values()))
        run = st.form_submit_button("Run scenario", type="primary")

    if basis.startswith("Landed"):
        st.error("Landed view is not available: no response quantifies freight. Showing ex-freight only.")

    if run or "award_state" not in st.session_state:
        passing, reasons = ar.eligible_by_requirements(ch.conn, responses, names)
        eligible = passing if qual else {vid for vid in names if responses.get(vid)}
        excluded_vids = {vid for vid, n in names.items() if n in excl}
        sc = award_mod.Scenario(eligible_vendors=eligible - excluded_vids, exclude_vendors=excluded_vids,
                                max_share=(cap / 100) if cap else None, include_usd_lines=usd,
                                exclude_unconfirmed=unconf)
        a = award_mod.run_award(m, sc)
        excluded_names = {n: r for n, r in reasons.items()}
        if qual:
            for vid, n in names.items():
                if responses.get(vid) and vid not in passing and n not in excluded_names:
                    excluded_names[n] = "did not pass hard requirements"
        for n in excl:
            excluded_names[n] = "excluded by the buyer"
        st.session_state.award_state = {
            "rep": ar.build_report(m, a, excluded=excluded_names),
            "text": _scenario_text(qual, cap, usd, unconf, excl),
        }

    state = st.session_state.award_state
    rep: ar.Report = state["rep"]
    a = rep.award

    st.markdown(f"**Scenario:** {state['text']}")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total award (INR ex-tax)", f"{a.total:,.0f}")
    k2.metric("Savings vs highest quote", f"{rep.savings_vs_highest:,.0f}",
              f"{rep.savings_pct:.1%}" if rep.highest_total else None)
    k3.metric("Savings vs last year", "n/a" if rep.last_year_total is None else f"{rep.last_year_total:,.0f}")
    k4.metric("Lines awarded", f"{len(a.assignments)} of {len(m.lines)}")
    if not a.feasible:
        st.warning("The vendor cap could not be met with these vendors. " + " ".join(a.notes[1:]))

    st.markdown("**Awarded per vendor**")
    rows = []
    for vid, name in m.vendors:
        pv = a.per_vendor.get(vid, {"lines": 0, "value": 0.0, "share": 0.0})
        rows.append({"Vendor": name, "Lines": int(pv["lines"]), "Award value (INR)": round(pv["value"], 2),
                     "Share": f"{pv['share']:.1%}"})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.markdown("**Lines**")
    line_rows = []
    for l in m.lines:
        vid = a.assignments.get(l["id"])
        c = m.cell(vid, l["id"]) if vid else None
        line_rows.append({"Line": l["id"], "Description": l["description"], "Qty": float(l["qty"]),
                          "Awarded to": dict(m.vendors)[vid] if vid else "not awarded",
                          "Unit price": None if c is None else round(c.price, 2),
                          "Confidence": "" if c is None else (c.confidence or "unknown")})
    st.dataframe(pd.DataFrame(line_rows), width="stretch", hide_index=True, height=340)

    st.markdown("**Risk notes**")
    if not rep.risks:
        st.success("No risk notes for this scenario.")
    for r in rep.risks:
        label = {"single_source": "Single source", "thin_coverage": "Thin coverage",
                 "unconfirmed_price": "Unconfirmed price", "unawarded": "Not awarded",
                 "no_response": "No response", "cap_not_met": "Cap not met"}.get(r["type"], r["type"])
        where = " · ".join(str(x) for x in (r.get("line"), r.get("vendor")) if x)
        st.write(f"- **{label}**" + (f" ({where})" if where else "") + f": {r['detail']}")
    if rep.excluded:
        st.markdown("**Excluded vendors**")
        for n, why in rep.excluded.items():
            st.write(f"- {n}: {why}")
    st.caption(rep.basis_note)

    st.divider()
    st.markdown("**Exports**")
    e1, e2 = st.columns(2)
    if e1.button("Build comparison workbook (xlsx)"):
        OUT.mkdir(parents=True, exist_ok=True)
        review_rows = [{"id": r["id"], "vendor": r["vendor"], "entity_type": r["entity_type"],
                        "field_name": r["field_name"], "issue": r["issue"], "reason": r["reason"],
                        "proposed": r["proposed"], "status": "open"}
                       for r in compare.open_reviews(ch.conn, rfx.id)]
        path = ar.write_workbook(OUT / f"comparison_{rfx.id}.xlsx", rfx, m, rep, review_rows,
                                 ar.source_rows(ch.conn, rfx.id))
        st.session_state.last_export = str(path)
    if e2.button("Draft award memo (docx)"):
        facts = ar.build_facts(rfx, m, rep, state["text"])
        memo = ar.draft_memo(client, facts, model=_model()) if client is not None else \
            {"sections": ar._template_sections(facts), "source": "template", "attempts": 0}
        path = ar.write_memo_docx(OUT / f"award_memo_{rfx.id}_{date.today():%Y%m%d}.docx", rfx, memo)
        st.session_state.last_export = str(path)
        st.session_state.memo_source = memo["source"]
    if st.session_state.get("last_export"):
        p = Path(st.session_state.last_export)
        if p.exists():
            st.download_button(f"Download {p.name}", p.read_bytes(), file_name=p.name)
            if p.suffix == ".docx":
                src = st.session_state.get("memo_source", "model")
                st.caption(f"Memo drafted by: {'the model, numbers checked against the scenario' if src == 'model' else 'the template (no model key, or the draft failed the number check)'}")


def _model() -> str:
    import os

    return os.environ.get("MEMO_MODEL") or os.environ.get("ANALYST_MODEL") or "gemini-3.1-flash-lite-preview"
