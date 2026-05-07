# Final resumable PAI scraper

## Install

```bash
python3 -m venv pai-venv
source pai-venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Smoke test

```bash
python scripts/pai_scraper_resumable.py \
  --years 2022-2023 \
  --state-contains Bihar \
  --limit-districts 1 \
  --limit-blocks 3
```

If Chromium is flaky, try installed Chrome:

```bash
python scripts/pai_scraper_resumable.py \
  --years 2022-2023 \
  --state-contains Bihar \
  --limit-districts 1 \
  --limit-blocks 3 \
  --browser-channel chrome
```

## Full run

```bash
python scripts/pai_scraper_resumable.py \
  --years 2022-2023 2023-2024 \
  --headless \
  --delay 1.5
```

## Resume

Run the same command again. Blocks with `DONE.json` are skipped.

Use `--retry-empty` to retry blocks that completed with zero GP rows.

Use `--overwrite` to rescrape matching blocks.

## Outputs

```text
test_data/
├── pai_scrape.log
├── block_manifest.csv
├── dropdown_inventory.csv
├── gp_metadata.csv
├── gp_scores_long.csv
├── 2022-2023/
│   └── State__code/District__code/Block__code/
│       ├── context.json
│       ├── DONE.json
│       ├── FAILED.json              # only if failed
│       ├── html/page_001.html
│       ├── data_wide.csv
│       ├── metadata.csv
│       └── scores_long.csv
└── 2023-2024/
```

## Rebuild global indexes

```bash
python scripts/pai_rebuild_index.py --out test_data
```

## Inspect progress

```bash
python scripts/pai_inspect_output.py --out test_data
```
