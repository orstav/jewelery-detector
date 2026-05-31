.PHONY: install-dev analyze analyze-max lint typecheck security deadcode test linux-test

install-dev:
	python3 -m pip install -r requirements-dev.txt

analyze: analyze-max

analyze-max:
	python3 devtools/static_analysis_max.py

lint:
	python3 -m ruff check --config pyproject.toml tools tests

typecheck:
	python3 -m mypy --config-file pyproject.toml
	python3 -m pyright

security:
	python3 -m bandit -c pyproject.toml -r tools tests

deadcode:
	python3 -m vulture tools tests --min-confidence 80

test:
	python3 -m pytest

linux-test:
	docker build -f Dockerfile.linux-test -t jewelry-detector-linux-test .
	docker run --rm jewelry-detector-linux-test
