.PHONY: up down test eval eval-live bench token lint fmt

up:
	docker compose up --build

down:
	docker compose down

test:
	docker compose run --rm --build tests

eval:
	docker compose run --rm --build eval

# Needs GROQ_API_KEY / GROQ_MODEL (and optionally Gemini) in .env
eval-live:
	docker compose --env-file .env run --rm --build eval python -m eval.run_eval --mode live

bench:
	docker compose --env-file .env run --rm --build eval python -m scripts.bench_latency

# usage: make token COMPANY=acme EMPLOYEE=A001
token:
	docker compose exec api python -m scripts.issue_token --company $(COMPANY) --employee $(EMPLOYEE)

lint:
	uv run ruff check app tests eval scripts

fmt:
	uv run ruff format app tests eval scripts
