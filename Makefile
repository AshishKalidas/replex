PY ?= python
EXAMPLE := examples/support_agent
PYTHON_VERSIONS := 3.10 3.11 3.12 3.13

.PHONY: help install test test-fast lint format typecheck build-console test-console build demo \
        record-claude record-mimo redact-check bench docker-build docker-test docker-matrix \
        docker-demo docker-console clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install replex in editable mode with dev extras
	$(PY) -m pip install -e ".[dev]"

test: ## Run the full test suite with coverage (gate: 90%)
	$(PY) -m pytest -q --cov=replex --cov-report=term-missing --cov-fail-under=90
	cd $(EXAMPLE) && $(PY) -m pytest -q

test-fast: ## Run the test suite in parallel without coverage
	$(PY) -m pytest -q -n auto

lint: ## Ruff lint + format check
	$(PY) -m ruff check src tests examples
	$(PY) -m ruff format --check src tests examples

format: ## Apply ruff formatting and safe fixes
	$(PY) -m ruff check --fix src tests examples
	$(PY) -m ruff format src tests examples

typecheck: ## mypy --strict on src/replex
	$(PY) -m mypy

build-console: ## Build the React console into src/replex/console/static
	cd console-ui && npm ci && npm run build

test-console: ## Run the console's vitest suite
	cd console-ui && npm ci && npm test

build: ## Build sdist and wheel into dist/
	$(PY) -m build

demo: ## Replay the example, run a drift check between prompt versions, write the HTML report
	cd $(EXAMPLE) && $(PY) demo.py

redact-check: ## Scan every committed cassette for secrets / personal data
	$(PY) -m replex.cli redact --check $(EXAMPLE)/cassettes
	$(PY) -m replex.cli redact --check tests/cassettes

bench: ## Measure decorated-tool overhead outside a session
	$(PY) -m pytest -q tests/test_benchmark.py -s

record-claude: ## Record REAL cassettes from Claude Sonnet 5.5 (needs ANTHROPIC_API_KEY)
	cd $(EXAMPLE) && $(PY) record_live.py claude

record-mimo: ## Record REAL cassettes from MiMo 2.6 (needs MIMO_API_KEY, MIMO_BASE_URL, MIMO_MODEL)
	cd $(EXAMPLE) && $(PY) record_live.py mimo

docker-build: ## Build the runtime image (console + CLI)
	docker build -t replex:latest .

docker-test: ## Run lint, types and the full test suite inside Docker
	docker build --target test -t replex:test .

docker-matrix: ## Run the test suite on every supported Python version in Docker
	@for v in $(PYTHON_VERSIONS); do \
	  echo "=== Python $$v ==="; \
	  docker build --target test --build-arg PYTHON_VERSION=$$v -t replex:test-py$$v . || exit 1; \
	done

docker-demo: ## Run `make demo` inside Docker (no API key)
	docker compose run --rm demo

docker-console: ## Serve the console for the example cassettes on http://127.0.0.1:8765
	docker compose up console

clean: ## Remove build and cache artefacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov .replex $(EXAMPLE)/.replex
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
