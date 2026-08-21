.PHONY: install lint format typecheck test check

install:
	uv sync --dev
	uv run pre-commit install

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run pyright

test:
	uv run pytest -v

check: lint typecheck test
