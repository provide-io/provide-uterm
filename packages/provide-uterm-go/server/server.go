//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/deckmux"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// WebhookManager is the optional session-webhook surface. When nil, the webhook
// routes return 503 (matching the Python "webhook manager not available"
// branch). It is defined as an interface because the manager itself is not part
// of this HTTP layer.
type WebhookManager interface {
	ValidateURL(url string) error
	ValidatePattern(pattern string) error
	Register(sessionID, url string, eventTypes []string, pattern, secret string) (map[string]any, error)
	ListWebhooks(sessionID string) []map[string]any
	GetWebhook(webhookID string) (map[string]any, bool)
	Unregister(webhookID string) bool
}

// Deps is the dependency bundle the CLI assembles and hands to [New]. Hub,
// Auth, Authz, Config and Registry are required; the rest are optional (nil
// disables the corresponding routes / uses a default).
type Deps struct {
	// Hub is the composed TermHub (leases, routing, presence, I/O). Required.
	Hub *hub.TermHub
	// Auth resolves a principal from a request. Required.
	Auth serverauth.Authenticator
	// Authz makes RBAC decisions. Required.
	Authz *serverauth.AuthorizationService
	// Config is the loaded server config. Required.
	Config *serverconfig.UtermServerConfig
	// Registry is the session-management surface. Required.
	Registry SessionRegistry
	// APIKeys backs the /api/keys routes. nil → routes report keys disabled.
	APIKeys *serverauth.ApiKeyStore
	// Profiles backs the /api/profiles routes. nil → those routes are 503.
	Profiles ProfileStore
	// Webhooks backs the session-webhook routes. nil → 503.
	Webhooks WebhookManager
	// Metrics is the shared counter map. nil → a fresh one. For hub counters to
	// appear in /api/metrics, the CLI must pass this same instance as the hub's
	// OnMetric sink.
	Metrics *Metrics
	// Clock must be the SAME clock the hub was built with, so monotonic lease
	// timestamps convert to wall-clock consistently. nil → a real clock.
	Clock hub.Clock
	// Version is reported by /api/health. Empty → "0.0.0-dev".
	Version string
	// FrontendDir, when set, is served as static assets at Config.UI.AssetsPath.
	FrontendDir string
	// Logger is the base structured logger. nil → the telemetry logger.
	Logger *slog.Logger
}

// Server is the assembled HTTP/WebSocket server. Construct it with [New].
type Server struct {
	deps    Deps
	cfg     *serverconfig.UtermServerConfig
	metrics *Metrics
	clock   hub.Clock
	logger  *slog.Logger
	handler http.Handler

	ready     atomic.Bool
	startTime float64

	allowedOrigins map[string]struct{}
	originWildcard bool

	httpSrv *http.Server

	sweepWG   sync.WaitGroup
	sweepOnce sync.Once

	// DeckMux collaborative-presence service, lazily built on first browser
	// connect (see (*Server).deck) and wired to the hub browser broadcast.
	deckOnce     sync.Once
	deckPresence *deckmux.DeckMuxPresence
}

// New assembles a Server from deps. It validates required dependencies, builds
// the route mux and middleware chain, and precomputes the allowed-origin set.
// It does not bind a socket or start sweeps — use [Server.Run] (or
// [Server.Handler] for in-process tests).
func New(deps Deps) (*Server, error) {
	switch {
	case deps.Hub == nil:
		return nil, errors.New("server.New: Hub is required")
	case deps.Auth == nil:
		return nil, errors.New("server.New: Auth is required")
	case deps.Authz == nil:
		return nil, errors.New("server.New: Authz is required")
	case deps.Config == nil:
		return nil, errors.New("server.New: Config is required")
	case deps.Registry == nil:
		return nil, errors.New("server.New: Registry is required")
	}
	if deps.Metrics == nil {
		deps.Metrics = NewMetrics()
	}
	if deps.Clock == nil {
		deps.Clock = hub.NewRealClock()
	}
	if deps.Version == "" {
		deps.Version = "0.0.0-dev"
	}
	if deps.Logger == nil {
		deps.Logger = ptel.GetLogger(context.Background(), "provide.uterm.server")
	}

	s := &Server{
		deps:      deps,
		cfg:       deps.Config,
		metrics:   deps.Metrics,
		clock:     deps.Clock,
		logger:    deps.Logger,
		startTime: deps.Clock.Wall(),
	}
	s.computeAllowedOrigins()
	s.handler = s.buildHandler()
	return s, nil
}

// computeAllowedOrigins normalizes Config.Server.AllowedOrigins into a lookup
// set (lowercased, trailing slash stripped) and detects the "*" wildcard,
// mirroring WebSocketOriginMiddleware's constructor.
func (s *Server) computeAllowedOrigins() {
	s.allowedOrigins = make(map[string]struct{})
	for _, o := range s.cfg.Server.AllowedOrigins {
		n := strings.ToLower(strings.TrimRight(o, "/"))
		if n == "*" {
			s.originWildcard = true
		}
		s.allowedOrigins[n] = struct{}{}
	}
}

// buildHandler registers every route on a ServeMux and wraps it in the
// middleware chain (outermost → innermost: request-logging/metrics, security
// headers, CORS/origin).
func (s *Server) buildHandler() http.Handler {
	mux := http.NewServeMux()
	s.registerHealthRoutes(mux)
	s.registerAPIRoutes(mux)
	s.registerSessionRoutes(mux)
	s.registerApprovalRoutes(mux)
	s.registerAPIKeyRoutes(mux)
	s.registerProfileRoutes(mux)
	s.registerTunnelRoutes(mux)
	s.registerSSERoutes(mux)
	s.registerWebhookRoutes(mux)
	s.registerBridgeRESTRoutes(mux)
	s.registerWSRoutes(mux)
	s.registerPageRoutes(mux)
	return s.requestLogging(s.securityHeaders(s.corsAndOrigin(mux)))
}

// Handler returns the fully-wrapped HTTP handler for in-process testing via
// httptest.
func (s *Server) Handler() http.Handler { return s.handler }

// Metrics returns the server's counter map.
func (s *Server) Metrics() *Metrics { return s.metrics }

// MarkReady flips the readiness flag so /readyz and /api/health report 200.
// The Python server does this at the end of lifespan startup.
func (s *Server) MarkReady() { s.ready.Store(true) }

// isReady reports the readiness flag.
func (s *Server) isReady() bool { return s.ready.Load() }

// Addr is the bind address derived from config (host:port).
func (s *Server) Addr() string {
	return net.JoinHostPort(s.cfg.Server.Host, strconv.Itoa(s.cfg.Server.Port))
}

// Run binds Config.Server.{Host,Port}, starts background sweeps, marks the
// server ready, and serves until ctx is cancelled — then it gracefully drains
// HTTP, stops sweeps, and shuts the hub down. It is the CLI entry point.
func (s *Server) Run(ctx context.Context) error {
	ln, err := net.Listen("tcp", s.Addr())
	if err != nil {
		return err
	}
	return s.Serve(ctx, ln)
}

// Serve runs the server on an already-bound listener. Same lifecycle as Run.
func (s *Server) Serve(ctx context.Context, ln net.Listener) error {
	s.StartSweeps(ctx)
	s.MarkReady()
	s.httpSrv = &http.Server{
		Handler:           s.handler,
		ReadHeaderTimeout: 30 * time.Second,
	}
	errCh := make(chan error, 1)
	go func() { errCh <- s.httpSrv.Serve(ln) }()
	s.logger.Info("uterm_server_listening", "addr", ln.Addr().String())

	select {
	case <-ctx.Done():
		return s.Shutdown()
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

// Shutdown drains in-flight HTTP requests, stops sweeps, and shuts the hub
// down. It mirrors the Python lifespan shutdown sequence (ready=false first).
func (s *Server) Shutdown() error {
	s.ready.Store(false)
	var httpErr error
	if s.httpSrv != nil {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		httpErr = s.httpSrv.Shutdown(shutdownCtx)
	}
	s.sweepWG.Wait()
	s.deps.Hub.Shutdown()
	return httpErr
}
