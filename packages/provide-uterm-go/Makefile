# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the Go port. `make quality-gate` mirrors the Python-side
# ci/quality_checks.sh philosophy: everything CI runs, runnable locally.

GO ?= go
GOLANGCI_LINT ?= golangci-lint
# 92.0, not 100.0: the library/wire packages are all at (or within a hair of)
# 100%. The lower whole-module floor accommodates the integration packages
# whose residual lines are non-deterministic and cannot be covered without
# fault injection or elaborate process/socket harnesses:
#   - manager (~77%): the agent-fleet supervisor's monitor loop (desired-state
#     reconciliation, queued-launch/stale handling, log cleanup, restart
#     respawn) needs live multi-process orchestration to exercise.
#   - server (~86%): WS/HTTP/SSE write-failure branches on live sockets.
#   - gateway (~86%): telnet/SSH listener accept + raw-mode / signal branches.
#   - cli (~91%): mid-bridge transport-send failures and OS/signal branches.
# transports/client/controlplane-sqlite/hub/pty carry only syscall/driver/
# JSON-encode guards the Python originals also exclude. Whole-module ~93.6%.
COVER_THRESHOLD ?= 92.0

.PHONY: fmt fmt-check vet lint test race cover vuln quality-gate build-binaries interop-test

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

cover:
	@$(GO) test -coverprofile=coverage.out -covermode=atomic ./... > /dev/null
	@total=$$($(GO) tool cover -func=coverage.out | awk '/^total:/ {gsub("%","",$$3); print $$3}'); \
	echo "total coverage: $$total% (threshold $(COVER_THRESHOLD)%)"; \
	awk -v t="$$total" -v min="$(COVER_THRESHOLD)" 'BEGIN { exit (t+0 < min+0) ? 1 : 0 }' || \
	  { echo "coverage below threshold"; $(GO) tool cover -func=coverage.out | grep -v '100.0%'; exit 1; }

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
