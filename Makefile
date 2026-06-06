.PHONY: sync format lint test check

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
