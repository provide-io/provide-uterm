# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the C# port. Mirrors packages/provide-uterm-go/Makefile.

DOTNET ?= dotnet
# 100.0 on Linux: every line outside the gate's exclusions (ci/coverage_gate.py
# -- Unix PTY P/Invoke, data tables, generated code) is executed by `make cover`.
# The platform-conditional arms are driven on either OS through
# OsPlatform.IsWindowsProbe, and the live-socket transports, gateways and relays
# run against loopback peers. A new uncovered line fails the gate: cover it, or
# delete it if it cannot run. The native PTY is excluded because its remaining
# arms are libc failure returns; `cover` also skips its batch (3d) on CI. Opt
# into it locally via PreferNativePty / UTERM_NATIVE_PTY=1.
#
# At 100 the gate fails on a single line, so a line that only runs when a race
# goes one way fails it on identical code. If it starts flapping, diff two
# cobertura runs and force the arm (as ResumeLifecycleIntegrationTests
# .ExpiryBehindAnInFlightTransitionQueuesBehindIt does) rather than lowering this.
#
# csharp-quality-windows passes its own COVER_THRESHOLD (see ci.yml): the
# Windows runner cannot execute the Unix-only lines that remain in the
# denominator, so it holds the measured Windows floor instead of 100.
COVER_THRESHOLD ?= 100.0
CONFIGURATION ?= Release

.PHONY: build test cover build-binaries quality-gate interop-test conformance-test mutation-gate

# -v normal so warnings reach the log the gate reads; -t:Rebuild because an
# incremental build re-emits nothing for the projects it skips, which would let
# a new warning pass the gate simply by not being recompiled.
#
# Via a file rather than a pipe: `set -o pipefail` is not in POSIX sh, and
# make's default shell is dash on Ubuntu, so a pipe would report the gate's
# status and swallow a failed build. obj/ is already ignored.
BUILD_LOG := obj/build-$(CONFIGURATION).log

build:
	@mkdir -p obj
	@$(DOTNET) build -c $(CONFIGURATION) -t:Rebuild -v normal --nologo > $(BUILD_LOG) 2>&1 \
	  || { cat $(BUILD_LOG); exit 1; }
	@grep -E ': (warning|error) [A-Z]+[0-9]+|Build succeeded|Build FAILED' $(BUILD_LOG) || true
	@python3 ci/warning_gate.py < $(BUILD_LOG)

# Serialize + batch test hosts. Ubuntu CI hits glibc free()/malloc crashes after
# ~300–400 tests in one process (native PTY/SSH residual). Fresh process per batch.
# Filters use class-name prefixes from --list-tests (no catch-all ~Term/~Coverage).
TEST_BATCH_1 := FullyQualifiedName~AnnotationDetector|FullyQualifiedName~AnsiColors|FullyQualifiedName~AuthMutationKill|FullyQualifiedName~ChannelsLineEditor|FullyQualifiedName~ChannelsMutationKill|FullyQualifiedName~Conformance|FullyQualifiedName~CtrlMsg|FullyQualifiedName~Defaults|FullyQualifiedName~Filters|FullyQualifiedName~Frames|FullyQualifiedName~Policy|FullyQualifiedName~StrictPolicy|FullyQualifiedName~Redaction|FullyQualifiedName~Sanitizer|FullyQualifiedName~Screen|FullyQualifiedName~Vt|FullyQualifiedName~DeckMux|FullyQualifiedName~ServerRecordingHttp|FullyQualifiedName~ServerRecordingRetire|FullyQualifiedName~ServerRecordingTimeout|FullyQualifiedName~RecordingInstance|FullyQualifiedName~ControlChannel|FullyQualifiedName~Egress|FullyQualifiedName~Sqlite|FullyQualifiedName~ControlPlaneGraphical|FullyQualifiedName~SharedCorpusParity|FullyQualifiedName~OutputCollector
TEST_BATCH_2 := FullyQualifiedName~Hub|FullyQualifiedName~BrowserWsRateLimit|FullyQualifiedName~Fanout|FullyQualifiedName~McpAuth|FullyQualifiedName~ResumeLifecycle|FullyQualifiedName~ResumeTokenStore|FullyQualifiedName~WebSocketCloseHandler|FullyQualifiedName~WebSocketFragmentation|FullyQualifiedName~WebSocketMessageReader|FullyQualifiedName~WorkerAdmission|FullyQualifiedName~WorkerBearerAuthentication|FullyQualifiedName~ServerIntegration|FullyQualifiedName~ServerConfig|FullyQualifiedName~ServerAuth|FullyQualifiedName~ServerObservabilityWiring|FullyQualifiedName~WebhookAuthorization|FullyQualifiedName~WebhookDelivery|FullyQualifiedName~JwksJwt|FullyQualifiedName~CfAccessJwt|FullyQualifiedName~Vnc|FullyQualifiedName~HumanRelay|FullyQualifiedName~HumanVnc|FullyQualifiedName~RfbInputFilter|FullyQualifiedName~ClientDefaults|FullyQualifiedName~Session|FullyQualifiedName~TermSession|FullyQualifiedName~ModuleSurface|FullyQualifiedName~SmokeSurface|FullyQualifiedName~MoreSurface|FullyQualifiedName~ServerFactoryControlPlane|FullyQualifiedName~BrowserWsAnonymousRefusal|FullyQualifiedName~WebhookShutdownRace|FullyQualifiedName~WebhookLateRegistration|FullyQualifiedName~ManagerStopRoute|FullyQualifiedName~SecurityHeadersSecurityPosture
# Native-heavy classes get their own process (batch 3 crashed Ubuntu after ~18
# tests when Cli+Pty+Gateway shared one host).
TEST_BATCH_3A := FullyQualifiedName~Cli
TEST_BATCH_3B := FullyQualifiedName~Embed|FullyQualifiedName~.Emulator.|FullyQualifiedName~Gui|FullyQualifiedName~Interop
TEST_BATCH_3C := FullyQualifiedName~Gateway|FullyQualifiedName~Shell|FullyQualifiedName~Tunnel|FullyQualifiedName~KnownHosts|FullyQualifiedName~SshPolicy
TEST_BATCH_3D := FullyQualifiedName~PtyAndUnicode|FullyQualifiedName~LiveParity
TEST_BATCH_4 := FullyQualifiedName~Coverage|FullyQualifiedName~DeepCoverage|FullyQualifiedName~HighCoverage|FullyQualifiedName~Over90|FullyQualifiedName~HubAndCli

XUNIT_ARGS := -- xUnit.ParallelizeAssembly=false xUnit.ParallelizeTestCollections=false xUnit.MaxParallelThreads=1

# One process per filter; retry once on host crash.
define DOTNET_TEST_BATCH
	@echo "=== test batch: $(1) ==="
	@$(DOTNET) test -c $(CONFIGURATION) --no-build --filter "$(2)" $(XUNIT_ARGS); \
	ec=$$?; \
	if [ $$ec -ne 0 ]; then \
	  echo "batch $(1) retry (ec=$$ec)…"; \
	  $(DOTNET) test -c $(CONFIGURATION) --filter "$(2)" $(XUNIT_ARGS); \
	fi
endef

test:
	$(call DOTNET_TEST_BATCH,1 library,$(TEST_BATCH_1))
	$(call DOTNET_TEST_BATCH,2 server/hub,$(TEST_BATCH_2))
	$(call DOTNET_TEST_BATCH,3a cli,$(TEST_BATCH_3A))
	$(call DOTNET_TEST_BATCH,3b embed/gui,$(TEST_BATCH_3B))
	$(call DOTNET_TEST_BATCH,3c gateway/shell/tunnel,$(TEST_BATCH_3C))
	@# Pty/LiveParity used to be skipped on CI: native interop crashed the test
	@# host with free()/malloc aborts "even in isolation". It was not a host
	@# quirk -- SpawnOpaqueBytes in NativeUnixPty.cs allocated 256 bytes for a
	@# posix_spawnattr_t that is 336 on glibc, so every native spawn ran 80 bytes
	@# past the end of its buffer. The abort surfaced later and elsewhere, which
	@# is why it read as a flaky host. Fixed; the batch runs everywhere Unix now.
	@if uname -s 2>/dev/null | grep -Eqv 'MINGW|MSYS|CYGWIN|Windows'; then \
	  $(MAKE) --no-print-directory _test_batch_3d; \
	else \
	  echo "=== test batch: 3d pty/live SKIPPED on Windows (no posix_spawn) ==="; \
	fi
	$(call DOTNET_TEST_BATCH,4 coverage-boost,$(TEST_BATCH_4))

.PHONY: _test_batch_3d
_test_batch_3d:
	$(call DOTNET_TEST_BATCH,3d pty/live,$(TEST_BATCH_3D))

# AC3: byte-exact Python differential driver (mirrors Go conformance/conformance_test.go).
conformance-test:
	$(DOTNET) test -c $(CONFIGURATION) --filter "FullyQualifiedName~ConformanceVectors" -v n

# Mutation gate. Mirrors Go packages/provide-uterm-go mutation-gate / Python
# mutmut perimeter. Requires python3 + dotnet. Two stages:
#   1. ci/mutation_gate.py    — the original hand-rolled &&/|| flipper, over
#      its own small perimeter (Policy/DeckMux/Colors/Filters/Sanitizer/
#      Redaction/Auth/Channels).
#   2. ci/stryker_gate.py     — dotnet-stryker (a local tool; see
#      .config/dotnet-tools.json), Roslyn-level mutation testing (boundary,
#      arithmetic, conditional, string, statement, …) over the egress guards,
#      webhook registry/delivery loop, and TOML config binder — see
#      tests/Provide.Uterm.Tests/stryker-config.json for the perimeter and
#      mutation_equivalents.toml for documented-equivalent survivors.
# NOT part of `quality-gate`: a full Stryker run pays a ~10-20 minute fixed
# whole-project instrumentation cost regardless of perimeter size (Go's
# gremlins gate is likewise kept separate for the same reason).
PYTHON ?= python3
# GATE_ARGS lets CI pass --changed-only/--base-ref (see ci/prepare_stryker_args.sh)
# to BOTH stages. Empty by default, so a bare `make mutation-gate` still runs the
# full perimeter. Both stages are per-mutant expensive -- the flipper rebuilds the
# test project once per mutant, and Stryker pays a fixed whole-project
# instrumentation cost -- so both need the narrowing, not just Stryker.
GATE_ARGS ?=
mutation-gate:
	$(PYTHON) ci/mutation_gate.py $(GATE_ARGS)
# dotnet-stryker is a LOCAL tool (.config/dotnet-tools.json), so it does not
# exist until restored. Without this, `dotnet stryker` exits 1 — the same code
# it uses for "below threshold" — and the gate reads a tool that never ran as
# a mutation result.
	$(DOTNET) tool restore
	$(PYTHON) ci/stryker_gate.py $(GATE_ARGS)

define DOTNET_COVER_BATCH
	@echo "=== cover batch: $(1) ==="
	@$(DOTNET) test -c $(CONFIGURATION) --filter "$(2)" \
	  --collect:"XPlat Code Coverage" --results-directory coverage-out \
	  -- DataCollectionRunSettings.DataCollectors.DataCollector.Configuration.ExcludeByFile="**/UnicodeNormTables.cs;**/UnicodeWidthTables.cs;**/CharsetTables.cs" \
	  xUnit.ParallelizeAssembly=false xUnit.ParallelizeTestCollections=false xUnit.MaxParallelThreads=1
endef

cover:
	@rm -rf coverage-out
	$(call DOTNET_COVER_BATCH,1,$(TEST_BATCH_1))
	$(call DOTNET_COVER_BATCH,2,$(TEST_BATCH_2))
	$(call DOTNET_COVER_BATCH,3a,$(TEST_BATCH_3A))
	$(call DOTNET_COVER_BATCH,3b,$(TEST_BATCH_3B))
	$(call DOTNET_COVER_BATCH,3c,$(TEST_BATCH_3C))
	@if [ -z "$$CI" ] && uname -s 2>/dev/null | grep -Eqv 'MINGW|MSYS|CYGWIN|Windows'; then \
	  $(MAKE) --no-print-directory _cover_batch_3d; \
	else \
	  echo "=== cover batch: 3d pty/live SKIPPED on CI/Windows ==="; \
	fi
	$(call DOTNET_COVER_BATCH,4,$(TEST_BATCH_4))
	@python3 ci/coverage_gate.py coverage-out $(COVER_THRESHOLD)

.PHONY: _cover_batch_3d
_cover_batch_3d:
	$(call DOTNET_COVER_BATCH,3d,$(TEST_BATCH_3D))

# One `cover` batch on its own, under the same collector and the same
# serialization the full target uses. `cover` is the only place a couple of
# timing-sensitive tests have ever failed, and reproducing that needs the
# instrumentation and the single-threaded run, not just the test -- but it does
# not need the other six batches. BATCH names the suffix: `BATCH=2` runs
# TEST_BATCH_2. No coverage floor is applied; one batch cannot meet it.
.PHONY: cover-batch
cover-batch:
	@rm -rf coverage-out
	$(call DOTNET_COVER_BATCH,$(BATCH),$(TEST_BATCH_$(BATCH)))

build-binaries:
	@rm -rf bin
	@mkdir -p bin
	$(DOTNET) publish cmd/Uterm/Uterm.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	$(DOTNET) publish cmd/Uterm.Manager/Uterm.Manager.csproj -c $(CONFIGURATION) -o bin --no-self-contained
	@# Guard: lowercase ship names (uterm / uterm-manager). On case-insensitive
	@# macOS APFS, `test ! -e bin/Uterm` is false when bin/uterm exists, so we
	@# check the directory listing for exact spellings instead.
	@# Windows publish emits uterm.exe / uterm-manager.exe.
	@if [ -x bin/uterm ] && [ -x bin/uterm-manager ]; then \
	  :; \
	elif [ -f bin/uterm.exe ] && [ -f bin/uterm-manager.exe ]; then \
	  :; \
	else \
	  echo "error: missing uterm / uterm-manager ship binaries under bin/"; \
	  ls -la bin || true; \
	  exit 1; \
	fi
	@ls -1 bin | grep -E '^(Uterm|Uterm\.Manager|Uterm\.Mcp)(\.exe)?$$' && \
	  { echo "error: unexpected PascalCase/MCP binary in bin/"; exit 1; } || true
	@echo "uterm + uterm-manager binaries under bin/ (lowercase; MCP not shipped)"

# Every test must be selected by some TEST_BATCH_* filter, or `test` and
# `cover` build it and never run it (ci/check_test_batches.py says why). Via a
# file for the same dash/pipefail reason as BUILD_LOG.
LIST_TESTS_LOG := obj/list-tests-$(CONFIGURATION).log

.PHONY: test-batches
test-batches:
	@mkdir -p obj
	@$(DOTNET) test -c $(CONFIGURATION) --no-build --list-tests > $(LIST_TESTS_LOG) 2>&1 \
	  || { cat $(LIST_TESTS_LOG); exit 1; }
	@python3 ci/check_test_batches.py Makefile < $(LIST_TESTS_LOG)

quality-gate: build test-batches conformance-test test cover build-binaries
	@echo "csharp quality gate passed"

# Optional live C# client -> Python server interop. Skips when uv is absent.
interop-test:
	@if command -v uv >/dev/null 2>&1; then \
	  $(DOTNET) test -c $(CONFIGURATION) --filter "FullyQualifiedName~Interop" -v n || true; \
	else \
	  echo "uv not found; interop-test skipped"; \
	fi
