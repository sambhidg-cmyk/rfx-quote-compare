# Demo script: the analyst conversation (about 7 minutes)

Before you start: run the one-click demo (Create RFx tab → **Load demo**, or `python3 scripts/run_demo.py`) so the
database, the Compare tab and the Award tab are populated. Open the **Ask** tab. Ask the questions in the order below.
Each question is chosen to show one behaviour. Watch the tool-call expander under each answer: the numbers come from there.

## The eight questions

**1. "Who is cheapest overall, like-for-like?"** (about 1 min)
Why ask: the basic comparison, and the first place the system has to choose its basis. It should compare only the
lines every responding vendor priced, say so, and state that prices are ex-tax and ex-freight. Point out the
`basket_summary` tool call and the total it returns.

**2. "Split the award: cheapest per line, but only among vendors who cleared the quality questionnaire."** (about 1 min)
Why ask: this is the VP's real question. The answer has to pass through the hard requirements (in-house ECT/BCT
testing, valid ISO 9001). It should call `hard_requirements` before naming anyone as qualified, and it should say
which vendors were excluded and why. If the model claims a vendor is qualified without a tool call, that is a failure.

**3. "What if we cap any vendor at 40%?"** (about 1 min)
Why ask: a constraint question with a mathematical answer. With two eligible vendors, shares must sum to 100%, so
a 40% cap cannot hold. The system should say the cap is infeasible and not invent a split. Check the `scenario`
result before you speak: `feasible` should be false.

**4. "Which vendor is cheapest on the full 30-line basket?"** (about 1 min)
Why ask: the pushback case. Not every vendor priced every line, so a full-basket total is unfair to partial
bidders. The answer should refuse the unfair comparison, name the partial vendors, and point to the like-for-like view.
This is the question to use if someone asks why the system does not just sum the sheets.

**5. "Which prices should I double-check before signing?"** (about 1 min)
Why ask: the trust question. The answer should come from `confirmation_list`, name each unconfirmed price with its
line, vendor and confidence, and say which ones feed the award. Point out that photo-read prices are always
flagged, because the system cannot verify them against text.

**6. "Chart the award value by vendor from the current scenario."** (about 45 s)
Why ask: shows a rendered chart produced by a tool from a stored table, not a description of one. The chart appears
under the answer and the table it came from is in the tool-call expander.

**7. "Export the award lines to Excel."** (about 30 s)
Why ask: the export is a real file. Open the download and show the Award sheet: line totals and shares are formulas,
not pasted numbers, and the Sources sheet carries each value's file and locator.

**8. "Draft the award memo for the VP."** (about 1 min)
Why ask: the memo is written by the model, but every number in it must appear in the scenario output. The memo
footer says whether the model draft passed the number check or fell back to the template. Say this out loud, since
it is the answer to "can it make up numbers?".

## If something goes wrong on stage

- A question fails or times out: the answer appears as an error in the chat. Ask the next question; the session
  keeps its memory.
- Cached answers: the demo replays cached answers (labelled "cached") when the data has not changed. Say so; it
  is the design, not a shortcut.
- No network: run the demo with cached answers only. Without a key and without the cache, the Ask stage is
  skipped and says so.
