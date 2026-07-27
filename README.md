# Deen Capital — live cloud dashboard

An always-on performance dashboard for the Deen Capital halal systematic strategy.
It rebuilds itself **once a day on GitHub's servers** (no local machine required) and is
served as a static page via GitHub Pages, viewable on any device.

**Live page:** `https://<your-username>.github.io/<repo-name>/`

## How it updates
A scheduled GitHub Action (`.github/workflows/update.yml`) runs every weekday ~1.5–2.5h
after the US close. Each run:
1. reads `data/paper_state.json` (sleeve weights + benchmark levels as of the last mark),
2. fetches adjusted daily closes for every held name + benchmark via `yfinance`,
3. marks NAV forward using the **same daily-return math** as the local `paper_tracker.mark()`
   (buy-and-hold intra-sleeve drift; dividend-inclusive via adjusted prices),
4. appends to `data/paper_track.csv` and regenerates `index.html`,
5. commits the refreshed data + page back to the repo, which Pages serves.

A failed price fetch never blanks the page — it just rebuilds from the last good data.

## Monthly rebalance sync (manual, ~once a month)
The monthly **rebalance** is produced locally (it needs the alpha/screen code, which is kept
off GitHub on purpose). After the local monthly run, copy the two refreshed files into this
repo's `data/` folder and commit:
- `paper_state.json`  (new sleeve weights + reset values)
- `target_book.json`  (new holdings, for the "what's in the book" panel)

The daily marking then continues automatically from the new state.

## Files
| Path | Purpose |
|---|---|
| `build_dashboard.py` | fetch + mark + regenerate `index.html` (`--offline` skips the fetch) |
| `index.html` | the generated dashboard (served by Pages) |
| `data/paper_track.csv` | the accumulating daily NAV record (Deen + QQQ/SPUS/SPY/HLAL) |
| `data/paper_state.json` | sleeve weights + benchmark levels; the marking seed |
| `data/target_book.json` | current holdings, for display |
| `data/tear_sheet_stats.json` | static 2010–2025 backtest stats |
| `.github/workflows/update.yml` | the daily cloud job |

## Disclosures
Paper NAV is **simulated** (modeled from closing prices, not broker fills), gross of costs.
The 2010–2025 figures are **hypothetical / backtested**, internally replicated (r = 0.9998 vs
the production engine), no financial-statement audit. Past performance does not predict future
results. For the operator's use — not an offer or solicitation.
