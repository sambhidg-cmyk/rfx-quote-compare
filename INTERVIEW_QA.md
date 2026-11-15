# Likely live questions and how the system handles them

**1. "Can the model make up a price?"**
Every extracted value must carry a verbatim snippet and a location. The snippet is checked against the source text
before the value is accepted. If it is not found, the model is told and asked again; if it still is not found, the
value is kept only as low confidence and goes to the review queue. A visually read value (a phone photo, a scanned
page with no text layer) is capped at medium confidence and is always flagged. The system never fills a blank with a
guess: a line the vendor did not price is recorded as not quoted.
*Limit to admit:* a snippet that exists in the text but supports the wrong number would pass the check. Verification
confirms the quote exists in the source, not that the model read it correctly. That is what the review queue is for.

**2. "How do you know the arithmetic is right?"**
The model is not allowed to do arithmetic. Totals, shares, savings and rankings come from deterministic code and from
read-only SQL. The memo is drafted by the model from a facts object, and every number in the draft is checked against
that object before the draft is accepted; a draft with a number that is not in the facts is rejected and retried, then
replaced by a template. The workbook uses real formulas, so anyone can check a total in Excel.
*Limit to admit:* the number check matches values, not meaning. A sentence can use correct numbers and still
misstate what they mean. The memo is marked as a draft for review for that reason.

**3. "What happens with a scanned PDF or a phone photo?"**
A PDF page with no usable text layer is sent to the model as an image. A photo is always an image. Both are marked
"visual only". Every value read this way is capped at medium confidence and goes to the review queue, and the Compare
tab shows the original page or photo beside the value. In the demo, the photo vendor's prices all appear in review.
*Limit to admit:* visual reading is the weakest step. The demo's photo prices are plausible, but they are not verified
against the source, and the award depends on some of them. Check them before signing.

**4. "Can it send the award to the vendors? Is this production?"**
No. Sending is simulated: messages are written to a folder with an `X-Simulated` header, and nothing leaves the
machine. Approval is required before anything is sent, and it locks the RFx. Clarification emails are drafts saved
to a folder, marked "DRAFT - not sent". It is a single-user prototype on SQLite with local files, and it depends on
a model API whose free-tier quota can run out mid-run. Those are the reasons it is not production.

**5. "What is your accuracy?"**
Answer with the eval numbers from the most recent run of `tests/eval/eval_extraction.py`, with the model and prompt
version named, and say which vendors were included. Do not quote an accuracy you have not just measured. The eval
scores prices, units, currency, freight and questionnaire presence against a ground truth that I generated, so
it measures the pipeline against a known answer, not against real vendors. Say that plainly. Known weak spots from
the most recent runs: USD-priced rolls and approximate ("around Rs X") prices, both of which the eval counts as misses.
