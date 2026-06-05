.PHONY: quality quality-gate lint typecheck test frontend-test frontend-build

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

lint:
	uv run ruff check .

typecheck:
	uv run mypy $(PY_PACKAGES)
	uv run ty check $(TY_PACKAGES)

test:
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
