//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"log/slog"
	"os/signal"
	"strconv"
	"strings"
	"syscall"

	"github.com/spf13/cobra"

	ptel "github.com/provide-io/provide-telemetry/go"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/bootstrap"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// serverBundle is the assembled server plus the collaborators whose lifecycle
// the CLI owns (the control-plane engine and the resolved config/logger).
type serverBundle struct {
	srv      *server.Server
	engine   cp.Engine
	cfg      *serverconfig.UtermServerConfig
	logger   *slog.Logger
	devToken string
	registry *SessionRegistryImpl
}

// newServerCmd registers the `server` subcommand — the primary command. It
// mirrors the Python `uterm server` flags exactly (--config/--host/--port).
func newServerCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "server",
		Short:        "run the reference hosted terminal server",
		Long:         "Run the provide-uterm reference server (Go port of the FastAPI + TermHub stack).",
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			configPath, _ := cmd.Flags().GetString("config")
			host, _ := cmd.Flags().GetString("host")
			port, _ := cmd.Flags().GetInt("port")
			frontendDir, _ := cmd.Flags().GetString("frontend-dir")
			return runServer(cmd.Context(), configPath, host, port, frontendDir)
		},
	}
	f := cmd.Flags()
	f.String("config", "", "Path to a TOML config file")
	f.String("host", "", "Override the bind host")
	f.Int("port", 0, "Override the bind port")
	f.String("frontend-dir", "", "Path to the built frontend assets directory "+
		"(packages/provide-uterm-server/src/provide/uterm/server/frontend after `npm run build:frontend`); "+
		"empty serves a minimal vanilla shell with no static asset mount")
	return cmd
}

// applyServerOverrides applies --host/--port and re-derives public_base_url
// exactly like server/cli.py: host overrides when non-empty, port when
// non-zero, and public_base_url is rebuilt (scheme preserved) if either changed.
func applyServerOverrides(cfg *serverconfig.UtermServerConfig, host string, port int) {
	if host != "" {
		cfg.Server.Host = host
	}
	if port != 0 {
		cfg.Server.Port = port
	}
	if host != "" || port != 0 {
		scheme := "http"
		if strings.HasPrefix(cfg.Server.PublicBaseURL, "https://") {
			scheme = "https"
		}
		cfg.Server.PublicBaseURL = scheme + "://" + cfg.Server.Host + ":" + strconv.Itoa(cfg.Server.Port)
	}
}

// buildServer assembles a runnable server from config + CLI overrides. It sets
// up telemetry (the app layer — the one place SetupTelemetry belongs), builds
// the authenticator/authz, TermHub, control-plane engine, and a concrete
// SessionRegistry, and returns the wired server without binding a socket.
func buildServer(ctx context.Context, configPath, host string, port int, frontendDir string) (*serverBundle, error) {
	cfg, err := serverconfig.LoadServerConfig(configPath)
	if err != nil {
		return nil, err
	}
	applyServerOverrides(cfg, host, port)

	// App layer: initialise telemetry once, then thread the logger through.
	if _, err := ptel.SetupTelemetry(); err != nil {
		return nil, err
	}
	logger := ptel.GetLogger(ctx, "provide.uterm")

	apiKeys := serverauth.NewApiKeyStore()
	auth, devToken, err := buildAuthenticator(cfg, apiKeys)
	if err != nil {
		return nil, err
	}

	metrics := server.NewMetrics()
	clock := hub.NewRealClock()
	bus := hub.NewEventBus(hub.EventBusOptions{})
	h := hub.NewTermHub(hub.TermHubConfig{
		Clock:                      clock,
		Logger:                     logger,
		EventBus:                   bus,
		OnMetric:                   metrics.Inc,
		WorkerToken:                cfg.Auth.WorkerBearerToken,
		WorkerFrameOnInvalid:       cfg.WorkerFrameOnInvalid,
		BrowserRateLimitPerSec:     cfg.BrowserRateLimitPerSec,
		MaxConnectionsPerPrincipal: cfg.MaxConnectionsPerPrincipal,
		MaxWorkers:                 cfg.MaxWorkers,
		// Match Python create_server_app: mint resume tokens on browser connect.
		ResumeStore: hub.NewInMemoryResumeStore(clock, nil),
		ResumeTTLS:  300,
		// Wire the real StreamRedactor as the output-redaction seam. It stays
		// dormant until an OutputPolicyGate yields rules (none by default, matching
		// the Python NoOp-gate default), so live output is unchanged unless a gate
		// is configured; when one is, snapshots + broadcasts redact per recipient.
		Redactor: hub.RedactFrameFields,
	})

	engine, err := bootstrap.New(controlPlaneConfig(cfg))
	if err != nil {
		return nil, err
	}
	if err := engine.Open(ctx); err != nil {
		return nil, err
	}

	registry := NewSessionRegistry(cfg)
	// Long-poll events/watch uses the same EventBus as SSE.
	registry.SetEventBus(bus)

	graphicalTargets, err := server.SeedGraphicalTargets(cfg)
	if err != nil {
		_ = engine.Close(ctx)
		return nil, err
	}

	srv, err := server.New(server.Deps{
		Hub:              h,
		Auth:             auth,
		Authz:            serverauth.NewAuthorizationServiceFromConfig(cfg),
		Config:           cfg,
		Registry:         registry,
		APIKeys:          apiKeys,
		GraphicalTargets: graphicalTargets,
		Metrics:          metrics,
		Clock:            clock,
		Version:          Version,
		Logger:           logger,
		Recording:        buildRecordingStore(cfg),
		FrontendDir:      frontendDir,
	})
	if err != nil {
		_ = engine.Close(ctx)
		return nil, err
	}

	// PAM integration (opt-in): a no-op unless pam.notify_socket is set. Started
	// on the server context so it stops on shutdown. Port of run_pam_integration.
	if cfg.Pam.NotifySocket != nil && *cfg.Pam.NotifySocket != "" {
		pam := server.NewPamIntegration(cfg.Pam, registry, nil, logger)
		go func() {
			if err := pam.Run(ctx); err != nil {
				logger.Warn("pam_integration_error", "error", err)
			}
		}()
	}
	return &serverBundle{srv: srv, engine: engine, cfg: cfg, logger: logger, devToken: devToken, registry: registry}, nil
}

// buildRecordingStore selects the recording store from config. Port of the
// factory's recording-store selection: a local JSONL store rooted at the
// configured directory, an in-memory store, or a no-op NullStore.
func buildRecordingStore(cfg *serverconfig.UtermServerConfig) recording.Store {
	switch cfg.Recording.StoreType {
	case "local":
		return recording.NewLocalFileStore(cfg.Recording.Directory)
	case "memory":
		return recording.NewInMemoryStore()
	default:
		return recording.NullStore{}
	}
}

// controlPlaneConfig maps the server config's control-plane section onto the
// control-plane package Config.
func controlPlaneConfig(cfg *serverconfig.UtermServerConfig) cp.Config {
	dbURL := ""
	if cfg.ControlPlane.DatabaseURL != nil {
		dbURL = *cfg.ControlPlane.DatabaseURL
	}
	return cp.Config{Backend: cp.Backend(cfg.ControlPlane.Backend), DatabaseURL: dbURL}
}

// runServer builds the server and runs it until SIGINT/SIGTERM, then shuts down
// gracefully (HTTP drain + hub shutdown via server.Run, then the engine).
func runServer(ctx context.Context, configPath, host string, port int, frontendDir string) error {
	if ctx == nil {
		ctx = context.Background()
	}
	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	bundle, err := buildServer(sigCtx, configPath, host, port, frontendDir)
	if err != nil {
		return err
	}
	defer func() { _ = bundle.engine.Close(context.Background()) }()

	bundle.logger.Info("uterm_server_start",
		"addr", bundle.srv.Addr(),
		"public_base_url", bundle.cfg.Server.PublicBaseURL,
		"auth_mode", bundle.cfg.Auth.Mode)
	if bundle.devToken != "" {
		bundle.logger.Info("uterm_dev_token_issued", "token", bundle.devToken)
	}

	// Spawn auto_start sessions in the background so a slow/failed connector dial
	// never blocks the server from listening (mirrors the Python lifespan boot
	// task registry.start_auto_start_sessions).
	go bundle.registry.StartAutoStartSessions(sigCtx)

	return bundle.srv.Run(sigCtx)
}
