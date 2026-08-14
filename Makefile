.PHONY: install lint test check

install:
	uv sync --dev

lint:
	uv run ruff check .

test:
	uv run pytest -v

check: lint test
