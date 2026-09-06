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

## Indicator framework: `docs/pai_indicators.csv`

One row per (`pai_version`, `theme_number`, `indicator_id`), fetched from the portal's
indicator browser by `scripts/pai_indicators.py`; `tests/test_indicators.py` re-checks the
committed file. Contract: key unique; no missing values; 516 PAI 1.0 and 150 PAI 2.0 rows
(435 and 119 distinct ids); every version covers all nine themes; a `ratio` row has a
non-empty denominator; `kind` is one of `ratio`, `number`, `binary`.

| column | type | meaning | provenance |
|---|---|---|---|
| `pai_version` | string | `PAI 1.0` or `PAI 2.0` | portal query `s=1` / `s=2` |
| `fiscal_year` | string | `2022-2023` or `2023-2024`; fixed by `pai_version` | PAI release year |
| `theme_number` | int8 | 1 to 9 | portal query `t` |
| `theme_slug` | string | the release column stem for the theme, e.g. `t8_panchayat_with_good_governance` | `CANONICAL_THEME_SLUGS` |
| `indicator_id` | int64 | the portal's indicator id, the bracketed suffix of its label. The portal repeats an id under alias wordings within a theme and only the first is kept; the same id can appear under several themes | portal |
| `mandatory` | string | `Mandatory` or `Optional` | portal column |
| `kind` | string | `ratio` (denominator distinct from numerator), `number` (a single reported quantity, matched on the label: number of, total, percentage, share of, ratio of, rate of, drop-out rate, average) or `binary` (a yes/no check) | derived by `classify()` from the portal's columns, not from a Ministry label |
| `indicator`, `numerator`, `denominator` | string | labels without their ids; `denominator` is empty for checks | portal columns |
| `source_url` | string | page fetched | script |
| `retrieved_utc` | string | ISO 8601 UTC timestamp of the fetch | script |
