//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// PamIntegration wires pty.PamNotifyListener into the session registry so sshd
// logins tracked by pam_uterm.so become provide-uterm sessions. Port of
// provide.uterm.server.pam_integration.
//
// Fail-closed posture, mirroring the Python guard:
//   - It is fully opt-in — Run is a no-op unless pam.notify_socket is set.
//   - The real PAM authentication is the pty package's platform-gated backend,
//     a fail-closed stub that never authenticates without a deliberately
//     installed libpam backend (see pty/pambackend.go).
//   - capture-mode socket paths are confined to a trusted directory; an
//     out-of-tree socket is refused (no session created).
//   - the relay-forward POST is egress-guarded (SSRF) before it leaves.
//
// The relay tunnel-provisioning + PamTunnelBridge path (pam_tunnel.py) is ported
// in server_pam_tunnel.go: when relay is configured, onOpen provisions a CF DO
// tunnel and, if the session's connector is reachable via the optional
// connectorLookup surface, starts a PamTunnelBridge (tracked in bridges for
// onClose teardown).
type PamIntegration struct {
	cfg      serverconfig.PamConfig
	registry SessionRegistry
	egress   *EgressGuard
	client   *http.Client
	logger   loggerLike

	// bridges tracks the live PamTunnelBridge per session id so onClose can stop
	// it. Guarded by bridgesMu because PAM events may be dispatched concurrently.
	bridgesMu sync.Mutex
	bridges   map[string]*PamTunnelBridge

	// newTunnel, when non-nil, overrides the tunnel-client factory a provisioned
	// bridge uses (test seam; production leaves it nil → the real client).
	newTunnel func(wsURL, token string) tunnelConn
}

// loggerLike is the subset of slog.Logger PamIntegration uses (kept narrow so
// tests can pass the server logger directly).
type loggerLike interface {
	Info(msg string, args ...any)
	Warn(msg string, args ...any)
}

var pamTTYSlugRe = regexp.MustCompile(`[^a-zA-Z0-9]+`)

// NewPamIntegration builds the integration. A nil egress guard uses a default.
func NewPamIntegration(cfg serverconfig.PamConfig, registry SessionRegistry, egress *EgressGuard, logger loggerLike) *PamIntegration {
	if egress == nil {
		egress = NewEgressGuard(nil, nil)
	}
	return &PamIntegration{
		cfg:      cfg,
		registry: registry,
		egress:   egress,
		client:   &http.Client{Timeout: 5 * time.Second},
		logger:   logger,
		bridges:  map[string]*PamTunnelBridge{},
	}
}

// Run starts the notify listener and dispatches events until ctx is cancelled.
// It returns nil immediately when pam.notify_socket is unset (fully opt-in),
// matching run_pam_integration.
func (p *PamIntegration) Run(ctx context.Context) error {
	if p.cfg.NotifySocket == nil || *p.cfg.NotifySocket == "" {
		return nil
	}
	var uids []int
	if p.cfg.RequirePeerUIDs != nil {
		uids = *p.cfg.RequirePeerUIDs
	}
	listener, err := pty.NewPamNotifyListener(*p.cfg.NotifySocket, uids)
	if err != nil {
		return err
	}
	if err := listener.Start(ctx, p.handle); err != nil {
		return err
	}
	p.logger.Info("pam_integration_started",
		"socket", *p.cfg.NotifySocket, "mode", p.cfg.Mode, "auto_session", p.cfg.AutoSession)
	<-ctx.Done()
	_ = listener.Stop(context.Background())
	p.logger.Info("pam_integration_stopped")
	return nil
}

// handle dispatches one parsed event. Port of the inner handle() coroutine.
func (p *PamIntegration) handle(ctx context.Context, ev pty.PamEvent) {
	p.logger.Info("pam_event",
		"event", ev.Event, "username", ev.Username, "tty", ev.TTY, "pid", ev.PID, "mode", ev.Mode)
	switch ev.Event {
	case "open":
		p.onOpen(ctx, ev)
	case "close":
		p.onClose(ctx, ev)
	}
}

// onOpen creates the companion/live session then best-effort relays the event.
func (p *PamIntegration) onOpen(ctx context.Context, ev pty.PamEvent) {
	switch {
	case p.cfg.Mode == "capture" && ev.CaptureSocket != "":
		p.createCaptureSession(ctx, ev)
	case p.cfg.AutoSession:
		p.createNotifySession(ctx, ev)
	}
	if p.relayConfigured() {
		p.forwardToRelay(ctx, map[string]any{
			"event": "open", "username": ev.Username, "tty": ev.TTY, "pid": ev.PID, "mode": ev.Mode,
		})
		p.provisionTunnel(ctx, ev)
	}
}

// onClose stops the tunnel bridge, best-effort relays the close event, then
// deletes the session. PAM sessions are ephemeral, so the definition is removed
// rather than left behind stopped (DeleteSession detaches the worker bridge and
// stops the connector).
//
// Relay before delete, matching _on_close: DeleteSession stops the connector
// synchronously, so relaying afterwards would hold the logout notification
// behind a shell that is slow — or refusing — to die.
func (p *PamIntegration) onClose(ctx context.Context, ev pty.PamEvent) {
	sessionID := pamSessionID(ev)
	p.stopBridge(sessionID)
	if p.relayConfigured() {
		p.forwardToRelay(ctx, map[string]any{
			"event": "close", "username": ev.Username, "tty": ev.TTY, "pid": ev.PID,
		})
	}
	// The success line is the teardown's only trace: the registry's delete is
	// idempotent, so a close whose id matches nothing returns nil and would
	// otherwise be indistinguishable from one that tore a session down.
	if err := p.registry.DeleteSession(ctx, sessionID); err != nil {
		p.logger.Warn("pam_session_delete_failed", "session_id", sessionID, "error", err)
		return
	}
	p.logger.Info("pam_session_deleted", "session_id", sessionID)
}

func (p *PamIntegration) relayConfigured() bool {
	return p.cfg.RelayURL != nil && *p.cfg.RelayURL != "" && p.cfg.RelayToken != nil && *p.cfg.RelayToken != ""
}

// createNotifySession auto-creates a companion shell as the authenticated user.
// Port of _create_notify_session.
func (p *PamIntegration) createNotifySession(ctx context.Context, ev pty.PamEvent) {
	command := p.cfg.AutoSessionCommand
	if command == "" {
		command = "/bin/bash"
	}
	p.safeCreate(ctx, map[string]any{
		"session_id":     pamSessionID(ev),
		"display_name":   ev.Username + " (" + ttyOrPam(ev.TTY) + ")",
		"connector_type": "pty",
		"connector_config": map[string]any{
			"command": command, "username": ev.Username, "inject": false,
		},
		"input_mode": "hijack",
		"auto_start": true,
		"ephemeral":  true,
		"tags":       []any{"pam", "notify", ev.Username},
		"visibility": "operator",
	})
}

// createCaptureSession attaches to the live SSH session's capture socket, after
// confining the socket path to a trusted directory. Port of
// _create_capture_session (including the path-confinement guard).
func (p *PamIntegration) createCaptureSession(ctx context.Context, ev pty.PamEvent) {
	if ev.CaptureSocket == "" {
		return
	}
	if !p.captureSocketConfined(ev.CaptureSocket) {
		return
	}
	p.safeCreate(ctx, map[string]any{
		"session_id":       pamSessionID(ev),
		"display_name":     ev.Username + " (" + ttyOrPam(ev.TTY) + ") [live]",
		"connector_type":   "pty_capture",
		"connector_config": map[string]any{"socket_path": ev.CaptureSocket},
		"input_mode":       "open",
		"auto_start":       true,
		"ephemeral":        true,
		"tags":             []any{"pam", "capture", ev.Username},
		"visibility":       "operator",
	})
}

// captureSocketConfined reports whether the capture socket lives under the
// trusted base dir (explicit capture_socket_dir, else the notify socket's
// parent). No configured base → no confinement (returns true), matching Python.
func (p *PamIntegration) captureSocketConfined(socket string) bool {
	base := ""
	switch {
	case p.cfg.CaptureSocketDir != nil && *p.cfg.CaptureSocketDir != "":
		base = *p.cfg.CaptureSocketDir
	case p.cfg.NotifySocket != nil && *p.cfg.NotifySocket != "":
		base = filepath.Dir(*p.cfg.NotifySocket)
	}
	if base == "" {
		return true
	}
	resolved, err := filepath.Abs(socket)
	if err != nil {
		p.logger.Warn("pam_capture_socket_confined_resolve_failed", "socket", socket)
		return false
	}
	trusted, err := filepath.Abs(base)
	if err != nil {
		p.logger.Warn("pam_capture_socket_confined_resolve_failed", "dir", base)
		return false
	}
	if resolved == trusted || strings.HasPrefix(resolved, trusted+string(filepath.Separator)) {
		return true
	}
	p.logger.Warn("pam_capture_socket_confined", "socket", socket, "dir", base)
	return false
}

// safeCreate creates the session, logging (never raising) on failure. Port of
// _safe_create.
func (p *PamIntegration) safeCreate(ctx context.Context, payload map[string]any) {
	sessionID, _ := payload["session_id"].(string)
	if _, err := p.registry.CreateSession(ctx, payload); err != nil {
		p.logger.Warn("pam_session_create_failed", "session_id", sessionID, "error", err)
		return
	}
	p.logger.Info("pam_session_created", "session_id", sessionID)
}

// forwardToRelay POSTs a PAM event to the relay /api/pam-events, egress-guarding
// the URL first. Best-effort — never raises. Port of _forward_to_relay.
func (p *PamIntegration) forwardToRelay(ctx context.Context, event map[string]any) {
	relayURL := *p.cfg.RelayURL
	url := strings.TrimRight(relayURL, "/") + "/api/pam-events"
	// SSRF guard: refuse to POST (which carries the relay bearer token) to a
	// metadata IP or rebound internal host. A block is logged and skipped.
	if err := p.egress.AssertWebhookTargetAllowed(ctx, relayURL); err != nil {
		p.logger.Warn("pam_relay_forward_blocked", "url", url, "error", err)
		return
	}
	body, err := json.Marshal(event)
	if err != nil {
		p.logger.Warn("pam_relay_forward_failed", "url", url, "error", err)
		return
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		p.logger.Warn("pam_relay_forward_failed", "url", url, "error", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+*p.cfg.RelayToken)
	resp, err := p.client.Do(req)
	if err != nil {
		p.logger.Warn("pam_relay_forward_failed", "url", url, "error", err)
		return
	}
	_ = resp.Body.Close()
}

// pamSessionID builds a stable session id for a PAM event. Port of _session_id:
// capture sessions key on the PID (pam_uterm.so publishes one capture socket per
// pid, and the close event carries a TTY that the open-side id must not depend
// on); otherwise the TTY slug, plus the PID when the TTY is absent.
func pamSessionID(ev pty.PamEvent) string {
	if ev.Mode == "capture" || ev.CaptureSocket != "" {
		return "pam-" + ev.Username + "-capture-" + strconv.Itoa(ev.PID)
	}
	slug := ttySlug(ev.TTY)
	if ev.TTY == "" {
		return "pam-" + ev.Username + "-" + slug + "-" + strconv.Itoa(ev.PID)
	}
	return "pam-" + ev.Username + "-" + slug
}

// ttySlug maps '/dev/pts/3' → 'pts-3'. Port of _tty_slug.
func ttySlug(tty string) string {
	basename := tty
	if i := strings.LastIndex(tty, "/"); i >= 0 {
		basename = tty[i+1:]
	}
	slug := strings.Trim(pamTTYSlugRe.ReplaceAllString(basename, "-"), "-")
	if slug == "" {
		return "tty"
	}
	return slug
}

func ttyOrPam(tty string) string {
	if tty == "" {
		return "pam"
	}
	return tty
}
