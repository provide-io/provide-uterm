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
			return runServer(cmd.Context(), configPath, host, port)
		},
	}
	f := cmd.Flags()
	f.String("config", "", "Path to a TOML config file")
	f.String("host", "", "Override the bind host")
	f.Int("port", 0, "Override the bind port")
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
func buildServer(ctx context.Context, configPath, host string, port int) (*serverBundle, error) {
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
	})

	engine, err := bootstrap.New(controlPlaneConfig(cfg))
	if err != nil {
		return nil, err
	}
	if err := engine.Open(ctx); err != nil {
		return nil, err
	}

	registry := NewSessionRegistry(cfg)

	srv, err := server.New(server.Deps{
		Hub:      h,
		Auth:     auth,
		Authz:    serverauth.NewAuthorizationService(),
		Config:   cfg,
		Registry: registry,
		APIKeys:  apiKeys,
		Metrics:  metrics,
		Clock:    clock,
		Version:  Version,
		Logger:   logger,
	})
	if err != nil {
		_ = engine.Close(ctx)
		return nil, err
	}
	return &serverBundle{srv: srv, engine: engine, cfg: cfg, logger: logger, devToken: devToken}, nil
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
func runServer(ctx context.Context, configPath, host string, port int) error {
	if ctx == nil {
		ctx = context.Background()
	}
	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	bundle, err := buildServer(sigCtx, configPath, host, port)
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

	return bundle.srv.Run(sigCtx)
}
