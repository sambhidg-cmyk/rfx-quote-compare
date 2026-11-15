# Walkthrough: the analyst conversation

A transcript of the eight questions in [DEMO_SCRIPT.md](DEMO_SCRIPT.md), run live against the demo database
(`gemini-3.1-flash-lite-preview`, one session, memory carried across questions). Answers are shown close to what the
model wrote. The notes after each one say what to check, because several answers contain errors. Read those notes
before you present.

Run it yourself with `python3 scripts/ask_analyst.py --questions-file <file>`, or use the Ask tab.

---

## Q1. Who is cheapest overall, like-for-like?

Tool calls: `basket_summary`

> Like-for-like across the 26 lines all vendors priced: **Nakoda Paper Packs** is cheapest at 53,034,592 INR. Sahyadri
> 54,571,528; Bhiwandi 57,216,773; Ganesh 58,217,225; Vardhan 82,207,761. Prices are ex-tax and ex-freight; no response
> quantifies freight.

**Check before presenting:** Vardhan's 82M is inflated. Vendor A's L06 price is 680.79, a pallet's price on the box
line (the correct figure is about 52). The error adds about 26.4M to Vardhan's total, which moves it from third
place to last. The ranking at the top is unchanged: Nakoda stays cheapest. **Do not present Q1's full table until it's
fixed.** It is the best question to show the problem.

---

## Q2. Split the award: cheapest per line, among vendors who cleared the questionnaire

Tool calls: `hard_requirements`, `scenario`, `confirmation_list`

> Only Vardhan and Ganesh Kraft pass both hard requirements. Vardhan: 28 lines, 91.4% of value. Ganesh: 2 lines
> (L06 and L23), 8.6%. Excluded: Bhiwandi, Sahyadri, Nakoda.

**What works:** the tool calls come before the claim of qualification, and the exclusions are given with reasons.

**Check before presenting:** the reasons are partly wrong. Bhiwandi passed the in-house ECT/BCT test; it failed only on
its expired ISO 9001 certificate. Ganesh's L06 win is a side effect of Vardhan's L06 error.

---

## Q3. What if we cap any vendor at 40%?

Tool calls: `scenario` (with max share 40)

> Not feasible. With two qualified vendors, shares must total 100%, so both cannot be under 40%. The closest split is
> Vardhan 54.2%, Ganesh 45.8%.

**What works:** the pushback is right, and the arithmetic is the tool's.

**Check before presenting:** the answer says 20 unconfirmed prices from Ganesh. The database has 30 for Ganesh in Q5.
The count is the model's, not the tool's.

---

## Q4. Which vendor is cheapest on the full 30-line basket?

Tool calls: `basket_summary`

> Not a fair comparison: only three vendors priced all 30 lines, and Sahyadri (27/30) and Nakoda (29/30) did not. The
> like-for-like view is the fair one, and it ranks Nakoda cheapest.

**What works:** this is the pushback the system is built for. It refuses the unfair comparison, names the partial
bidders, and points to the fair view. Use this question if someone asks why the system doesn't just sum the sheets.

---

## Q5. Which prices should I double-check before signing?

Tool calls: `confirmation_list`

> Every line Ganesh quoted (30 in total) is unconfirmed: medium confidence, open review items, read from
> `rate_card_photo.jpg`. Check the units (per piece versus per 100 pieces) against the original.

**What works:** the answer comes from the tool, and it says why (the photo) and what to check (the units).

**Check before presenting:** the count of 30 disagrees with Q3's 20 for the same vendor.

---

## Q6. Chart the award value by vendor

Tool calls: `make_table`, `make_chart`

> Vardhan 53,310,990 INR; Ganesh 4,991,140 INR. Chart: `data/outbox/analysis/q6_chart1.png`.

**Known issue:** the table behind the chart was built from literal values typed into the SQL, not read from the
scenario. The numbers match this run, but nothing guarantees they will after a change. The fix is to have the
analyst pass the scenario's output into the table, not type it.

---

## Q7. Export the award lines to Excel

Tool calls: `make_table`, `export`

> Exported to `data/outbox/analysis/final_award_lines.xlsx`: 30 lines, assigned between Vardhan and Ganesh by cheapest
> price per line.

**Verified:** I compared the file with the scenario output. All 30 assignments match. The same caveat as Q6 applies:
the table was typed in, not generated from the scenario.

---

## Q8. Draft the award memo for the VP

Tool calls: `write_memo`

> Memo written to `data/outbox/analysis/award_memo_RFX_2026_PKG_017_docx.docx`. It covers qualification, the
> allocation, and the price-verification risk for Ganesh.

**Known issue:** the analyst's memo tool does not run the number check. The check exists in the award report, but the
analyst bypasses it. The numbers in this memo are not verified against the scenario. Before showing the memo, open it
and check every figure against Q1 to Q3. Use the Award tab's memo button for the verified version: it runs the check.

---

## Summary of what to show, and what to avoid

- **Show:** Q2 (the qualification split, with its tool calls), Q3 (the infeasible cap), Q4 (the pushback).
- **Show with a caveat:** Q5, Q7.
- **Avoid for now:** Q1 (the L06 error changes the answer), Q6 and Q8 (numbers not guaranteed by the tool).
