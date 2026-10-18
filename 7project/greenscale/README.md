# GreenScale

Flexible Kubernetes batch-job scheduler that plans jobs around Norwegian (NO2) electricity spot prices.

Run everything from this directory (`7project/greenscale`).

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # then edit secrets locally; .env is git-ignored
docker compose up -d db
python -m uvicorn app.main:app --reload
```

- Health: http://127.0.0.1:8000/health
- Docs: http://127.0.0.1:8000/docs

## Tests

```bash
pytest
```

## Price adapter (`app/prices.py`)

`fetch_day(day, area="NO2")` downloads the published day-ahead spot prices for one
calendar day from [hvakosterstrommen.no](https://www.hvakosterstrommen.no/strompris-api)
and returns a list of `PriceSlot(start, end, nok_per_kwh)` sorted by start time.

- Intervals are taken from the provider's ISO 8601 `time_start` / `time_end`
  fields; the adapter never assumes "array index == hour".
- All timestamps are timezone-aware. Naive timestamps or `end <= start` raise `ValueError`.
- Tomorrow's file is published no earlier than ~13:00 local time, so an HTTP 404
  for tomorrow is normal and is raised as `httpx.HTTPStatusError`. Callers should
  keep the job pending rather than invent prices.
- Unit tests use `httpx.MockTransport` and `fixtures/sample_prices.json`; no network needed.

Live check against today's NO2 prices:

```bash
python -c "from app.prices import fetch_day; from datetime import date; print(fetch_day(date.today())[:2])"
```

These are public spot prices, not GCP's actual electricity cost. Describe results as
"modelled exposure to the NO2 price signal", not as money saved on Google infrastructure.

## Runtime predictor (`app/predictor.py`)

`predict_runtime(workload_type, input_size_mb) -> (seconds, source)` asks Gemini
for a rough runtime estimate from job metadata only. The LLM never receives API
keys, job descriptions or customer data, and it never chooses the schedule; the
deterministic scheduler does that using the returned seconds.

Every call returns a usable number. `source` tells you where it came from:

| `source`             | Meaning                                                              |
|----------------------|----------------------------------------------------------------------|
| `llm`                | Gemini returned an integer within 1..14400 seconds                   |
| `fallback_no_key`    | `GEMINI_API_KEY` is not set; conservative default used               |
| `fallback_llm_error` | Request failed (quota, timeout, bad JSON, out-of-range); default used |

Fallback defaults: `sleep` 60 s, `factorial` 90 s, `csv_summary` 120 s.
Failures are logged by exception type only, never with the prompt, response or key.

Configuration (local `.env`, never committed):

```
GEMINI_API_KEY=your_key_from_aistudio.google.com
GEMINI_MODEL=gemini-3.8-flash
```

Live check (prints one row per workload type):

```bash
python scripts/try_llm.py
```

Without a key every row shows `fallback_no_key`. Unit tests in
`tests/test_predictor.py` use a fake client and need no key or network.

LLM estimates are uncertain guesses from metadata. Record predicted vs. actual
runtime once jobs really execute, and do not claim forecast accuracy without
that comparison.
