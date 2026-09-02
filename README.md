# Panchayat Advancement Index data

This repository releases analysis-ready Gram Panchayat scores from the Government of India's
[Panchayat Advancement Index (PAI)](https://pai.gov.in) and contains the scraper that rebuilds
them. It covers overall and theme-wise scores by State, District, and Block for both published
PAI vintages.

## Versioned data package

Each data release uses a `v*` tag to pin three files under `data/release/`:

| file | grain | purpose |
| --- | --- | --- |
| `pai_gp_scores.parquet` | one GP × PAI vintage | stable identifiers and ten numeric scores |
| `pai_gp_universe.parquet` | one official GP × PAI vintage | denominator, LGD names/codes, and handler provenance |
| `MANIFEST.json` | one release | version, schemas, row counts, byte sizes, and SHA-256 checksums |

The score table deliberately omits scraper paths, raw display strings, and redundant grades.
The raw HTML and resumable cache remain in Dataverse rather than enlarging every Git checkout.
See [`DATA_DICTIONARY.md`](DATA_DICTIONARY.md) for column definitions and
[`CHANGELOG.md`](CHANGELOG.md) for corrections between tags.

```python
import pyarrow.parquet as pq

scores = pq.read_table("data/release/pai_gp_scores.parquet")
universe = pq.read_table("data/release/pai_gp_universe.parquet")
```

## Data

Covers **PAI 1.0 (2022-2023)** and **PAI 2.0 (2023-2024)**: overall PAI score plus nine
thematic scores/grades (T1 Poverty-Free … T9 Women-Friendly) per Gram Panchayat. PAI 2.0
is collected from the current unified `TW-GP.aspx` page, whose legacy table includes GP LGD
codes; the retired flat `TW-GP-New.aspx` route is incomplete and is not used.

| year | states with data | districts | GPs | score rows (long) | blocks (data / no-data) |
| --- | --- | --- | --- | --- | --- |
| 2022-2023 | 29 / 34 | 753 | 169,673 | 1,696,730 | 5,917 / 1,282 |
| 2023-2024 | 33 / 34 | 741 | 183,011 | 1,830,110 | 6,103 / 1,032 |

Full per-state breakdown: [`docs/pai_summary_by_state.csv`](docs/pai_summary_by_state.csv)
(year totals: [`docs/pai_summary.csv`](docs/pai_summary.csv)). Regenerate with
`uv run scripts/data_summary.py`.

### Historical full archives — [doi:10.7910/DVN/FRUKWS](https://doi.org/10.7910/DVN/FRUKWS)

The original parsed data and raw page captures are published on Harvard Dataverse. These are
the larger provenance/recovery archives; the compact canonical tables live in tagged releases.
No account or API token is needed:

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

The published archives above are the original `.tar.gz` release and include the historical
per-block cache. `build_release.py` now emits a much smaller `.tar.zst` analysis-data release
containing only typed Parquet tables and their checksummed collection manifest by default.
The consolidated CSVs are rebuilt from the per-block files (de-duplicated current state),
not the append-only logs.

The `_html` archives hold the rendered HTML for every block page, as scraped.

### Notes

- **West Bengal** has no GP scores in either year (the portal returns "Details are not available"); a few small UTs/states have data only in 2023-2024.
- Reproducible end-to-end from [`scripts/pai_scraper_resumable.py`](scripts/pai_scraper_resumable.py) → [`scripts/data_summary.py`](scripts/data_summary.py) → [`scripts/build_release.py`](scripts/build_release.py).

## Install

Uses [uv](https://docs.astral.sh/uv/) (Python ≥ 3.14):

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

The final PAI 2.0 state collections use independent resumable staging roots so one process
cannot clobber the other:

```bash
uv run scripts/pai_scraper_resumable.py --years 2023-2024 --state-values 9 \
  --out runs/pai2_up --headless --delay 0.5 --block-third-party \
  --baseline-counts-from data --universe-exceptions config/gp_universe_exceptions.csv \
  --gp-name-links config/gp_name_links.csv \
  --expected-state-gps "Uttar Pradesh=57678"
uv run scripts/pai_scraper_resumable.py --years 2023-2024 --state-values 8 \
  --out runs/pai2_rajasthan --headless --delay 0.5 --block-third-party \
  --baseline-counts-from data --universe-exceptions config/gp_universe_exceptions.csv \
  --gp-name-links config/gp_name_links.csv \
  --expected-state-gps "Rajasthan=11037"
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

Every block also caches the official `Y_GPs_By_LGD_Block` JSON response and request provenance.
A block is marked done only when independently repeated score retrievals are identical and their
GP-code set exactly equals that official handler universe. Prior-release counts are diagnostics in
`block_count_audit.csv`, not a completeness gate. Any intentional unscored GP or ambiguous
PAI 1.0 name-to-code link requires a manually reviewed CSV row with evidence in `config/`.
Historical PAI 2.0 pages contain three blocks where two distinct GPs share the same displayed
name. Those six identities are recovered only through reviewed exact ten-score-vector links to
current code-bearing official scorecards in `config/gp_score_vector_links.csv`; row order is never
used as a join key.

The denominator is also collectible once nationwide for every configured PAI vintage, independently
of the slower browser scrape:

```bash
uv run scripts/pai_collect_universe.py --out runs/pai_universe \
  --years 2022-2023 2023-2024 --delay 1
```

This resumable collection preserves every official district, block, and GP handler response plus
request URL, parameters, retrieval time, HTTP status, byte count, and SHA-256 checksum. The final
`gp_universe.parquet` is keyed by `(year, gp_code)` and retains the full State/District/Block
hierarchy. Exact trailing `[null, null]` handler sentinels are excluded from the typed hierarchy,
left untouched in the raw JSON, and counted in `collection_manifest.json`; other malformed nulls
remain fatal.

## Outputs

The scraper writes one directory per block:

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
│       ├── source/gp_universe.json
│       ├── source/gp_universe_provenance.json
│       ├── html/page_001.html
│       ├── data_wide.csv        # authoritative per-block data
│       ├── metadata.csv
│       └── scores_long.csv
└── 2023-2024/
```

The per-block CSVs are resumable parsed cache, while rendered HTML is the raw source cache.
Canonical analysis data are typed Parquet generated from that cache after hard contracts pass:

```bash
uv run scripts/pai_rebuild_index.py --data-dir runs/pai2_up \
  --expected-state-gps "Uttar Pradesh=57678"
uv run scripts/pai_rebuild_index.py --data-dir runs/pai2_rajasthan \
  --expected-state-gps "Rajasthan=11037"
uv run scripts/data_summary.py                        # coverage tables -> docs/pai_summary*.csv
uv run scripts/scrape_progress.py --data-dir runs/pai2_up --year 2023-2024
uv run scripts/pai_inspect_output.py --out data       # quick counts
uv run scripts/build_release.py --universe-data-dir runs/pai_universe  # -> dist/
```

Each `derived/` directory contains `gp_scores_wide.parquet` (canonical one row per GP),
`gp_metadata.parquet`, tidy `gp_scores_long.parquet`, the official denominator/linkage table
`gp_universe.parquet`, typed block/dropdown manifests, and
`collection_manifest.json` with schemas, counts, official controls, and SHA-256 checksums.
Identifiers remain strings; scores and count/order fields are numeric. The build asserts unique
GP-year keys (LGD code where present, documented location/name fallback otherwise), exactly ten
unique scores per GP, one overall score, scores in `[0, 100]`, and row conservation against every
successful `DONE.json`. The universe table requires unique `(year, gp_code)` keys and exact
reconciliation to the score metadata. All large CSV conversion and global validation runs in
bounded Arrow record batches rather than materializing the national long table in memory.

`build_release.py` emits a compact data-only archive by default. Use `--include-cache` only when
you intentionally want a much larger recovery archive containing the per-block cache. Raw HTML is
always a separate archive and never part of the analysis-data bundle.
PAI 2.0 release creation requires all 33 State/UT totals and the 259,867 national total in the
Ministry's final August 2026 table by default. A deliberately scoped state release must pass
`--allow-partial` and should also supply its `--expected-state-gps` control.

### Compact storage

That tree is 6.8 GB in 79,815 files, none of it compressed, and about a third of it is a
copy: `gp_scores_long.csv` is a concatenation of the 12,464 per-block `scores_long.csv`.
`make compact` collapses it to **314 MB** without losing anything:

```bash
make verify                    # prove the global CSVs rebuild from the per-block files
make compact                   # -> data/blocks_<year>.tar.zst, html_<year>.tar.zst, *.parquet
make data-status               # what form each year is in
make expand YEAR=2022-2023     # restore a byte-identical tree (needed to resume a scrape)
```

| | before | after |
| --- | --- | --- |
| per-block csv+json | 2.35 GB | 51.8 MB (`blocks_<year>.tar.zst`) |
| html page captures | 2.20 GB | 244.8 MB (`html_<year>.tar.zst`) |
| `gp_scores_long.csv` + `gp_metadata.csv` | 2.15 GB | 0 — rebuilt on demand |
| `block_manifest`, `dropdown_inventory` | 57 MB | 3.2 MB (parquet) |

Solid `.tar.zst` rather than per-file `.gz`: these files are near-duplicates of each other,
and zstd's long window compresses across them where gzip's 32 KB window cannot — 42x against
about 8x. Every reader goes through `scripts/pai_stores.py`, so the tools work against either
form and the scraper keeps writing the live tree regardless.

Nothing is deleted until its replacement has been written and read back with every file's
sha256 matched against `data/compact_<year>.json`; `make compact` refuses to start unless
`make verify` shows the globals rebuild row-for-row. Debug screenshots (`debug/*.png`) are
dropped by default — pass `--keep-debug` to archive them — and every dropped path is listed
in the manifest.

## Develop

```bash
make sync     # uv sync + chromium
make format   # ruff format + ruff check --fix
make lint     # ruff format --check + ruff check
make test     # pytest
make check    # lint + test
make data-package DERIVED=/path/to/validated/derived
make verify-data
make release-check VERSION=0.1.0
```

`release-check` does not create a tag. It requires a clean `main` worktree, a dated changelog
entry matching `pyproject.toml`, a version not already tagged, and a byte-for-byte valid data
manifest. A release still requires independent review and green CI before a human creates and
pushes the annotated tag.

Requires Python 3.14 or newer: the archives use `compression.zstd`, which entered the
standard library in 3.14, so the packaging tools stay dependency-free.
