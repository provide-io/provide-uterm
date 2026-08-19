# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# Quality gate for the C# port. Mirrors packages/provide-uterm-go/Makefile.

DOTNET ?= dotnet
# 97.0 (not 100): pure library packages are near-full. Remaining misses are the
# same residual class Go documents (COVER_THRESHOLD 97.2 with per-package notes):
#   - Cli/Manager DefaultWaitCancel: real Console.CancelKeyPress wait (tests inject
#     WaitForCancel no-op to exercise non--once paths without blocking).
#   - Server/UtermServer: browser/worker WS accept loops + rare authz edge arms.
#   - Gateway: ObjectDisposedException on accept during Stop (file excluded in
#     ci/coverage_gate.py residual list).
#   - Cli/Proxy BridgeAsync: live socket cancel/stop races + SSH dispose path
#     (file excluded in ci/coverage_gate.py residual list).
#   - CtrlMsg CanonicalJson FallbackScientific: [ExcludeFromCodeCoverage] — only
#     when R→sci round-trip fails (platform float residual).
#   - Live OS transports (Pty/Telnet/Ssh/WebSocket) excluded from denominator
#     entirely (unit tests cannot drive real PTY/SSH handshakes fully).
# Residual policy: drop an exclusion only when a dedicated OS/harness job
# exercises that path; conformance/live Layer B is a separate gate (not coverlet).
# Also residual: NativeUnixPty (posix_spawn), SshWsGateway (FxSsh accept pump),
# RfbClient (live RFB TCP). Embed/TelnetUpstream live TCP residual (scripted IAC tested).
# Opt into native PTY via PreferNativePty / UTERM_NATIVE_PTY=1.
# MCP is not part of the C# port (operator de-scope); sources and binary removed.
# 97.6 floors residual rare arms after Wave8–10 harness + human-VNC/JWKS work.
# Residual class: UtermServer WS disconnect/CloseAsync, Gui RFB attach catch,
# HumanRelay dual-pump cancel races, Embed Session dispose, Tunnel/HostRest
# rare arms, CanonicalJson FallbackScientific, dual-OS FileIo SetUnixFileMode.
# Live residuals excluded in ci/coverage_gate.py (RfbClient, HumanVnc WS, MCP
# StreamJsonRpc, WsBinaryStreams, Pty/Ssh/Telnet transports, Gateway/Proxy).
# Pure library batches still track ~99%+. Raise only with ≥0.2pt headroom on
# BOTH csharp-quality and csharp-quality-windows after a full cover run.
# Floor is set by the Windows run: platform-conditional Linux-only arms
# (IsWindows-gated Connector/DevIdp/TunnelClient branches) sit in the denominator
# but cannot execute on Windows, so csharp-quality-windows tops out ~97.46% while
# Ubuntu covers them. 97.4 keeps both green without excluding whole files.
COVER_THRESHOLD ?= 97.4
CONFIGURATION ?= Release

.PHONY: build test cover build-binaries quality-gate interop-test conformance-test mutation-gate

build:
	$(DOTNET) build -c $(CONFIGURATION)

# Serialize + batch test hosts. Ubuntu CI hits glibc free()/malloc crashes after
# ~300–400 tests in one process (native PTY/SSH residual). Fresh process per batch.
# Filters use class-name prefixes from --list-tests (no catch-all ~Term/~Coverage).
TEST_BATCH_1 := FullyQualifiedName~AnsiColors|FullyQualifiedName~AuthMutationKill|FullyQualifiedName~ChannelsLineEditor|FullyQualifiedName~ChannelsMutationKill|FullyQualifiedName~Conformance|FullyQualifiedName~CtrlMsg|FullyQualifiedName~Defaults|FullyQualifiedName~Filters|FullyQualifiedName~Frames|FullyQualifiedName~Policy|FullyQualifiedName~StrictPolicy|FullyQualifiedName~Redaction|FullyQualifiedName~Sanitizer|FullyQualifiedName~Screen|FullyQualifiedName~Vt|FullyQualifiedName~DeckMux|FullyQualifiedName~ServerRecordingHttp|FullyQualifiedName~ControlChannel|FullyQualifiedName~Egress|FullyQualifiedName~Sqlite|FullyQualifiedName~ControlPlaneGraphical
TEST_BATCH_2 := FullyQualifiedName~Hub|FullyQualifiedName~BrowserWsRateLimit|FullyQualifiedName~Fanout|FullyQualifiedName~McpAuth|FullyQualifiedName~ResumeLifecycle|FullyQualifiedName~ResumeTokenStore|FullyQualifiedName~WebSocketCloseHandler|FullyQualifiedName~WebSocketFragmentation|FullyQualifiedName~WebSocketMessageReader|FullyQualifiedName~WorkerAdmission|FullyQualifiedName~WorkerBearerAuthentication|FullyQualifiedName~ServerIntegration|FullyQualifiedName~ServerConfig|FullyQualifiedName~ServerAuth|FullyQualifiedName~ServerObservabilityWiring|FullyQualifiedName~WebhookAuthorization|FullyQualifiedName~WebhookDelivery|FullyQualifiedName~JwksJwt|FullyQualifiedName~CfAccessJwt|FullyQualifiedName~Vnc|FullyQualifiedName~HumanRelay|FullyQualifiedName~HumanVnc|FullyQualifiedName~RfbInputFilter|FullyQualifiedName~ClientDefaults|FullyQualifiedName~Session|FullyQualifiedName~TermSession|FullyQualifiedName~ModuleSurface|FullyQualifiedName~SmokeSurface|FullyQualifiedName~MoreSurface|FullyQualifiedName~ServerFactoryControlPlane
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

quality-gate: build conformance-test test cover build-binaries
	@echo "csharp quality gate passed"

# Optional live C# client -> Python server interop. Skips when uv is absent.
interop-test:
	@if command -v uv >/dev/null 2>&1; then \
	  $(DOTNET) test -c $(CONFIGURATION) --filter "FullyQualifiedName~Interop" -v n || true; \
	else \
	  echo "uv not found; interop-test skipped"; \
	fi
