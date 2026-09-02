# Data dictionary

`data/release/pai_gp_scores.parquet` has one row for one Gram Panchayat in one PAI
fiscal-year vintage. Its unique key is `(year, gp_code)`. The companion
`pai_gp_universe.parquet` defines the official denominator at the same grain.

## Score table

| Column | Type | Meaning and universe | Missing values | Provenance |
| --- | --- | --- | --- | --- |
| `year` | string | PAI fiscal-year vintage; every released score row | none allowed | portal year selector |
| `state`, `district`, `block` | string | Official hierarchy labels for the GP in that vintage | none allowed | PAI hierarchy handlers |
| `state_value`, `district_value`, `block_value` | string | Portal/LGD hierarchy identifiers, retained as strings | none allowed | PAI hierarchy handlers |
| `gp_name` | string | Official displayed Gram Panchayat name | none allowed | PAI score table and GP handler |
| `gp_code` | string | LGD Gram Panchayat code; part of the release key | none allowed | scorecard URL or GP handler |
| `scorecard_url` | string | Official PAI scorecard path for the GP and vintage | none allowed | PAI score table |
| `overall_pai_score_score` | float64 | Overall PAI score on the portal's 0–100 scale | none allowed | PAI score table |
| `t1_poverty_free_and_enhanced_livelihoods_panchayat_score` | float64 | Theme 1 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t2_healthy_panchayat_score` | float64 | Theme 2 score on the portal's 0–100 scale | 22 reviewed PAI 1.0 source blanks | PAI score table and archived HTML evidence |
| `t3_child_friendly_panchayat_score` | float64 | Theme 3 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t4_water_sufficient_panchayat_score` | float64 | Theme 4 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t5_clean_and_green_panchayat_score` | float64 | Theme 5 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t6_self_sufficient_infrastructure_in_panchayat_score` | float64 | Theme 6 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t7_socially_just_and_socially_secured_panchayat_score` | float64 | Theme 7 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t8_panchayat_with_good_governance_score` | float64 | Theme 8 score on the portal's 0–100 scale | none allowed | PAI score table |
| `t9_women_friendly_panchayat_score` | float64 | Theme 9 score on the portal's 0–100 scale | none allowed | PAI score table |

PAI 1.0 and PAI 2.0 use different indicator systems. The two vintages are separate
cross-sectional measures, not repeated observations of an invariant scale.

## Universe table

| Column | Type | Meaning and universe | Missing values | Provenance |
| --- | --- | --- | --- | --- |
| `year` | string | PAI fiscal-year vintage | none allowed | portal year selector |
| `state`, `district`, `block` | string | Official hierarchy labels | none allowed | official PAI handlers |
| `state_value`, `district_value`, `block_value` | string | Portal/LGD hierarchy identifiers | none allowed | official PAI handlers |
| `gp_code` | string | LGD Gram Panchayat code; part of the universe key | none allowed | official GP handler |
| `gp_name` | string | Official GP-handler name after removing its checked `[LGD code]` suffix | none allowed | official GP handler |
| `source_url` | string | Exact handler request URL | none allowed | collection provenance |
| `retrieved_utc` | string | UTC retrieval timestamp | none allowed | collection provenance |
| `source_sha256` | string | SHA-256 of the retained raw handler response | none allowed | collection provenance |
