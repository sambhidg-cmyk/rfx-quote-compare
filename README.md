# rfx-quote-compare

A prototype for comparing vendor responses to a corrugated-packaging RFx. It reads each vendor's files in whatever
form they arrive, turns the prices into one comparable unit, shows where every number came from, and lets a buyer
ask questions over the comparison and draft an award.

Everything in the demo is synthetic: the buyer, the vendors, the certificates and the prices. Email is simulated.
Nothing is sent over a network except calls to the model API.

Read these first:
- [NOTE.md](NOTE.md): what I decided, what I left out, and what's shaky
- [DEMO_SCRIPT.md](DEMO_SCRIPT.md): the analyst conversation, in order, with reasons for each question
- [INTERVIEW_QA.md](INTERVIEW_QA.md): likely questions and how the system handles them

## Run it

Requires Python 3.9 or later.

```bash
python3 -m pip install -r requirements.txt
export GEMINI_API_KEY=...          # your key; never commit it
python3 -m pytest -q               # 131 tests, no network calls
streamlit run app/streamlit_app.py # open http://localhost:8501
```

In the app, press **Load demo (one click)** on the Create RFx tab. It resets the database and walks the stages in
order: create the RFx, send it to five vendors, receive their replies, extract each one, compare, and ask. Each step
says whether it made live model calls or used the cache. A rerun is fast because extraction results and analyst
answers are cached by content. Pass `--fresh` to `scripts/run_demo.py` to make every call live.

To run the same thing from the terminal:

```bash
python3 scripts/run_demo.py            # cached where possible
python3 scripts/run_demo.py --fresh    # every model call live
```

## The tabs

- **Create RFx:** a chat co-pilot (needs `ANTHROPIC_API_KEY`) or the loaded 30-line RFx. Approve writes a PDF and an Excel file to `data/outbox/`.
- **Inbox:** send to vendors (simulated), simulate replies arriving, and extract them with the model. A progress bar shows each batch.
- **Compare:** the price matrix with lowest-price highlighting, coverage, and totals on RFx quantities. Click a price to see the vendor's original value, the normalization steps, the source snippet, and the original page or photo. Also: questionnaire pass/fail on hard requirements, attached certificates with expiry flags, and the review queue (accept or override, with an audit trail, and a draft clarification email).
- **Award:** an award scenario builder (qualification filter, vendor cap, ex-freight basis, USD and unconfirmed-value filters). It shows savings, risks and exclusions, and exports a comparison workbook and a memo.
- **Ask:** a chat with the analyst over the comparison. It calls tools for every figure and cites its sources.

## How it's built

```
core/readers        file router: xlsx, pdf, docx, text, images -> located text or images
core/extract        one agent per vendor response; schema-checked output; snippet verification; cache
core/schema.py      canonical model: RFx, line items, quotes, normalized quotes, answers, review items
core/normalize      stated price -> RFx unit, INR ex-tax (code, not model)
core/compare        matrix, totals, trust counts, review actions, audit trail
core/award          scenario engine (cheapest per line, eligibility, cap, filters)
core/award_report   savings, risks, workbook, memo with a number check
core/analyst        tool-using agent over read-only SQL and award tools
core/channel        simulated email: writes .eml files, records sends and replies
core/demo           the one-click demo, staged and cached
app/                Streamlit tabs
scripts/            generate_dataset, run_pipeline, run_demo, ask_analyst, load_cached_extractions
tests/              unit tests; tests/eval/eval_extraction.py scores extraction against ground truth
```

## Models

Extraction and the analyst default to `gemini-3.1-flash-lite-preview`. Set `EXTRACT_MODEL`, `ANALYST_MODEL` or
`MEMO_MODEL` to change them. `EXTRACT_PROVIDER=anthropic` switches extraction to Claude. The free Gemini tier has
daily limits, and a full extraction run uses about 20 calls. If calls fail, the pipeline retries with backoff and
then reports the failing vendor without stopping the others.

## Evaluation

`python3 tests/eval/eval_extraction.py --cache-only` scores the cached extraction against
`data/ground_truth.json` and lists every miss. The ground truth is generated with the dataset, so this measures the
pipeline against a known answer, not against real vendors. Current known misses are listed in NOTE.md.

## Known limitations

- Vendor A's L06 price is wrong: a pallet price is attached to the box line. The snippet check passes because the
  text exists in the file, just on another row. This inflates Vardhan's like-for-like total in the demo.
- Some analyst answers quote counts that differ from the database. The Compare and Award tabs are the source of truth.
- Freight and discount are missed on some files.
- Photo-read prices are never verified against text; they always go to review.
- Email is simulated and nothing is sent. Clarification emails are drafts saved to `data/outbox/drafts/`.
- Single user, SQLite, local files. Not designed for multiple users or production.

## Data

- `data/rfx/rfx.json`: the 30-line RFx
- `data/responses/`: what each vendor sent (generated by `scripts/generate_dataset.py`)
- `data/ground_truth.json`: the answers used by the eval. Nothing under `app/` or `core/` reads it.
- `data/cache/`: cached model output, keyed by content. Safe to delete; the demo will be slower.
