.PHONY: quality lint typecheck test frontend-test frontend-build

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

lint:
	uv run ruff check .

typecheck:
	uv run mypy $(PY_PACKAGES)
	uv run ty check $(TY_PACKAGES)

test:
	@for pkg in $(PY_TEST_PACKAGES); do \
		echo "==> pytest $$pkg"; \
		uv run pytest $$pkg || exit $$?; \
	done

frontend-test:
	npm run typecheck:frontend
	npm run typecheck:app
	npm run test --workspace=packages/provide-uterm-frontend
	npm run test --workspace=packages/provide-uterm-app

frontend-build:
	npm run build:frontend
