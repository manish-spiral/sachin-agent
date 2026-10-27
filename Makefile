.PHONY: init db-init db-stats db-drop scraper matcher applier test lint fmt clean

init:
	@test -n "$$VIRTUAL_ENV" || (echo "ERROR: activate venv first:" && echo "  source .venv/bin/activate" && exit 1)
	uv pip install -e ".[dev]"

db-init:
	sachin-agent-db init

db-migrate:
	sachin-agent-db migrate -m "$(MSG)"

db-upgrade:
	sachin-agent-db upgrade

db-downgrade:
	sachin-agent-db downgrade

db-current:
	sachin-agent-db current

db-stats:
	sachin-agent-db stats

db-drop:
	sachin-agent-db drop

scraper:
	sachin-agent-scraper run

matcher:
	sachin-agent-matcher run

applier:
	sachin-agent-applier run

test:
	pytest

lint:
	ruff check .
	mypy

fmt:
	ruff format .
	ruff check --fix .

clean:
	rm -rf data logs snapshots browser_profile test_sachin-agent.db
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +