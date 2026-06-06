# PAI scraper

Scrapes Gram Panchayat (GP) scores from the Government of India's
[Panchayat Advancement Index (PAI)](https://pai.gov.in) portal — overall and theme-wise
PAI scores for every GP, by State → District → Block, for both published years.

## Data

Covers **PAI 1.0 (2022-2023)** and **PAI 2.0 (2023-2024)**: overall PAI score plus nine
thematic scores/grades (T1 Poverty-Free … T9 Women-Friendly) per Gram Panchayat.

| year | states with data | districts | GPs | score rows (long) | blocks (data / no-data) |
| --- | --- | --- | --- | --- | --- |
| 2022-2023 | 29 / 34 | 753 | 125,551 | 1,255,510 | 5,107 / 2,092 |
| 2023-2024 | 33 / 34 | 741 | 114,583 | 1,145,830 | 4,894 / 2,241 |

Full per-state breakdown: [`docs/pai_summary_by_state.csv`](docs/pai_summary_by_state.csv)
(year totals: [`docs/pai_summary.csv`](docs/pai_summary.csv)). Regenerate with
`python scripts/data_summary.py`.

### Download — parsed data (GitHub Release [`data-v1`](https://github.com/in-rolls/pai/releases/tag/data-v1))

- [`pai_2022-2023_data.tar.gz`](https://github.com/in-rolls/pai/releases/download/data-v1/pai_2022-2023_data.tar.gz) (~72 MB)
- [`pai_2023-2024_data.tar.gz`](https://github.com/in-rolls/pai/releases/download/data-v1/pai_2023-2024_data.tar.gz) (~56 MB)

Each archive contains, under `consolidated/`:

| file | grain | description |
| --- | --- | --- |
| `gp_metadata_<year>.csv` | one row / GP | identity & location: `state, district, block, gp_name, gp_code, scorecard_url`, … |
| `gp_scores_long_<year>.csv` | one row / GP × theme | tidy scores: `theme_slug, theme_header, score, grade, band` (slug `overall_pai_score` = overall) |
| `gp_scores_wide_<year>.csv` | one row / GP | wide form: one `<theme>_score` / `_grade` / `_band` column per theme |
| `block_manifest_<year>.csv`, `dropdown_inventory_<year>.csv` | — | scrape provenance / the option universe |

Archives also include the raw per-block CSV/JSON tree under `blocks/` and a `SUMMARY.md`.
The consolidated CSVs are rebuilt from the per-block files (de-duplicated current state),
not the append-only logs.

### Download — raw HTML page captures (Dataverse)

The rendered HTML for every block page (~11,400 pages) is archived on Dataverse:
**[doi:10.7910/DVN/FRUKWS](https://doi.org/10.7910/DVN/FRUKWS)**
(`pai_2022-2023_html.tar.gz`, `pai_2023-2024_html.tar.gz`).

### Notes

- **West Bengal** has no GP scores in either year (the portal returns "Details are not available"); a few small UTs/states have data only in 2023-2024.
- Reproducible end-to-end from [`scripts/pai_scraper_resumable.py`](scripts/pai_scraper_resumable.py) → [`scripts/data_summary.py`](scripts/data_summary.py) → [`scripts/build_release.py`](scripts/build_release.py).

## Install

Uses [uv](https://docs.astral.sh/uv/) (Python ≥ 3.13):

```bash
uv sync
uv run playwright install chromium
```

## Smoke test

```bash
uv run scripts/pai_scraper_resumable.py \
  --years 2022-2023 --state-contains Bihar --limit-districts 1 --limit-blocks 3
```

If Chromium is flaky, try installed Chrome with `--browser-channel chrome`.

## Full run

```bash
uv run scripts/pai_scraper_resumable.py --years 2022-2023 2023-2024 --headless --delay 1.5
```

## Resume

Run the same command again — blocks with `DONE.json` are skipped. Use `--retry-empty` to
retry blocks that completed with zero GP rows, or `--overwrite` to rescrape matching blocks.

## Outputs

```text
test_data/
├── pai_scrape.log
├── block_manifest.csv          # append-only scrape log
├── dropdown_inventory.csv      # append-only option universe
├── 2022-2023/
│   └── State__code/District__code/Block__code/
│       ├── context.json
│       ├── DONE.json
│       ├── FAILED.json          # only if failed
│       ├── html/page_001.html
│       ├── data_wide.csv        # authoritative per-block data
│       ├── metadata.csv
│       └── scores_long.csv
└── 2023-2024/
```

The **per-block files are the source of truth.** The consolidated `gp_metadata.csv` /
`gp_scores_long.csv` are *generated on demand* from them (the scraper no longer writes them
inline), so they can never drift or duplicate:

```bash
uv run scripts/pai_rebuild_index.py --out test_data   # de-duplicated global indexes
uv run scripts/data_summary.py                        # coverage tables -> docs/pai_summary*.csv
uv run scripts/scrape_progress.py --year 2023-2024    # progress from block_manifest.csv
uv run scripts/pai_inspect_output.py --out test_data  # quick counts
uv run scripts/build_release.py                       # release archives -> dist/
```

## Develop

```bash
make sync     # uv sync + chromium
make format   # ruff format + ruff check --fix
make lint     # ruff format --check + ruff check
make test     # pytest
```
