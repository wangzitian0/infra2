#!/bin/bash
# E2E Regression Tests Runner
# Usage: ./run_tests.sh [test_type] [options]
#
# Examples:
#   ./run_tests.sh smoke                 # Run smoke tests
#   ./run_tests.sh sso --headed          # Run SSO tests with visible browser
#   ./run_tests.sh all                   # Run all tests
#   ./run_tests.sh install               # Install dependencies

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
TEST_TYPE="${1:-smoke}"
HEADED="${2:-}"
EXTRA_ARGS="${@:3}"

# Functions
print_usage() {
    cat <<EOF
Usage: $0 [test_type] [options]

Test Types:
  install      - Install dependencies
  smoke        - Quick smoke tests (default)
  sso          - SSO/Portal tests
  platform     - Platform service tests
  api          - API health tests
  database     - Database connection tests
  e2e          - Full E2E tests
  all          - Run all tests

Options:
  --headed     - Show browser during tests
  --debug      - Verbose output + slower execution
  --report     - Generate HTML report

Examples:
  $0 smoke
  $0 sso --headed
  $0 all --report
EOF
}

run_install() {
    echo -e "${YELLOW}Installing dependencies...${NC}"
    uv sync
    echo -e "${YELLOW}Installing browsers...${NC}"
    uv run playwright install chromium
    echo -e "${GREEN}✓ Installation complete${NC}"
}

run_tests() {
    local test_type="$1"
    local extra_args="$2"

    # Check if .env exists
    if [ ! -f .env ]; then
        echo -e "${YELLOW}⚠ .env file not found${NC}"
        echo "Creating from template..."
        cp .env.example .env
        echo -e "${YELLOW}Please configure .env before running tests${NC}"
        exit 1
    fi

    # Parse options
    local pytest_args="-v --tb=short"

    if [[ "$extra_args" == *"--headed"* ]]; then
        export HEADLESS=false
        echo -e "${YELLOW}Running with visible browser${NC}"
    fi

    if [[ "$extra_args" == *"--debug"* ]]; then
        export SLOW_MO=1000
        export TIMEOUT_MS=60000
        pytest_args="$pytest_args -vv -s"
        echo -e "${YELLOW}Running in debug mode (slower, verbose)${NC}"
    fi

    if [[ "$extra_args" == *"--report"* ]]; then
        pytest_args="$pytest_args --html=report.html --self-contained-html"
    fi

    echo -e "${YELLOW}Running ${test_type} tests...${NC}"
    echo ""

    case "$test_type" in
        install)
            run_install
            ;;
        smoke)
            uv run pytest -m smoke $pytest_args
            ;;
        sso)
            uv run pytest -m sso $pytest_args
            ;;
        platform)
            uv run pytest -m platform $pytest_args
            ;;
        api)
            uv run pytest -m api $pytest_args
            ;;
        database)
            uv run pytest -m database $pytest_args
            ;;
        e2e)
            uv run pytest -m e2e $pytest_args
            ;;
        all)
            uv run pytest $pytest_args
            ;;
        *)
            echo -e "${RED}Unknown test type: $test_type${NC}"
            print_usage
            exit 1
            ;;
    esac

    echo ""
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Tests passed${NC}"
        if [[ "$extra_args" == *"--report"* ]]; then
            echo -e "${GREEN}Report: report.html${NC}"
        fi
    else
        echo -e "${RED}✗ Tests failed${NC}"
        exit 1
    fi
}

# Main
if [ "$TEST_TYPE" = "-h" ] || [ "$TEST_TYPE" = "--help" ]; then
    print_usage
    exit 0
fi

run_tests "$TEST_TYPE" "$HEADED $EXTRA_ARGS"
