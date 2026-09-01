# Alfred dev tasks. The two test suites live in two languages with two
# invocations (and one working-dir requirement); this is the single entry point.
#
#   make test        run both suites (Python + Raycast/TS)
#   make test-py     Python engine tests (pytest)
#   make test-ts     Raycast extension tests (vitest)
#   make lint        ruff (Python) + eslint (TS), best-effort
#   make typecheck   Raycast TypeScript typecheck
#   make dev         install dev dependencies into the venv
#   make coverage    Python coverage report
#   make quality     quality gate: fast pass over local changes (config in .quality/)
#   make quality-ship  the ship report — every gate; green before handoff

PY := ./.venv/bin/python
PIP := ./.venv/bin/pip
# The code-discipline skill that owns the quality-loop runner (override to a checkout).
QUALITY_SKILL ?= $(HOME)/.claude/skills/code-discipline

.PHONY: test test-py test-ts lint typecheck dev coverage quality quality-ship help

help:
	@sed -n 's/^#   //p' Makefile | sed '/^$$/q'

test: test-py test-ts
	@echo "✓ all suites passed"

test-py:
	@echo "── Python (pytest) ─────────────────────────────"
	$(PY) -m pytest tests/ -q

test-ts:
	@echo "── Raycast (vitest) ────────────────────────────"
	cd raycast && npx --no-install vitest run

typecheck:
	cd raycast && npm run typecheck

lint:
	@echo "── ruff ────────────────────────────────────────"
	-$(PY) -m ruff check voicebridge.py voicebridge tests/ .quality/dependency_edges.py
	@echo "── eslint (raycast) ────────────────────────────"
	-cd raycast && npm run lint:eslint

dev:
	$(PIP) install -r requirements-dev.txt

coverage:
	$(PY) -m coverage run -m pytest tests/ -q && $(PY) -m coverage report --include="*/voicebridge.py,*/voicebridge/*.py"

quality:
	python3 $(QUALITY_SKILL)/scripts/quality_loop.py --root . --local-changes --fast --no-install

quality-ship:
	python3 $(QUALITY_SKILL)/scripts/quality_loop.py --root . --local-changes --no-install
