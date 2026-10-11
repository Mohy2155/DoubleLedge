.PHONY: up stop down demo stress test bench integrity

up:
	docker compose up -d

stop:
	docker compose stop

# WIPES data
down:
	docker compose down -v

demo:
	docker compose run --rm --no-deps owner python scripts/gen_dev_keys.py
	docker compose up -d --wait
	docker compose run --rm owner python manage.py demo_bootstrap

stress:
	docker compose run --rm owner python scripts/demo.py

test:
	docker compose run --rm owner pytest --create-db -p no:cacheprovider --ignore-glob='*redteam*'

bench:
	docker compose run --rm owner python scripts/benchmarks/run.py

integrity:
	docker compose run --rm owner python manage.py check_ledger_integrity
