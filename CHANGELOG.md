# Changelog

Changes are organized by tagged data release. Corrections come first because they are the
changes most likely to affect an analysis already in progress.

## Unreleased

### Corrected

- PAI 1.0 theme columns are normalized through a reviewed 20-header English/Hindi dictionary.
  The portal switched 1,975 GP rows into Hindi headers, which previously split ten outcomes
  across legacy columns even though their scores were intact.
- Six PAI 2.0 rows belonging to three same-name GP pairs are linked to LGD codes through exact
  reviewed ten-score vectors rather than display order.
- Twenty-two official blank PAI 1.0 Healthy Panchayat values remain null and are documented;
  they are never changed to zero.
- Three PAI 1.0 district placements incorrectly duplicated under Chhattisgarh are excluded through
  a reviewed ledger; four affected score rows take their canonical state from the valid hierarchy.
- Blank or truncated PAI 1.0 display IDs are replaced by the full LGD code encoded in each retained
  scorecard URL.

### Added

- A versioned, universe-left GP-year table with explicit score availability and a checksummed
  manifest.
- Hard release checks for typed schemas, nonblank identities, unique `(year, gp_code)` keys,
  score membership in the full hierarchy, repository cleanliness, and tag/version consistency.
