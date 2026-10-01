.PHONY: up down migrate test test-race bench demo integrity lint

up:
	docker compose up -d

down:
	docker compose down -v

migrate:
	docker compose exec -T api python manage.py migrate

test:
	docker compose exec -T api pytest

test-race:
	docker compose exec -T api pytest -m "race"

bench:
	docker compose exec -T api python scripts/benchmarks/run.py

demo:
	docker compose exec -T api python scripts/demo.py

integrity:
	docker compose exec -T api python manage.py check_ledger_integrity

lint:
	ruff check .
	mypy .
