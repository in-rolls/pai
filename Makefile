DERIVED ?= data/derived
DATA_RELEASE ?= data/release

.PHONY: sync format lint test check verify compact expand data-status data-package verify-data release-check

sync:                ## Create/refresh the .venv from pyproject + uv.lock
	uv sync
	uv run playwright install chromium

format:              ## Auto-format and auto-fix lint
	uv run ruff format scripts tests
	uv run ruff check --fix scripts tests

lint:                ## Check formatting + lint (no changes)
	uv run ruff format --check scripts tests
	uv run ruff check scripts tests

test:                ## Run the test suite
	uv run pytest

check: lint test     ## Lint + test

data-package:        ## Build the committed universe-left package from a validated derived bundle
	uv run scripts/build_data_package.py --derived-dir $(DERIVED) --out $(DATA_RELEASE)

verify-data:         ## Verify the committed package schemas, keys, counts, and checksums
	uv run scripts/verify_data_package.py --data-dir $(DATA_RELEASE)

release-check: check verify-data ## Preflight a tag without creating it: make release-check VERSION=0.1.0
	uv run scripts/release_check.py $(VERSION)

verify:              ## Prove the derived global CSVs are rebuildable from the per-block files
	uv run scripts/pai_compact.py verify

compact:             ## Archive the data tree with zstd and drop what verify proved redundant
	uv run scripts/pai_compact.py compact

expand:              ## Restore a byte-identical tree (needed to resume a scrape): make expand YEAR=2022-2023
	uv run scripts/pai_compact.py expand --years $(YEAR)

data-status:         ## Report what form each year is stored in, and what it costs
	uv run scripts/pai_compact.py status
