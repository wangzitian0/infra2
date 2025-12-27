.PHONY: help install test test-smoke test-sso test-platform test-api test-database test-e2e clean lint format

help:
	@echo "E2E Regression Tests - Available Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install          - Install dependencies with uv"
	@echo "  make install-browser  - Install Playwright browsers"
	@echo ""
	@echo "Testing:"
	@echo "  make test             - Run all tests"
	@echo "  make test-smoke       - Quick smoke tests (< 5 min)"
	@echo "  make test-sso         - Portal SSO tests"
	@echo "  make test-platform    - Platform service tests"
	@echo "  make test-api         - API health tests"
	@echo "  make test-database    - Database connection tests"
	@echo "  make test-e2e         - Full E2E tests"
	@echo ""
	@echo "Development:"
	@echo "  make test-headed      - Run tests with visible browser"
	@echo "  make test-debug       - Run with verbose output and slower execution"
	@echo "  make report           - Generate HTML test report"
	@echo "  make lint             - Check code style"
	@echo "  make format           - Format code"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean            - Remove test artifacts"
	@echo "  make env              - Create .env file from template"

install:
	uv sync

install-browser:
	uv run playwright install chromium

test:
	uv run pytest -v

test-smoke:
	uv run pytest -m smoke -v

test-sso:
	uv run pytest -m sso -v

test-platform:
	uv run pytest -m platform -v

test-api:
	uv run pytest -m api -v

test-database:
	uv run pytest -m database -v

test-e2e:
	uv run pytest -m e2e -v

test-headed:
	HEADLESS=false uv run pytest -v

test-debug:
	SLOW_MO=1000 TIMEOUT_MS=60000 uv run pytest -vv -s

report:
	uv run pytest --html=report.html --self-contained-html
	@echo "Report generated: report.html"

lint:
	uv run pytest --lf -v

format:
	@echo "Formatting with Python's autopep8 (if needed)"
	@echo "TODO: Add code formatter configuration"

clean:
	rm -rf .pytest_cache __pycache__ test-results test-videos report.html
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

env:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from template. Please configure your environment variables."; \
	else \
		echo ".env already exists"; \
	fi

.DEFAULT_GOAL := help
