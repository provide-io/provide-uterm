# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the Go port. `make quality-gate` mirrors the Python-side
# ci/quality_checks.sh philosophy: everything CI runs, runnable locally.

GO ?= go
GOLANGCI_LINT ?= golangci-lint
# 95.0, not 100.0: the library packages are all at (or within a hair of) 100%.
# The lower whole-module floor accommodates the two integration packages whose
# residual lines are non-deterministic and cannot be covered without fault
# injection: server (~85% — WS/HTTP/SSE write-failure branches on live
# sockets, plus hub-API-dependent surfaces left as documented follow-ups) and
# cli (~96% — mid-bridge transport-send failures and OS/signal branches).
# transports (98.2%), client (99.7%), controlplane/sqlite (95.1%), and hub
# (98.4%) likewise carry only syscall/driver/JSON-encode guards the Python
# originals also exclude. Whole-module total measures ~97%.
COVER_THRESHOLD ?= 95.0

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
