# rhoai-e2e pipeline unit tests (no oc / cluster required).
# Run from this directory: make test
# Or from repo root: make -C integration-tests/rhoai-e2e test

PYTHON ?= python3
PYTEST ?= pytest
# Per-test hard limit (pytest-timeout; also set in pytest.ini).
PYTEST_TIMEOUT ?= 10
# Parallel workers: auto (CPU count), a positive integer, or 0 for serial (make test-serial).
PYTEST_WORKERS ?= auto
# Verbose per-test lines + slowest-N timing summary at end; no progress dots.
PYTEST_ARGS ?= -v --tb=short --durations=20 --timeout=$(PYTEST_TIMEOUT)

.PHONY: help test test-serial test-cli deps

help:
	@echo "rhoai-e2e local unit tests (pytest; no Konflux cluster)"
	@echo ""
	@echo "  make deps         Install Python test deps (requirements.txt)"
	@echo "  make test         Run full suite in parallel (~2 min; verbose + 10s/test timeout)"
	@echo "  make test-serial  Debug-only single process (much slower; do not use routinely)"
	@echo "  make test-cli     Run CLI argument tests only"
	@echo ""
	@echo "Overrides: PYTEST_WORKERS=4 make test   PYTEST_TIMEOUT=60 make test"

deps:
	$(PYTHON) -m pip install -r requirements.txt

test:
ifeq ($(PYTEST_WORKERS),0)
	$(PYTEST) $(PYTEST_ARGS)
else
	$(PYTEST) $(PYTEST_ARGS) -n $(PYTEST_WORKERS) --dist loadscope
endif

test-serial:
	$(MAKE) test PYTEST_WORKERS=0

test-cli:
	$(PYTEST) $(PYTEST_ARGS) unit_tests/runners/test_rhoai_e2e_cli.py
