# Data dictionary

`data/release/pai_gp.parquet` has one row for one valid hierarchy Gram Panchayat in one
PAI fiscal-year vintage. Its unique key is `(year, gp_code)`. Every hierarchy GP is retained
whether or not the portal publishes its score.

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

Score absence is structural portal nonpublication, not a zero score. The release does not infer
why a hierarchy GP lacks a score. PAI 1.0 and PAI 2.0 also use different indicator systems: the
two vintages are separate cross-sectional measures, not repeated observations of an invariant
scale.
