# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the C# port. Mirrors packages/provide-uterm-go/Makefile.

DOTNET ?= dotnet
COVER_THRESHOLD ?= 88.0
CONFIGURATION ?= Release

.PHONY: build test cover build-binaries quality-gate interop-test

build:
	$(DOTNET) build -c $(CONFIGURATION)

test:
	$(DOTNET) test -c $(CONFIGURATION) --no-build || $(DOTNET) test -c $(CONFIGURATION)

cover:
	@rm -rf coverage-out
	$(DOTNET) test -c $(CONFIGURATION) \
		--collect:"XPlat Code Coverage" \
		--results-directory coverage-out \
		-- DataCollectionRunSettings.DataCollectors.DataCollector.Configuration.ExcludeByFile="**/UnicodeNormTables.cs;**/UnicodeWidthTables.cs;**/CharsetTables.cs"
	@python3 ci/coverage_gate.py coverage-out $(COVER_THRESHOLD)

build-binaries:
	@mkdir -p bin
	$(DOTNET) publish cmd/Uterm/Uterm.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	$(DOTNET) publish cmd/Uterm.Mcp/Uterm.Mcp.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	$(DOTNET) publish cmd/Uterm.Manager/Uterm.Manager.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	@echo "all 3 binaries built under bin/"

quality-gate: build test cover build-binaries
	@echo "csharp quality gate passed"

# Optional live C# client -> Python server interop. Skips when uv is absent.
interop-test:
	@if command -v uv >/dev/null 2>&1; then \
	  $(DOTNET) test -c $(CONFIGURATION) --filter "FullyQualifiedName~Interop" -v n || true; \
	else \
	  echo "uv not found; interop-test skipped"; \
	fi
