# What I decided and what I left out

## The problem as I framed it

A buyer sends one RFx to several vendors and gets replies in mixed shapes: spreadsheets, PDFs, prose, photos, emails. I need one comparable price per line, a record of where each number came from, and a clear view of what is safe to sign.

## Key decisions and why

**Category.** Corrugated packaging: enough units to make normalization real, small enough to test end to end.

**Extraction.** Readers turn each file into located text or images. One agent sees all of a vendor's files together, because "same as last year" can only be resolved against an attached rate sheet. Output must pass a schema-checked tool call.

**Normalization in code.** The model reports prices as written. Code converts only what I can verify: USD at a fixed rate, pack of 100 to piece. Everything else goes to review.

**Trust.** A value counts only if its quoted snippet appears in the named source. Otherwise it is retried once, then flagged low. Image-read values are capped at medium and always reviewed.

**Review queue.** The buyer accepts or overrides each item. Overrides recompute totals and are logged.

**Analyst.** A tool-using agent over read-only SQL and fixed award tools. It does no arithmetic. Memo numbers must appear in the scenario output, or the template replaces the draft.

## What I deliberately left out

Real email, authentication, multiple users, currencies beyond INR and USD, OCR training, a vendor portal, supplier history, and a landed-cost view. No response quantifies freight, so I show ex-freight only.

## What's shaky

- **The ground truth is mine.** I generated the documents and answers, so the eval measures the pipeline against a known answer, not real vendors.
- **Photo reads** are unverified against text, and the award depends on some of them.
- **A prompt change backfired.** My v3 rules to fix four misses made four of five vendors fail schema validation, so I reverted to v2. The four misses are still open.
- **Freight and discount** are still missed on some files.
- **Simple rules** decide hard requirements, and the vendor cap is a greedy heuristic.
- **A free model tier**, which ran out mid-run more than once.

## The better problem

The interesting problem is the vendor clarification loop. Most value sits in the review queue, and a clear question answered quickly would confirm more prices than any extraction gain. Two others matter more: upstream, RFx lines that allow two readings ("per 100 pieces" or "per piece") cause most unit errors; downstream, a VP has to defend a split to finance, and my memo is only a first attempt.
