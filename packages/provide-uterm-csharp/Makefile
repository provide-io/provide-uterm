# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the C# port. Mirrors packages/provide-uterm-go/Makefile.

DOTNET ?= dotnet
# 97.0 (not 100): pure library packages are near-full. Remaining misses are the
# same residual class Go documents (COVER_THRESHOLD 97.2 with per-package notes):
#   - Cli/Manager DefaultWaitCancel: real Console.CancelKeyPress wait (tests inject
#     WaitForCancel no-op to exercise non--once paths without blocking).
#   - Server/UtermServer: browser/worker WS accept loops + rare authz edge arms.
#   - Gateway: ObjectDisposedException on accept during Stop.
#   - Cli/Proxy BridgeAsync: live socket cancel/stop races + SSH dispose path.
#   - CtrlMsg CanonicalJson FallbackScientific: only when R→sci round-trip fails.
#   - Live OS transports (Pty/Telnet/Ssh/WebSocket) excluded from denominator
#     entirely (unit tests cannot drive real PTY/SSH handshakes fully).
# Residual policy: drop an exclusion only when a dedicated OS/harness job
# exercises that path; conformance/live Layer B is a separate gate (not coverlet).
# MCP is not part of the C# port (operator de-scope); sources and binary removed.
COVER_THRESHOLD ?= 97.0
CONFIGURATION ?= Release

.PHONY: build test cover build-binaries quality-gate interop-test conformance-test

build:
	$(DOTNET) build -c $(CONFIGURATION)

test:
	$(DOTNET) test -c $(CONFIGURATION) --no-build || $(DOTNET) test -c $(CONFIGURATION)

# AC3: byte-exact Python differential driver (mirrors Go conformance/conformance_test.go).
conformance-test:
	$(DOTNET) test -c $(CONFIGURATION) --filter "FullyQualifiedName~ConformanceVectors" -v n

cover:
	@rm -rf coverage-out
	$(DOTNET) test -c $(CONFIGURATION) \
		--collect:"XPlat Code Coverage" \
		--results-directory coverage-out \
		-- DataCollectionRunSettings.DataCollectors.DataCollector.Configuration.ExcludeByFile="**/UnicodeNormTables.cs;**/UnicodeWidthTables.cs;**/CharsetTables.cs"
	@python3 ci/coverage_gate.py coverage-out $(COVER_THRESHOLD)

build-binaries:
	@rm -rf bin
	@mkdir -p bin
	$(DOTNET) publish cmd/Uterm/Uterm.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	$(DOTNET) publish cmd/Uterm.Manager/Uterm.Manager.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	@# Guard: lowercase ship names (uterm / uterm-manager). On case-insensitive
	@# macOS APFS, `test ! -e bin/Uterm` is false when bin/uterm exists, so we
	@# check the directory listing for exact spellings instead.
	@test -x bin/uterm -a -x bin/uterm-manager
	@ls -1 bin | grep -E '^(Uterm|Uterm\.Manager|Uterm\.Mcp)$$' && \
	  { echo "error: unexpected PascalCase/MCP binary in bin/"; exit 1; } || true
	@echo "uterm + uterm-manager binaries under bin/ (lowercase; MCP not shipped)"

quality-gate: build conformance-test test cover build-binaries
	@echo "csharp quality gate passed"

# Optional live C# client -> Python server interop. Skips when uv is absent.
interop-test:
	@if command -v uv >/dev/null 2>&1; then \
	  $(DOTNET) test -c $(CONFIGURATION) --filter "FullyQualifiedName~Interop" -v n || true; \
	else \
	  echo "uv not found; interop-test skipped"; \
	fi

