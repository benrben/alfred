# Alfred dev tasks. The three test suites live in three languages with three
# invocations (and two working-dir requirements); this is the single entry point.
#
#   make test        run all three suites (Python + Lua + Raycast/TS)
#   make test-py     Python engine tests (pytest)
#   make test-lua    Hammerspoon pure-helper tests (plain lua)
#   make test-ts     Raycast extension tests (vitest)
#   make lint        ruff (Python) + luacheck (Lua) + eslint (TS), best-effort
#   make typecheck   Raycast TypeScript typecheck
#   make dev         install dev dependencies into the venv
#   make coverage    Python coverage report

PY := ./.venv/bin/python
PIP := ./.venv/bin/pip

.PHONY: test test-py test-lua test-ts lint typecheck dev coverage help

help:
	@sed -n 's/^#   //p' Makefile | sed '/^$$/q'

test: test-py test-lua test-ts
	@echo "✓ all suites passed"

test-py:
	@echo "── Python (pytest) ─────────────────────────────"
	$(PY) -m pytest tests/ -q

test-lua:
	@echo "── Lua (plain lua) ─────────────────────────────"
	VB_LUA_TEST=1 lua tests/lua/test_helpers.lua

test-ts:
	@echo "── Raycast (vitest) ────────────────────────────"
	cd raycast && npx --no-install vitest run

typecheck:
	cd raycast && npm run typecheck

lint:
	@echo "── ruff ────────────────────────────────────────"
	-$(PY) -m ruff check voicebridge.py tests/
	@echo "── luacheck ────────────────────────────────────"
	-luacheck voicebridge.lua tests/lua/ 2>/dev/null || echo "(luacheck not installed — brew install luacheck)"
	@echo "── eslint (raycast) ────────────────────────────"
	-cd raycast && npm run lint:eslint

dev:
	$(PIP) install -r requirements-dev.txt

coverage:
	$(PY) -m coverage run -m pytest tests/ -q && $(PY) -m coverage report --include="*/voicebridge.py"
