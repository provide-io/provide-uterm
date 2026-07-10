//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Command uterm-manager runs a standalone swarm manager (the Go port of the
// provide-uterm External Management Tier), without game plugins. It mirrors the
// Python manager/cli.py entry point plus create_manager_app + AgentManager.run.
package main

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/manager"
)

// parseArgs applies the simple --host/--port/--log-level flags onto cfg,
// mirroring manager/cli.py's hand-rolled arg loop.
func parseArgs(cfg *manager.ManagerConfig, args []string) {
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--host" && i+1 < len(args):
			cfg.Host = args[i+1]
			i++
		case args[i] == "--port" && i+1 < len(args):
			if p, err := strconv.Atoi(args[i+1]); err == nil {
				cfg.Port = p
			}
			i++
		case args[i] == "--log-level" && i+1 < len(args):
			cfg.LogLevel = args[i+1]
			i++
		}
	}
}

func main() {
	cfg := manager.DefaultManagerConfig()
	parseArgs(&cfg, os.Args[1:])

	if err := run(cfg); err != nil {
		slog.Error("swarm_manager_failed", "error", err.Error())
		os.Exit(1)
	}
}

// run builds the app, starts background loops, and serves HTTP until a signal
// (or auto-shutdown) fires, mirroring AgentManager.run.
func run(cfg manager.ManagerConfig) error {
	logger := slog.Default()
	s, handler, err := manager.CreateManagerApp(cfg, manager.AppOptions{})
	if err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var wg sync.WaitGroup
	s.M.StartBackground(ctx, &wg)

	addr := net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port))
	srv := &http.Server{Addr: addr, Handler: handler} //nolint:gosec // operator-configured bind
	s.M.Shutdown = func() { _ = srv.Shutdown(context.Background()) }

	// Translate SIGINT/SIGTERM into a graceful shutdown.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		logger.Info("swarm_manager_stopping")
		_ = srv.Shutdown(context.Background())
	}()

	logger.Info("swarm_manager_starting", "host", cfg.Host, "port", cfg.Port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		cancel()
		wg.Wait()
		return err
	}
	cancel()
	wg.Wait()
	return nil
}
