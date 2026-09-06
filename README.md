# Panchayat Advancement Index data

[![Dataset on Hugging Face](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-soodoku%2Fpai-blue)](https://huggingface.co/datasets/soodoku/pai)

This repository releases analysis-ready Gram Panchayat scores from the Government of India's
[Panchayat Advancement Index (PAI)](https://pai.gov.in) and contains the scraper that rebuilds
them. It covers overall and theme-wise scores by State, District, and Block for both published
PAI vintages. **Get the data:** the release table and provenance archives are on Hugging Face at
[`soodoku/pai`](https://huggingface.co/datasets/soodoku/pai); the same table is pinned in each
`v*` tag here under `data/release/`.

## Versioned data package

Each data release uses a `v*` tag to pin two files under `data/release/`:

| file | grain | purpose |
| --- | --- | --- |
| `pai_gp.parquet` | one hierarchy GP × PAI vintage | denominator, score availability, and ten nullable scores |
| `MANIFEST.json` | one release | version, schemas, row counts, byte sizes, and SHA-256 checksums |

The table retains GPs without a published score and distinguishes them with `score_available`;
their score columns remain null, never zero. It omits scraper paths, raw display strings, and
redundant grades.
The raw HTML and per-block cache live on Hugging Face rather than enlarging every Git checkout.
See [`DATA_DICTIONARY.md`](DATA_DICTIONARY.md) for column definitions and
[`CHANGELOG.md`](CHANGELOG.md) for corrections between tags.

```python
import pyarrow.parquet as pq

gps = pq.read_table("data/release/pai_gp.parquet")
scores = gps.filter(gps["score_available"])
```

## Data

Covers **PAI 1.0 (2022-2023)** and **PAI 2.0 (2023-2024)**: overall PAI score plus nine
thematic scores/grades (T1 Poverty-Free … T9 Women-Friendly) per Gram Panchayat. PAI 2.0
is collected from the current unified `TW-GP.aspx` page, whose legacy table includes GP LGD
codes; the retired flat `TW-GP-New.aspx` route is incomplete and is not used.

| year | states with data | districts | GPs | score rows (long) | blocks (data / no-data) |
| --- | --- | --- | --- | --- | --- |
| 2022-2023 | 29 / 34 | 753 | 216,256 | 2,162,560 | 6,318 / 872 |
| 2023-2024 | 33 / 34 | 741 | 259,867 | 2,598,670 | 6,766 / 369 |

### Indicator framework

Each theme score aggregates equally weighted indicators that the Gram Panchayat reports on
the portal and the Gram Sabha and district validate. [`docs/pai_indicators.csv`](docs/pai_indicators.csv)
lists every indicator per theme and version with its numerator and denominator, as fetched
from the portal's [indicator browser](https://pai.gov.in/MMS/Indicator/Theme-Indicators.aspx?t=8&s=2)
by `uv run scripts/pai_indicators.py`, which asserts the column contract, key uniqueness
and the counts below. An indicator used in several themes is listed once per theme, which
is how the Ministry arrives at 516 and 150. "Rates" have a denominator distinct from the
numerator; "checks" are yes/no questions; the split is derived from the portal's columns,
not from a Ministry label. PAI 2.0 cut the framework from 516 rows to 150, and in Good
Governance replaced most rates with checks; few indicator ids carry over, so the two
vintages are separate measures, not a panel.

| Theme | PAI 1.0 indicators (rates / checks) | PAI 2.0 indicators (rates / checks) |
|---|---:|---:|
| T1 Poverty-free and enhanced livelihoods | 32 (28 / 4) | 14 (12 / 2) |
| T2 Healthy | 21 (21 / 0) | 15 (10 / 5) |
| T3 Child-friendly | 82 (61 / 21) | 15 (13 / 2) |
| T4 Water-sufficient | 21 (14 / 7) | 10 (4 / 6) |
| T5 Clean and green | 33 (25 / 8) | 11 (6 / 5) |
| T6 Self-sufficient infrastructure | 159 (30 / 129) | 18 (4 / 14) |
| T7 Socially just and secured | 62 (38 / 24) | 20 (13 / 7) |
| T8 Good governance | 62 (25 / 37) | 26 (3 / 23) |
| T9 Women-friendly | 44 (37 / 7) | 21 (14 / 7) |
| Total theme-indicator rows (distinct ids) | 516 (435) | 150 (119) |

Full per-state breakdown: [`docs/pai_summary_by_state.csv`](docs/pai_summary_by_state.csv)
(year totals: [`docs/pai_summary.csv`](docs/pai_summary.csv)). Regenerate with
`uv run scripts/data_summary.py`.

### Downloads — Hugging Face [`soodoku/pai`](https://huggingface.co/datasets/soodoku/pai)

The release table, the provenance archives per vintage, the independent hierarchy crawl and the
append-only logs (`archives/compact_<year>.json`, `logs/`) are published on the Hub; no account is needed. `publish_hf.py` checks every file's size and SHA-256 against the Hub after upload.

| file | contents | size | sha256 |
| --- | --- | --- | --- |
| `release/pai_gp.parquet` | the versioned GP × vintage table (also in each `v*` git tag) | 12.8 MiB | `da92576b…848b1e` |
| `archives/blocks_2022-2023.tar.zst` | per-block typed Parquet cache, PAI 1.0 | 55.0 MiB | `05e4c0d1…fe0a98` |
| `archives/blocks_2023-2024.tar.zst` | per-block typed Parquet cache, PAI 2.0 | 64.1 MiB | `02cd7594…d27cf0` |
| `archives/html_2022-2023.tar.zst` | rendered page captures, PAI 1.0 (both retrievals per block) | 303.8 MiB | `e6d06a80…187198` |
| `archives/html_2023-2024.tar.zst` | rendered page captures, PAI 2.0 (both retrievals per block) | 359.8 MiB | `2ed5a24e…619357` |
| `universe/gp_universe.parquet` | official LGD hierarchy denominator, both vintages | 5.2 MiB | `1edf5b61…3e35bf` |
| `archives/universe_source.tar.zst` | raw hierarchy handler responses behind the denominator | 3.5 MiB | `a1cf532a…fafb6e` |

```bash
pip install huggingface_hub
hf download soodoku/pai release/pai_gp.parquet --repo-type dataset --local-dir .
hf download soodoku/pai archives/blocks_2023-2024.tar.zst --repo-type dataset --local-dir .
```

Drop the archives into `data/` and `make expand YEAR=2023-2024` restores the block tree; the
derived analysis bundle (long/wide score tables with grades, metadata, universe) is not published
because `uv run scripts/pai_rebuild_index.py --data-dir data --national-official-controls`
regenerates it in a few minutes.

The original June 2026 collection remains on Harvard Dataverse
([doi:10.7910/DVN/FRUKWS](https://doi.org/10.7910/DVN/FRUKWS)) for the record; its PAI 2.0
tables are incomplete (PAI 2.0: 183,011 of 259,867 GPs; PAI 1.0: 169,673 of 216,285) and are
superseded by this release.

### Notes

- **West Bengal** has no GP scores in either year (the portal returns "Details are not available"); a few small UTs/states have data only in 2023-2024.
- **Both vintages equal the Ministry's published state tables**, enforced state by state at rebuild:
  PAI 2.0 259,867 GPs across 33 States/UTs; PAI 1.0 216,285 across 29 (Meghalaya, Nagaland, Goa,
  Puducherry and West Bengal were excluded pending validation). One reviewed exception: the portal
  displays 2,154 Assam GPs for 2022-23 against 2,183 in the Ministry's table, identically in two
  independent double retrievals (`config/official_count_exceptions.csv`), so PAI 1.0 ships 216,256.
- Reproducible end-to-end from [`scripts/pai_scraper_resumable.py`](scripts/pai_scraper_resumable.py) → [`scripts/pai_rebuild_index.py`](scripts/pai_rebuild_index.py) → [`scripts/build_data_package.py`](scripts/build_data_package.py) → [`scripts/publish_hf.py`](scripts/publish_hf.py).

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
uv run scripts/pai_collect_universe.py --out runs/pai_universe --years 2022-2023 2023-2024 --delay 1
uv run scripts/pai_scraper_resumable.py --years 2022-2023 2023-2024 --headless --delay 1.5
```

Collect the hierarchy universe first: the scraper reads `runs/pai_universe/collection_manifest.json`
by default to learn which states the Ministry scored below their hierarchy size, and without it
every block must match its universe exactly.

The v0.2.0 PAI 2.0 collection ran as three workers on disjoint `--state-values` sets against
the same `--out`; block directories never overlap and the append-only logs take a file lock. A
block costs about 25 s of server round-trips, so three workers give roughly three times the
throughput. One worker:

```bash
uv run scripts/pai_scraper_resumable.py --years 2023-2024 --state-values 9,5,3 \
  --out data --headless --delay 0.5 --block-third-party \
  --baseline-counts-from data --universe-exceptions config/gp_universe_exceptions.csv \
  --gp-name-links config/gp_name_links.csv \
  --hierarchy-manifest runs/pai_universe/collection_manifest.json \
  --max-retries 2 --skip-rebuild-derived
```

## Resume

Run the same command again — blocks with `DONE.json` are skipped. Retry flags:

- `--retry-empty` — re-do blocks that finished with zero GP rows.
- `--retry-no-data` — re-verify blocks previously marked "no data available". The server returns an
  identical "not available" alert both for genuinely empty blocks and (spuriously) under load, so a
  no-data result is confirmed by re-searching `--no-data-confirm` times (default 1) before it is
  accepted; if data appears it is recovered, and an indeterminate recheck is retried (never a
  terminal false negative).
- `--overwrite` — rescrape matching blocks unconditionally.

Every block also caches the official `Y_GPs_By_LGD_Block` JSON response and request provenance.
A block is marked done only when independently repeated score retrievals are identical and their
GP-code set exactly equals that official handler universe. Two reviewed facts relax that rule
without per-block ledger rows, and both are recorded in `DONE.json` (`universe_contract_status`):
a state absent from the Ministry's final table (West Bengal) may report no data, and a state whose
official scored total is below its hierarchy size in the universe manifest (Goa, Meghalaya) may
score a subset of each block — there the state total at rebuild time is the completeness check.
Prior-release counts are diagnostics in `block_count_audit.csv`, not a completeness gate. Any other
intentional unscored GP or ambiguous PAI 1.0 name-to-code link requires a manually reviewed CSV row
with evidence in `config/`.
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
│       ├── data_wide.parquet    # typed per-block tables (schema in pai_contracts.typed_schema)
│       ├── metadata.parquet
│       └── scores_long.parquet
└── 2023-2024/
```

The per-block Parquet tables are the resumable parsed cache, written through the same typed
schema as the release (identifiers string, scores `float64`, `theme_order` `int8`) with a
read-back check, so a malformed cell fails that block at scrape time rather than the rebuild.
Rendered HTML is the raw source cache. Canonical analysis data are typed Parquet generated from
that cache after hard contracts pass:

```bash
uv run scripts/pai_rebuild_index.py --data-dir data --national-official-controls \
  --universe-data-dir runs/pai_universe               # -> data/derived/
uv run scripts/data_summary.py                        # coverage tables -> docs/pai_summary*.csv
uv run scripts/scrape_progress.py --data-dir data --year 2023-2024
uv run scripts/pai_inspect_output.py --out data       # quick counts
make data-package DERIVED=data/derived                # -> data/release/pai_gp.parquet + MANIFEST.json
uv run scripts/publish_hf.py --repo soodoku/pai --version 0.2.0 --card docs/hf_dataset_card.md
```

Each `derived/` directory contains `gp_scores_wide.parquet` (canonical one row per GP),
`gp_metadata.parquet`, tidy `gp_scores_long.parquet`, the official denominator/linkage table
`gp_universe.parquet`, typed block/dropdown manifests, and
`collection_manifest.json` with schemas, counts, official controls, and SHA-256 checksums.
Identifiers remain strings; scores and count/order fields are numeric. The build asserts unique
GP-year keys (LGD code where present, documented location/name fallback otherwise), exactly ten
unique scores per GP, one overall score, scores in `[0, 100]`, and row conservation against every
successful `DONE.json`. The universe table requires unique `(year, gp_code)` keys and requires
every scored GP to belong to the hierarchy; hierarchy GPs without scores are retained. The global
tables are streamed block by block into one Parquet writer per table, and validated in bounded
Arrow record batches, rather than materializing the national long table in memory.

The derived bundle is an intermediate and is not published: regenerate it with
`pai_rebuild_index.py`. What is published is the release table (git tag + Hugging Face) and, as
provenance, the per-block Parquet cache and raw HTML archives from `make compact`
(`publish_hf.py` uploads them and verifies size and SHA-256 against the Hub).
PAI 2.0 release creation requires all 33 State/UT totals and the 259,867 national total in the
Ministry's final August 2026 table (`--national-official-controls`).

### Compact storage

That tree is tens of thousands of small files. `make compact` collapses a finished year into
two solid archives without losing anything:

```bash
make verify                    # archives match their manifests; every block still passes the contract
make compact                   # -> data/blocks_<year>.tar.zst, html_<year>.tar.zst, *.parquet
make data-status               # what form each year is in
make expand YEAR=2022-2023     # restore a byte-identical tree (needed to resume a scrape)
```

| year | live tree | `blocks_<year>.tar.zst` | `html_<year>.tar.zst` |
| --- | --- | --- | --- |
| 2022-2023 | 1.4 GB | 43 MB | 128 MB |
| 2023-2024 | 3.0 GB | 64 MB | 360 MB |

Solid `.tar.zst` rather than per-file `.gz`: the JSON files are near-duplicates of each other,
and zstd's long window compresses across them where gzip's 32 KB window cannot. Every reader
goes through `scripts/pai_stores.py`, so the tools work against either form and the scraper
keeps writing the live tree regardless.

Nothing is deleted until its replacement has been written and read back with every file's
sha256 matched against `data/compact_<year>.json`; `make compact` first runs the `verify`
checks (archive manifests, every block contract) and stops on any failure. Debug screenshots (`debug/*.png`) are
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
make release-check VERSION=0.2.0
```

`release-check` does not create a tag. It requires a clean `main` worktree, a dated changelog
entry matching `pyproject.toml`, a version not already tagged, and a byte-for-byte valid data
manifest. A release still requires independent review and green CI before a human creates and
pushes the annotated tag.

Requires Python 3.14 or newer: the archives use `compression.zstd`, which entered the
standard library in 3.14, so the packaging tools stay dependency-free.
