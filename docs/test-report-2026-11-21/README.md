# Hero Hotel Test Report — 2026-11-21

English edition of the Milestone 2 HTTP concurrency, stress and data-consistency
test report for the Hero Hotel booking system.

## Files

| File | Description |
| --- | --- |
| `Test-Report-2026-11-21.pdf` | Final report, 25 pages, with cover, contents, 10 charts and 6 screenshots |
| `Test-Report-2026-11-21.docx` | Editable version; the contents page is a Word field and can be refreshed |
| `Test-Report-2026-11-21.html` | Source HTML consumed by `loadtest/tools/html_to_docx.py` |
| `assets/charts/` | 10 charts generated from the raw load-test JSON by `loadtest/tools/make_charts.py` |
| `assets/screenshots/` | 6 browser screenshots from the same build and test database |

## Test code

The harness lives in the repository root at `loadtest/`:

- `loadtest/run_conflict.py` — same-room contention, idempotency and false sold-out probe
- `loadtest/run_stress.py` / `run_ramp.py` — S-browse, S-book, S-mixed and stepwise ramps
- `loadtest/run_seed_check.py` — 9 seed-data checks guarding the F1 fix
- `loadtest/run_dbcheck.py` — I1–I15 consistency SQL
- `loadtest/lib/`, `loadtest/scenarios/` — HTTP flow, metrics, fixtures and scenarios
- `loadtest/results/` — raw CSV/JSON, pytest baseline, F1 evidence, final check output

## Regeneration

```powershell
# 1) English charts
python loadtest/tools/make_charts.py --lang en --results loadtest/results `
  --output "<this directory>/assets/charts"

# 2) DOCX and PDF
python loadtest/tools/html_to_docx.py --html "<this directory>/Test-Report-2026-11-21.html" `
  --font "Segoe UI" --lang en --output "<this directory>/Test-Report-2026-11-21.docx"

# 3) Optional document self-check
python "<docx-skill>/scripts/postcheck.py" "<this directory>/Test-Report-2026-11-21.docx"
```

The layout script reads the semantic structure of the English HTML only; narrative,
table values and figure captions come from that file. Re-run the commands after
editing the HTML to refresh the PDF.
