# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the Go port. `make quality-gate` mirrors the Python-side
# ci/quality_checks.sh philosophy: everything CI runs, runnable locally.

GO ?= go
GOLANGCI_LINT ?= golangci-lint
# 99.5, not 100.0: every package is at 100% except transports (98.2%),
# client (99.7%), and controlplane/sqlite (95.1%), whose residual lines are
# non-deterministic syscall/driver-fault guards documented in the package
# reports (the Python originals exclude the same paths via pragma no cover).
# Whole-module total measures 99.7%.
COVER_THRESHOLD ?= 99.5

.PHONY: fmt fmt-check vet lint test race cover vuln quality-gate

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

quality-gate: fmt-check vet lint race cover
	@echo "go quality gate passed"
