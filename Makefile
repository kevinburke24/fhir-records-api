.PHONY: run docker test

run:
	uvicorn app.main:app --reload --port 8000

docker:
	docker compose up --build

test:
	pytest tests/ -v
