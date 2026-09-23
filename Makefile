.PHONY: install init seed run test lint
install:
	python -m pip install -e ".[dev]"
init:
	python -m alembic upgrade head
seed:
	python -m app.seed.demo
run:
	python -m uvicorn app.main:app --reload
test:
	python -m pytest
lint:
	python -m ruff check .
