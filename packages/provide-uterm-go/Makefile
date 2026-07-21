# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the Go port. `make quality-gate` mirrors the Python-side
# ci/quality_checks.sh philosophy: everything CI runs, runnable locally.

GO ?= go
GOLANGCI_LINT ?= golangci-lint
# 97.0, not 100.0: the library/wire packages are all at (or within a hair of)
# 100%. A pass across manager/server/gateway/cli proved the "these need fault
# injection" assumption largely wrong — real child processes and real live
# sockets (httptest, ephemeral-port listeners, actual subprocess teardown)
# drove all four from the 77-91% range up to 94-99%. What is left in each is
# now genuinely fault-injection-only (a specific syscall failing in a way a
# test cannot trigger):
#   - manager (~94%): syscall-failure branches (rename/write/MkdirAll errors,
#     log rotation) in the monitor loop's reconciliation.
#   - server (~95%): frame-encoder/deckmux internal-error log arms; handshake
#     mid-write races; OS/socket-level Serve() failure branches; residual WS
#     resume/deckmux paths.
#   - gateway (~99%): a handful of crypto/rand and channel-accept error arms.
#   - cli (~98%): external-constructor failures (telemetry/engine wiring),
#     os.Hostname()/json.Marshal(fixed map) failure arms.
#   - pty (~95.6% on macOS, higher on Linux CI — SO_PEERCRED is Linux-only):
#     real-libpam success paths (the fail-closed stub can never authenticate),
#     /etc/passwd read failures, and a few accept-vs-Stop/queue-drain races
#     that only manifest under adversarial scheduling.
# transports/client/controlplane-sqlite/hub carry only syscall/driver/
# JSON-encode guards the Python originals also exclude. Whole-module ~98.0%
# after browser resume/deck + timeseries/swarm strip + no-worker REST paths;
# cover profile filters cmd/ (see cover target). Floor
# 97.5 leaves ~0.5pt headroom.
# Measured ~98.0% after GUI/MCP/embed residual waves; keep 0.2pt dual-runner headroom.
COVER_THRESHOLD ?= 97.8

# Driver for the mutation gate (stdlib-only Python >= 3.11; tomllib + json).
PYTHON ?= python3

.PHONY: fmt fmt-check vet lint test race cover vuln quality-gate build-binaries interop-test mutation-gate

fmt:
	gofmt -w .

fmt-check:
	@out=$$(gofmt -l .); if [ -n "$$out" ]; then echo "gofmt needed on:"; echo "$$out"; exit 1; fi

vet:
	$(GO) vet ./...

lint:
	$(GOLANGCI_LINT) run ./...

test:
	$(GO) test ./...

race:
	$(GO) test -race ./...

# Cover libraries only. Denominator filters:
#   - cmd/ mains and demo programs (validated by build-binaries / manual demos)
cover:
	@rm -f coverage.raw coverage.out
	@$(GO) test -coverprofile=coverage.raw -covermode=atomic ./...
	@test -s coverage.raw || { echo "cover: empty coverage.raw"; exit 1; }
	@head -1 coverage.raw > coverage.out
	@grep -E '^(mode:|.*\.go:)' coverage.raw | grep -vE '(/cmd/)' | grep -v '^mode:' >> coverage.out || true
	@total=$$($(GO) tool cover -func=coverage.out | awk '/^total:/ {gsub("%","",$$3); print $$3}'); \
	echo "total coverage: $${total:-0}% (threshold $(COVER_THRESHOLD)%)"; \
	if [ -z "$$total" ]; then echo "cover: could not parse total"; exit 1; fi; \
	awk -v t="$$total" -v min="$(COVER_THRESHOLD)" 'BEGIN { exit (t+0 < min+0) ? 1 : 0 }' || \
	  { echo "coverage below threshold"; $(GO) tool cover -func=coverage.out | grep -v '100.0%' | head -40; exit 1; }

vuln:
	$(GO) run golang.org/x/vuln/cmd/govulncheck@latest ./...

# Builds all 3 shipped binaries into a throwaway dir so a cmd/ build break
# fails this step by name instead of hiding inside quality-gate's coverage run.
build-binaries:
	@mkdir -p bin
	$(GO) build -o bin/uterm ./cmd/uterm
	$(GO) build -o bin/uterm-mcp ./cmd/uterm-mcp
	$(GO) build -o bin/uterm-manager ./cmd/uterm-manager
	@echo "all 3 binaries built"

quality-gate: fmt-check vet lint race cover
	@echo "go quality gate passed"

# Live Go↔Python runtime interop proof. Kept SEPARATE from quality-gate (and
# from race/cover) on purpose: it starts a real Python `uterm server` via
# `uv run`, so it needs a Python toolchain + synced deps that the rest of the
# Go gate deliberately does not. It skips gracefully when uv is absent, but the
# gate must never depend on Python being present.
interop-test:
	$(GO) test ./interop/... -run TestLivePythonInterop -count=1 -v

# Mutation gate for a SMALL perimeter of already-~100%-covered pure-function
# packages (sanitizer, colors, filters, lineeditor). Kept SEPARATE from
# quality-gate: mutation testing recompiles+reruns the test binary once per
# covered mutant, so it is minutes-slow versus seconds for `go test`, and it
# needs `python3` to drive gremlins + enforce the equivalent-mutant allowlist.
# Enforces killed==100% minus the documented equivalents in
# mutation_equivalents.toml. gremlins is fetched via `go run` (no go.mod entry).
mutation-gate:
	$(PYTHON) ci/mutation_gate.py
