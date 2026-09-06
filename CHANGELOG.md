# Changelog

Changes are organized by tagged data release. Corrections come first because they are the
changes most likely to affect an analysis already in progress.

## v0.2.1 — 2026-09-05

- `docs/pai_indicators.csv`: the indicator framework behind each theme score, one row per
  version, theme and indicator (516 PAI 1.0 rows over 435 distinct ids; 150 PAI 2.0 rows over
  119), fetched from the portal's indicator browser by `scripts/pai_indicators.py` under a
  typed contract with asserts (kind: ratio, number or yes/no check, from the portal's data
  columns); documented in README and DATA_DICTIONARY; published to the Hub
  alongside the unchanged v0.2.0 data package (same `pai_gp.parquet` bytes).

## v0.2.0 — 2026-09-04

### Corrected

- PAI 1.0 coverage rises from 169,673 to 216,256 scored GPs against the Ministry's baseline
  table of 216,285 (PIB 2120320), now enforced state by state like PAI 2.0. The 29-GP gap is
  Assam, where the portal displays 2,154 of the 2,183 validated GPs in every retrieval
  (`config/official_count_exceptions.csv`). Sixteen further Healthy Panchayat blanks in
  Manipur/Tengnoupal/Machi join the reviewed null-score ledger (38 rows).
- PAI 2.0 coverage rises from 183,011 to 259,867 scored GPs, matching the Ministry's final
  table in every State/UT. The earlier collection lost page 1 of every block with more than 100
  GPs and recorded false "no data" for blocks that followed a block of exactly 100 GPs, because
  the portal keeps its grid page index across postbacks (worst in Maharashtra, Uttar Pradesh,
  Madhya Pradesh, Punjab, Chhattisgarh). Every PAI 2.0 row now carries its LGD code.
- Same-name GPs in one block keep their own LGD codes (Balaghat/Paraswada's two Pondi), and
  GP names with bracket suffixes (`Paroo [N]`, `Bariyarpur[East]`) are no longer truncated.
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

### Changed

- Per-block caches are typed Parquet (`metadata.parquet`, `scores_long.parquet`,
  `data_wide.parquet`) written through the release schema with a read-back check, so a
  malformed cell fails its block at scrape time. The per-block CSVs, the consolidated global
  CSVs, and the CSV-to-Parquet conversion step are gone; the global tables are streamed from
  the block tables. Existing caches were migrated through the same typed writer
  (`scripts/pai_migrate_block_tables.py`).
- Results are rewound to page 1 before and after every Search: the portal's grid keeps its
  page index across postbacks, which had silently dropped page 1 of every block with more
  than 100 GPs and produced false "no data" blocks after blocks of exactly 100 GPs.

### Added

- Independent review fixes before tagging: the data package refuses any hierarchy block without
  a successful collection outcome (`block_coverage` in `MANIFEST.json`), `make verify` refuses a
  year that still carries a `FAILED.json`, zero scores are no longer conflated with blanks in the
  identity signature, progress reports key blocks by hierarchy id rather than path, `DONE.json`
  is written atomically, a pager click that renders nothing is retried rather than read as the
  last page, blank identity fields fail the block contract, and the Hub publisher compares small
  files by content rather than size. The Hub bundle also carries the compact manifests
  `make expand` verifies against and the folded append-only logs; the scraper refuses to
  resume into a year that exists only as an archive; release builds require the independent
  universe crawl; hierarchy exclusions apply only to the response they were reviewed against.
- A versioned, universe-left GP-year table with explicit score availability and a checksummed
  manifest.
- Hard release checks for typed schemas, nonblank identities, unique `(year, gp_code)` keys,
  score membership in the full hierarchy, repository cleanliness, and tag/version consistency.
