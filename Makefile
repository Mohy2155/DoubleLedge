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
	docker compose exec -T -e DB_USER=postgres -e DB_PASSWORD=postgres -e REQUIRE_RESTRICTED_DB_ROLE=0 api python scripts/benchmarks/run.py

demo:
	docker compose exec -T -e DB_USER=postgres -e DB_PASSWORD=postgres -e REQUIRE_RESTRICTED_DB_ROLE=0 api python scripts/demo.py

integrity:
	docker compose exec -T -e DB_USER=postgres -e DB_PASSWORD=postgres -e REQUIRE_RESTRICTED_DB_ROLE=0 api python manage.py check_ledger_integrity

lint:
	ruff check .
	mypy .
