.PHONY: quality quality-gate sync lint typecheck test test-native-capture frontend-test frontend-build

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

# The capture library's LD_PRELOAD tests. CI never runs these in the same
# process as the rest of the platform suite -- it has a dedicated step for them
# and passes these same --ignore flags to the bulk pty step. Doing otherwise is
# flaky: run inside the full 1400-test platform suite they intermittently fail
# on a socket-collector timeout, and NOT always the same test
# (test_library_does_not_intercept_non_stdio_fds one run,
# test_copy_file_range_is_captured_when_stdout_is_a_file the next). Each passes
# consistently on its own, with or without coverage. These tests fork a process
# under the shim and wait on a Unix socket, so they are sensitive to whatever
# else the interpreter is carrying; 1400 tests' worth of residue is enough.
#
# So `make test` runs them the way CI does, rather than inventing a combination
# CI has never run and then treating its flakiness as a property of the code.
NATIVE_CAPTURE_TESTS := \
	tests/pty/test_ld_preload_capture.py \
	tests/pty/test_capture_frame_atomicity.py
NATIVE_CAPTURE_IGNORES := \
	--ignore=tests/pty/test_ld_preload_capture.py \
	--ignore=tests/pty/test_capture_frame_atomicity.py

# Depends on `sync` so the suite provisions what it needs rather than trusting
# whatever the last `uv` command happened to leave behind.
test: sync
	@for pkg in $(PY_TEST_PACKAGES); do \
		pkg_dir=$$(dirname $$pkg); \
		pkg_tests=$$(basename $$pkg); \
		echo "==> pytest $$pkg (cwd=$$pkg_dir)"; \
		if [ "$$pkg_dir" = "packages/provide-uterm-platform" ]; then \
			( cd $$pkg_dir && uv run pytest $$pkg_tests $(NATIVE_CAPTURE_IGNORES) ) || exit $$?; \
		else \
			( cd $$pkg_dir && uv run pytest $$pkg_tests ) || exit $$?; \
		fi; \
	done
	@$(MAKE) --no-print-directory test-native-capture

# Run separately, in their own interpreter, matching CI's dedicated step
# (--no-cov, importlib import mode, 30s cap). Requires the built shim:
#   make -C packages/provide-uterm-platform/native/capture clean test install
test-native-capture:
	@echo "==> pytest native capture (isolated, as CI runs it)"
	cd packages/provide-uterm-platform && uv run pytest $(NATIVE_CAPTURE_TESTS) \
		-q --no-cov --timeout=30 -o "addopts=--import-mode=importlib"

frontend-test:
	npm run typecheck:frontend
	npm run typecheck:app
	npm run test --workspace=packages/provide-uterm-frontend
	npm run test --workspace=packages/provide-uterm-app

frontend-build:
	npm run build:frontend
