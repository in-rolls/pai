DERIVED ?= data/derived
DATA_RELEASE ?= data/release

.PHONY: sync format lint test check verify compact expand data-status data-package verify-data release-check ci-docker

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
	uv run pre-commit run --all-files

data-package:        ## Build the committed universe-left package from a validated derived bundle
	uv run scripts/build_data_package.py --derived-dir $(DERIVED) --out $(DATA_RELEASE) --universe-dir runs/pai_universe

verify-data:         ## Verify the committed package schemas, keys, counts, and checksums
	uv run scripts/verify_data_package.py --data-dir $(DATA_RELEASE)

release-check: check verify-data ## Preflight a tag without creating it: make release-check VERSION=0.1.0
	uv run scripts/release_check.py $(VERSION)

verify:              ## Check archives against their manifests and re-run every block contract
	uv run scripts/pai_compact.py verify

compact:             ## Archive the data tree with zstd and drop what verify proved redundant
	uv run scripts/pai_compact.py compact

expand:              ## Restore a byte-identical tree (needed to resume a scrape): make expand YEAR=2022-2023
	uv run scripts/pai_compact.py expand --years $(YEAR)

data-status:         ## Report what form each year is stored in, and what it costs
	uv run scripts/pai_compact.py status

ci-docker:
	COPYFILE_DISABLE=1 tar --no-xattrs --exclude=._* --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache --exclude=.DS_Store --exclude=runs -cf - . | \
	  docker run --rm -i python:3.14-slim sh -ec 'mkdir /work; tar -xf - -C /work; cd /work; apt-get update -qq; apt-get install -y --no-install-recommends git; pip install -q uv; uv sync --frozen; uv run playwright install --with-deps chromium; uv run ruff check scripts tests; uv run ruff format --check scripts tests; uv run pytest; uv run scripts/verify_data_package.py --data-dir data/release'
