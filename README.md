# PAI scraper

Scrapes Gram Panchayat (GP) scores from the Government of India's
[Panchayat Advancement Index (PAI)](https://pai.gov.in) portal — overall and theme-wise
PAI scores for every GP, by State → District → Block, for both published years.

## Data

Covers **PAI 1.0 (2022-2023)** and **PAI 2.0 (2023-2024)**: overall PAI score plus nine
thematic scores/grades (T1 Poverty-Free … T9 Women-Friendly) per Gram Panchayat.

| year | states with data | districts | GPs | score rows (long) | blocks (data / no-data) |
| --- | --- | --- | --- | --- | --- |
| 2022-2023 | 29 / 34 | 753 | 169,673 | 1,696,730 | 5,917 / 1,282 |
| 2023-2024 | 33 / 34 | 741 | 183,011 | 1,830,110 | 6,103 / 1,032 |

Full per-state breakdown: [`docs/pai_summary_by_state.csv`](docs/pai_summary_by_state.csv)
(year totals: [`docs/pai_summary.csv`](docs/pai_summary.csv)). Regenerate with
`uv run scripts/data_summary.py`.

### Download — [doi:10.7910/DVN/FRUKWS](https://doi.org/10.7910/DVN/FRUKWS)

Everything is published on Harvard Dataverse — parsed data and raw page captures alike.
No account or API token needed:

| archive | contents | size | md5 |
| --- | --- | --- | --- |
| `pai_2022-2023_data.tar.gz` | parsed CSVs | 95.5 MiB | `92605f222e4dcdf4cfbdb1d0bd318cd5` |
| `pai_2023-2024_data.tar.gz` | parsed CSVs | 86.6 MiB | `ba3839ce006355c43253e89c57d07ee3` |
| `pai_2022-2023_html.tar.gz` | raw page captures (~13,400 pages) | 318.0 MiB | `9b9ebd5e9c1cf8d489eb76702622eec7` |
| `pai_2023-2024_html.tar.gz` | raw page captures | 300.6 MiB | `635073437396c306e81cf34d48c832b5` |

```bash
curl -L -o pai_2022-2023_data.tar.gz https://dataverse.harvard.edu/api/access/datafile/13999603
curl -L -o pai_2023-2024_data.tar.gz https://dataverse.harvard.edu/api/access/datafile/13999607
curl -L -o pai_2022-2023_html.tar.gz https://dataverse.harvard.edu/api/access/datafile/13999606
curl -L -o pai_2023-2024_html.tar.gz https://dataverse.harvard.edu/api/access/datafile/13999608
```

Each `_data` archive contains, under `consolidated/`:

| file | grain | description |
| --- | --- | --- |
| `gp_metadata_<year>.csv` | one row / GP | identity & location: `state, district, block, gp_name, gp_code, scorecard_url`, … |
| `gp_scores_long_<year>.csv` | one row / GP × theme | tidy scores: `theme_slug, theme_header, score, grade, band` (slug `overall_pai_score` = overall) |
| `gp_scores_wide_<year>.csv` | one row / GP | wide form: one `<theme>_score` / `_grade` / `_band` column per theme |
| `block_manifest_<year>.csv`, `dropdown_inventory_<year>.csv` | — | scrape provenance / the option universe |

Archives also include the raw per-block CSV/JSON tree under `blocks/` and a `SUMMARY.md`.
The consolidated CSVs are rebuilt from the per-block files (de-duplicated current state),
not the append-only logs.

The `_html` archives hold the rendered HTML for every block page, as scraped.

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

Run the same command again — blocks with `DONE.json` are skipped. Retry flags:

- `--retry-empty` — re-do blocks that finished with zero GP rows.
- `--retry-no-data` — re-verify blocks previously marked "no data available". The server returns an
  identical "not available" alert both for genuinely empty blocks and (spuriously) under load, so a
  no-data result is confirmed by re-searching `--no-data-confirm` times (default 2) before it is
  accepted; if data appears it is recovered, and an indeterminate recheck is retried (never a
  terminal false negative).
- `--overwrite` — rescrape matching blocks unconditionally.

## Outputs

```text
data/
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
uv run scripts/pai_rebuild_index.py --out data   # de-duplicated global indexes
uv run scripts/data_summary.py                        # coverage tables -> docs/pai_summary*.csv
uv run scripts/scrape_progress.py --year 2023-2024    # progress from block_manifest.csv
uv run scripts/pai_inspect_output.py --out data  # quick counts
uv run scripts/build_release.py                       # release archives -> dist/
```

## Develop

```bash
make sync     # uv sync + chromium
make format   # ruff format + ruff check --fix
make lint     # ruff format --check + ruff check
make test     # pytest
```
