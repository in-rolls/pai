# Panchayat Advancement Index data

[![CI](https://github.com/in-rolls/pai/actions/workflows/ci.yml/badge.svg)](https://github.com/in-rolls/pai/actions/workflows/ci.yml)
[![Dataset](https://img.shields.io/badge/Hugging%20Face-soodoku%2Fpai-blue)](https://huggingface.co/datasets/soodoku/pai)

Gram Panchayat scores from the Government of India's [Panchayat Advancement Index](https://pai.gov.in), covering PAI 1.0 (2022–2023) and PAI 2.0 (2023–2024). The published table retains every Gram Panchayat in the collected official hierarchy, including those without scores. This repository provides the versioned data, its validation rules, and the tools that reproduce it.

## Data

Download the current data and provenance archives from [soodoku/pai on Hugging Face](https://huggingface.co/datasets/soodoku/pai). The release table is also committed under [`data/release/`](data/release/) and pinned by Git release tags.

| File | Unit | Contents |
| --- | --- | --- |
| [pai_gp.parquet](data/release/pai_gp.parquet) | Gram Panchayat × PAI vintage | 535,113 hierarchy records; score availability and ten nullable scores |
| [MANIFEST.json](data/release/MANIFEST.json) | Data release | Version, schema, counts, reviewed exceptions, and SHA-256 hashes |
| [pai_indicators.csv](docs/pai_indicators.csv) | Indicator × theme × PAI vintage | Indicator definitions, numerators, denominators, and source URLs |

The release contains 216,256 scored GPs for 2022–2023 and 259,867 for 2023–2024. Other hierarchy records have no published score. Generated [year summaries](docs/pai_summary.csv) and [state summaries](docs/pai_summary_by_state.csv) give the detailed coverage; the release manifest distinguishes scored and unscored hierarchy rows.

The original collection at Harvard Dataverse, [10.7910/DVN/FRUKWS](https://doi.org/10.7910/DVN/FRUKWS), is retained as a historical deposit. Its incomplete tables are superseded by the versioned GitHub/Hugging Face release. That DOI does not identify the current release files.

## Columns

The unique key is `(year, gp_code)`. [DATA_DICTIONARY.md](DATA_DICTIONARY.md) defines every column, type, missing-value rule, and source.

| Columns | Meaning |
| --- | --- |
| `year`, `gp_code`, `gp_name` | PAI vintage and Gram Panchayat identity |
| `state`, `district`, `block` and corresponding `*_value` fields | Official hierarchy labels and identifiers |
| `hierarchy_source_url`, `hierarchy_retrieved_utc`, `hierarchy_source_sha256` | Provenance for the retained hierarchy response |
| `score_available` | Whether the portal publishes a score record for this GP |
| `scorecard_url` | Official scorecard link, null when unscored |
| `overall_pai_score_score` | Overall score on the portal's 0–100 scale |
| Nine `t1_..._score` through `t9_..._score` fields | Theme scores on the same displayed range |

Missing scores remain null. They are never filled with zero. There are also 38 reviewed missing PAI 1.0 Healthy Panchayat theme values among scored records, documented in the dictionary and source evidence.

## Coverage and known gaps

PAI 1.0 and PAI 2.0 use different indicator frameworks: 516 and 150 theme-indicator rows respectively. They are separate cross-sectional measures; a difference between the two scores cannot be interpreted as change on an unchanged scale. The [indicator definitions](docs/pai_indicators.csv) preserve the framework for each vintage.

West Bengal has no published GP scores in either vintage. State totals are checked against the Ministry's tables with one reviewed exception: the 2022–2023 portal displays 2,154 Assam GPs against the Ministry's 2,183. Two independent collections reproduced the 29-GP difference. The release therefore contains 216,256 scored PAI 1.0 GPs, rather than the Ministry's 216,285. Evidence is recorded in [official_count_exceptions.csv](config/official_count_exceptions.csv) and the release manifest.

The table preserves GPs absent from the score publication but does not infer why they lack a score. Reviewed name-to-code links, duplicate displayed names, and hierarchy corrections are documented in [`config/`](config/) and the [collection guide](docs/collection.md).

## How collected

| Stage | Source and method | Retained evidence |
| --- | --- | --- |
| Hierarchy | Official State/District/Block/GP handlers, collected independently for each vintage | Raw responses, request parameters, timestamps, hashes, and GP identifiers |
| Scores | Resumable browser collection from the current unified `TW-GP.aspx` page | Rendered HTML, per-block typed tables, and completion/failure logs |
| Validation | Match scores to the hierarchy; check keys, ten-score structure, state totals, and reviewed exceptions | Collection manifests and reviewed evidence files |
| Release | Retain the full hierarchy with nullable scores; verify schema, row counts, and file hashes | Versioned Parquet and release manifest |

The retired `TW-GP-New.aspx` route is incomplete and is not the current PAI 2.0 source. Raw HTML and per-block archives are distributed on Hugging Face. [The collection guide](docs/collection.md) documents rebuilding, resuming, archive verification, and publication.

## Usage

```bash
git clone https://github.com/in-rolls/pai.git
cd pai
uv sync --frozen
make verify-data
```

```python
import pyarrow.parquet as pq

gps = pq.read_table("data/release/pai_gp.parquet")
scored = gps.filter(gps["score_available"])
print(scored.select(["year", "gp_name", "overall_pai_score_score"]))
```

To download only the released table:

```bash
uv tool run --from huggingface-hub hf download soodoku/pai release/pai_gp.parquet \
  --repo-type dataset --local-dir .
```

Python 3.14 or newer is required for the standard-library Zstandard archive support. Install Chromium with `uv run playwright install chromium` to run browser tests or collect pages. Reading the released Parquet does not require a browser.

## Development

`make check` runs lint, formatting, tests, and pre-commit hooks. `make verify-data` checks the committed release's schema, keys, counts, and hashes. `make ci-docker` runs the checks in a standard Python 3.14 container with Chromium. GitHub CI also verifies the committed data package.

Release changes are documented in [CHANGELOG.md](CHANGELOG.md). `make release-check VERSION=<version>` performs the existing release preflight; it does not create a tag. Collection and publishing commands are in the [guide](docs/collection.md).

## Citation

Cite Gaurav Sood, *Panchayat Advancement Index data*, and identify the release tag or commit used. [CITATION.cff](CITATION.cff) provides machine-readable metadata. When using the historical Dataverse files, cite their separate DOI and deposit version.

## License

The code and current Hugging Face data release use the [MIT License](LICENSE), as recorded in the [dataset card](docs/hf_dataset_card.md). The historical Dataverse deposit is released under CC0 1.0.
