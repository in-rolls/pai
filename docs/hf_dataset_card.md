---
license: mit
language:
  - en
pretty_name: Panchayat Advancement Index (PAI) Gram Panchayat scores
size_categories:
  - 100K<n<1M
tags:
  - india
  - panchayat
  - local-government
  - governance
configs:
  - config_name: default
    data_files: release/pai_gp.parquet
---

# Panchayat Advancement Index — Gram Panchayat scores

Analysis-ready scores from the Government of India's
[Panchayat Advancement Index](https://pai.gov.in) for every Gram Panchayat (GP) in the
official hierarchy, for both published vintages: **PAI 1.0 (2022-2023)** and
**PAI 2.0 (2023-2024)**. The overall PAI score and nine theme scores are on the portal's
0–100 scale.

Code, contracts, and the scraper that rebuilds everything:
<https://github.com/in-rolls/pai>.

## Files

| path | what it is |
| --- | --- |
| `release/pai_gp.parquet` | one row per hierarchy GP × vintage, key `(year, gp_code)`; `score_available` marks GPs the portal never scored (scores null, never zero) |
| `release/MANIFEST.json` | version, schema, row counts, byte sizes, SHA-256 checksums |
| `archives/blocks_<year>.tar.zst` | per-block typed Parquet cache — rebuilds the release table without a browser |
| `archives/html_<year>.tar.zst` | rendered page captures — the raw source evidence |

## Coverage

| vintage | states with scores | districts | blocks with data | scored GPs | score rows |
| --- | --- | --- | --- | --- | --- |
| 2022-2023 | 29 / 34 | 753 | 6,318 | 216,256 | 2,162,560 |
| 2023-2024 | 33 / 34 | 741 | 6,766 | 259,867 | 2,598,670 |

PAI 2.0 totals equal the Ministry's final state table (PIB release
[PRID 2294288](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2294288&lang=1&reg=6)):
259,867 scored GPs across 33 States/UTs, and PAI 1.0 totals the baseline table (PIB release
[PRID 2120320](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2120320)): 216,285 GPs across 29
States/UTs, less 29 Assam GPs the portal does not display. West Bengal has no scores in either
vintage; Goa and Meghalaya score a subset of their hierarchy. PAI 1.0 and PAI 2.0 use different indicator systems
and are separate cross-sectional measures, not a panel on an invariant scale.

## Columns

| Column | Type | Meaning and universe | Missing values | Provenance |
| --- | --- | --- | --- | --- |
| `year` | string | PAI fiscal-year vintage | none allowed | portal year selector |
| `state`, `district`, `block` | string | Hierarchy labels for the GP in that vintage | none allowed | PAI hierarchy handlers |
| `state_value`, `district_value`, `block_value` | string | Portal/LGD hierarchy identifiers | none allowed | PAI hierarchy handlers |
| `gp_name` | string | GP-handler name after removing its checked LGD-code suffix | none allowed | PAI GP handler |
| `gp_code` | string | LGD Gram Panchayat code; part of the release key | none allowed | PAI GP handler |
| `hierarchy_source_url` | string | Exact GP-handler request URL | none allowed | PAI GP handler |
| `hierarchy_retrieved_utc` | string | UTC retrieval timestamp | none allowed | collection provenance |
| `hierarchy_source_sha256` | string | SHA-256 of the retained raw GP-handler response | none allowed | collection provenance |
| `score_available` | boolean | Whether the portal publishes ten PAI score rows | none allowed | exact score-to-universe join |
| `scorecard_url` | string | Official PAI scorecard path | null when `score_available` is false | PAI score table |
| `overall_pai_score_score` | float64 | Overall PAI score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t1_poverty_free_and_enhanced_livelihoods_panchayat_score` | float64 | Theme 1 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t2_healthy_panchayat_score` | float64 | Theme 2 score on the portal's 0–100 scale | null when unscored; plus 38 reviewed PAI 1.0 source blanks | PAI score table and archived HTML evidence |
| `t3_child_friendly_panchayat_score` | float64 | Theme 3 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t4_water_sufficient_panchayat_score` | float64 | Theme 4 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t5_clean_and_green_panchayat_score` | float64 | Theme 5 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t6_self_sufficient_infrastructure_in_panchayat_score` | float64 | Theme 6 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t7_socially_just_and_socially_secured_panchayat_score` | float64 | Theme 7 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t8_panchayat_with_good_governance_score` | float64 | Theme 8 score on the portal's 0–100 scale | null when unscored | PAI score table |
| `t9_women_friendly_panchayat_score` | float64 | Theme 9 score on the portal's 0–100 scale | null when unscored | PAI score table |

## Provenance

Every block was retrieved twice in independent postbacks and accepted only when both retrievals
agreed and their GP-code set matched the portal's official GP handler for that block (exactly, or
as a documented subset in partially scored states). Rendered HTML for every page is in the
`html_<year>` archive; the per-block Parquet cache in `blocks_<year>` is what
`scripts/pai_rebuild_index.py` and `scripts/build_data_package.py` consume.

```bash
pip install huggingface_hub
hf download soodoku/pai release/pai_gp.parquet --repo-type dataset
```

```python
import pyarrow.parquet as pq
gps = pq.read_table("pai_gp.parquet")
scores = gps.filter(gps["score_available"])
```
