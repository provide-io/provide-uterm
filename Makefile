.PHONY: quality quality-gate sync lint typecheck test frontend-test frontend-build

PY_PACKAGES := \
	packages/provide-uterm/src \
	packages/provide-uterm-annotation/src \
	packages/provide-uterm-client/src \
	packages/provide-uterm-platform/src \
	packages/provide-uterm-server/src \
	packages/provide-uterm-cloudflare/src

PY_TEST_PACKAGES := \
	packages/provide-uterm/tests \
	packages/provide-uterm-annotation/tests \
	packages/provide-uterm-client/tests \
	packages/provide-uterm-platform/tests \
	packages/provide-uterm-server/tests \
	packages/provide-uterm-cloudflare/tests

TY_PACKAGES := \
	packages/provide-uterm/src \
	packages/provide-uterm-annotation/src \
	packages/provide-uterm-client/src \
	packages/provide-uterm-platform/src \
	packages/provide-uterm-server/src
# Cloudflare runs under Pyodide/Workers with flat runtime imports and JS proxy
# objects; mypy gates it, while ty is gated on packages it can analyze reliably.

quality: lint typecheck frontend-test test

# The exact static checks CI's `quality` job runs (max-LOC, SPDX headers,
# codegen-frames drift, event literals, ruff, mypy/ty, bandit, xenon, vulture,
# pip-audit, licenses, performance smoke, CF vendor tree, package artifacts).
# Run before pushing so CI-only failures surface locally, not on the runner.
quality-gate:
	bash ci/quality_checks.sh

# Provision the environment the whole suite needs.
#
# `uv sync --group dev` is NOT enough, and it fails in a way that reads as a
# broken dependency rather than a broken environment. It resolves the ROOT
# project only, so workspace members' own dependencies are uninstalled --
# verified with `--dry-run`, which reports:
#
#     Would uninstall 2 packages
#      - provide-uterm-cloudflare==0.5.0
#      - psutil==7.2.2
#
# psutil belongs to provide-uterm-platform, and losing it breaks COLLECTION of
# packages/provide-uterm-platform/tests/pty/test_stability_stress.py with
# ModuleNotFoundError, so the run aborts before a single test executes.
#
# CI does not hit this because its jobs are per-package: the pty job runs its
# own `uv sync --frozen --package provide-uterm-platform --extra dev`. `make
# test` runs every package's tests from ONE venv, so it needs every package.
sync:
	uv sync --all-packages --all-extras --group dev

lint:
	uv run ruff check .

typecheck:
	uv run mypy $(PY_PACKAGES)
	uv run ty check $(TY_PACKAGES)

# Depends on `sync` so the suite provisions what it needs rather than trusting
# whatever the last `uv` command happened to leave behind.
test: sync
	@for pkg in $(PY_TEST_PACKAGES); do \
		pkg_dir=$$(dirname $$pkg); \
		pkg_tests=$$(basename $$pkg); \
		echo "==> pytest $$pkg (cwd=$$pkg_dir)"; \
		( cd $$pkg_dir && uv run pytest $$pkg_tests ) || exit $$?; \
	done

frontend-test:
	npm run typecheck:frontend
	npm run typecheck:app
	npm run test --workspace=packages/provide-uterm-frontend
	npm run test --workspace=packages/provide-uterm-app

frontend-build:
	npm run build:frontend
