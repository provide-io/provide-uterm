//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
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

	// instances holds the id each open login was given, for its close to find:
	// the id carries the PAM process's start time, which the close may no
	// longer be able to read, and failing that a random tag no second
	// computation reproduces. Guarded by instancesMu.
	instancesMu sync.Mutex
	instances   map[pamLoginKey]pamLoginEntry
	loginSeq    uint64

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

// pamLoginKey is what a login's open and close have in common: the user and
// the process that ran PAM.
type pamLoginKey struct {
	username string
	pid      int
}

// NewPamIntegration builds the integration. A nil egress guard uses a default.
func NewPamIntegration(cfg serverconfig.PamConfig, registry SessionRegistry, egress *EgressGuard, logger loggerLike) *PamIntegration {
	if egress == nil {
		egress = NewEgressGuard(nil, nil)
	}
	return &PamIntegration{
		cfg:       cfg,
		registry:  registry,
		egress:    egress,
		client:    &http.Client{Timeout: 5 * time.Second},
		logger:    logger,
		bridges:   map[string]*PamTunnelBridge{},
		instances: map[pamLoginKey]pamLoginEntry{},
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
	sessionID := p.beginLogin(ev)
	switch {
	case p.cfg.Mode == "capture" && ev.CaptureSocket != "":
		p.createCaptureSession(ctx, ev, sessionID)
	case p.cfg.AutoSession:
		p.createNotifySession(ctx, ev, sessionID)
	}
	if p.relayConfigured() {
		p.forwardToRelay(ctx, map[string]any{
			"event": "open", "username": ev.Username, "tty": ev.TTY, "pid": ev.PID, "mode": ev.Mode,
		})
		p.provisionTunnel(ctx, ev, sessionID)
	}
}

// onClose stops the tunnel bridge, best-effort relays the close event, then
// deletes the session. A PAM session is owned by the login, so it is removed here
// (not when its last viewer leaves) rather than left behind stopped (DeleteSession detaches the worker bridge and
// stops the connector).
//
// Relay before delete, matching _on_close: DeleteSession stops the connector
// synchronously, so relaying afterwards would hold the logout notification
// behind a shell that is slow — or refusing — to die.
func (p *PamIntegration) onClose(ctx context.Context, ev pty.PamEvent) {
	sessionID := p.endLogin(ev)
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
func (p *PamIntegration) createNotifySession(ctx context.Context, ev pty.PamEvent, sessionID string) {
	command := p.cfg.AutoSessionCommand
	if command == "" {
		command = "/bin/bash"
	}
	p.safeCreate(ctx, map[string]any{
		"session_id":     sessionID,
		"display_name":   ev.Username + " (" + ttyOrPam(ev.TTY) + ")",
		"connector_type": "pty",
		"connector_config": map[string]any{
			"command": command, "username": ev.Username, "inject": false,
		},
		"input_mode": "hijack",
		"auto_start": true,
		// Owned by the PAM login: removed on close, not when the last viewer leaves.
		"ephemeral":  false,
		"tags":       []any{"pam", "notify", ev.Username},
		"visibility": "operator",
	})
}

// createCaptureSession attaches to the live SSH session's capture socket, after
// confining the socket path to a trusted directory. Port of
// _create_capture_session (including the path-confinement guard).
func (p *PamIntegration) createCaptureSession(ctx context.Context, ev pty.PamEvent, sessionID string) {
	if ev.CaptureSocket == "" {
		return
	}
	if !p.captureSocketConfined(ev.CaptureSocket) {
		return
	}
	p.safeCreate(ctx, map[string]any{
		"session_id":       sessionID,
		"display_name":     ev.Username + " (" + ttyOrPam(ev.TTY) + ") [live]",
		"connector_type":   "pty_capture",
		"connector_config": map[string]any{"socket_path": ev.CaptureSocket},
		"input_mode":       "open",
		"auto_start":       true,
		// Owned by the PAM login: removed on close, not when the last viewer leaves.
		"ephemeral":  false,
		"tags":       []any{"pam", "capture", ev.Username},
		"visibility": "operator",
	})
}

// filepathAbs / filepathRel are filepath.Abs / filepath.Rel, swappable in
// tests: Abs only fails when os.Getwd does, and Rel of two absolute paths on
// one Unix volume never fails, so neither can be provoked on demand.
var (
	filepathAbs = filepath.Abs
	filepathRel = filepath.Rel
)

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
	resolved, err := filepathAbs(socket)
	if err != nil {
		p.logger.Warn("pam_capture_socket_confined_resolve_failed", "socket", socket)
		return false
	}
	trusted, err := filepathAbs(base)
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

// procStartTicks returns when pid started, in clock ticks since boot (field 22
// of /proc/<pid>/stat), or false when /proc will not say. The command name
// before it is in parentheses and may itself hold spaces and parentheses;
// every field after the LAST ")" is fixed, which puts the start time at offset
// 19 there. Port of _process_start_ticks.
func procStartTicks(pid int) (int64, bool) {
	if pid <= 0 {
		return 0, false
	}
	raw, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return 0, false
	}
	text := string(raw)
	fields := strings.Fields(text[strings.LastIndex(text, ")")+1:])
	if len(fields) < 20 {
		return 0, false
	}
	ticks, err := strconv.ParseInt(fields[19], 10, 64)
	if err != nil {
		return 0, false
	}
	return ticks, true
}

// pamProcessStartTicks is procStartTicks, swappable in tests.
var pamProcessStartTicks = procStartTicks

// pamInstanceTag returns what tells this login's process apart from every
// other that had its pid: its start time, or a random r<hex> where that
// cannot be read. Port of _instance_tag.
func pamInstanceTag(pid int) string {
	if ticks, ok := pamProcessStartTicks(pid); ok {
		return strconv.FormatInt(ticks, 10)
	}
	var b [4]byte
	_, _ = rand.Read(b[:])
	return "r" + hex.EncodeToString(b[:])
}

// pamSessionID builds a new session id for the login a PAM event names. Port
// of _session_id: capture sessions key on the pid (pam_uterm.so publishes one
// capture socket per pid, and the close event carries a TTY that the open-side
// id must not depend on); otherwise the TTY slug, plus the pid when the TTY is
// absent. Pids are reused, so every id carrying one also carries the
// process's instance tag: pam-<user>-capture-<pid>-<start>. A tty-named
// notify id is unchanged: it is the shape the relay's /api/pam-events derives
// for the same login. Computed once per login, at open (see beginLogin).
func pamSessionID(ev pty.PamEvent) string {
	if ev.Mode == "capture" || ev.CaptureSocket != "" {
		return "pam-" + ev.Username + "-capture-" + strconv.Itoa(ev.PID) + "-" + pamInstanceTag(ev.PID)
	}
	slug := ttySlug(ev.TTY)
	if ev.TTY == "" {
		return "pam-" + ev.Username + "-" + slug + "-" + strconv.Itoa(ev.PID) + "-" + pamInstanceTag(ev.PID)
	}
	return "pam-" + ev.Username + "-" + slug
}

// pamLoginEntry is a remembered login: the id its open gave it, and when it
// was remembered (for the cap to drop the oldest first).
type pamLoginEntry struct {
	id  string
	seq uint64
	// goneSince is when the entry was first seen gone (zero: not seen gone).
	goneSince time.Time
}

// pamGoneGrace is how long an entry must have looked gone before it is
// dropped. pam_uterm.so sends the close and the PAM process exits straight
// after, so a login is "gone" by its start time before its own close has been
// handled; dropping it then would leave the close to recompute an id it can no
// longer reproduce (a random tag), and the capture session would leak. Port of
// _GONE_GRACE_S.
const pamGoneGrace = 60 * time.Second

// pamNow is time.Now, swappable in tests.
var pamNow = time.Now

// maxRememberedLogins bounds the open-login map. Each open login holds one
// entry, so this is far above any real concurrency; it only bounds the map
// when closes go missing on a host where start times cannot be read (see
// pruneLoginsLocked). Port of _MAX_REMEMBERED_LOGINS.
var maxRememberedLogins = 4096

// loginIsGone reports whether the process an entry's id names has provably
// ended: only an id carrying a start time can say, and the login is gone when
// its pid now reports another start time, or none. Port of _login_is_gone.
func loginIsGone(key pamLoginKey, id string) bool {
	suffix := "-" + strconv.Itoa(key.pid) + "-"
	i := strings.LastIndex(id, suffix)
	if i < 0 {
		return false
	}
	tag := id[i+len(suffix):]
	if tag == "" || strings.Trim(tag, "0123456789") != "" {
		return false // a random tag proves nothing
	}
	ticks, ok := pamProcessStartTicks(key.pid)
	return !ok || strconv.FormatInt(ticks, 10) != tag
}

// pruneLoginsLocked forgets logins whose close never came: an entry whose
// process has provably ended once it has looked gone for pamGoneGrace (which
// leaves its own close, already on its way, time to find it), then, past
// maxRememberedLogins, the oldest. keep is never dropped. Caller holds
// instancesMu. Port of _prune_logins.
func (p *PamIntegration) pruneLoginsLocked(keep *pamLoginKey) {
	now := pamNow()
	for key, entry := range p.instances {
		if (keep != nil && key == *keep) || !loginIsGone(key, entry.id) {
			if !entry.goneSince.IsZero() {
				entry.goneSince = time.Time{}
				p.instances[key] = entry
			}
			continue
		}
		if entry.goneSince.IsZero() {
			entry.goneSince = now
			p.instances[key] = entry
			continue
		}
		if now.Sub(entry.goneSince) >= pamGoneGrace {
			delete(p.instances, key)
		}
	}
	for len(p.instances) > maxRememberedLogins {
		var oldest pamLoginKey
		found := false
		for key, entry := range p.instances {
			if keep != nil && key == *keep {
				continue
			}
			if !found || entry.seq < p.instances[oldest].seq {
				oldest, found = key, true
			}
		}
		if !found {
			return
		}
		delete(p.instances, oldest)
	}
}

// beginLogin gives an opening login a new id and remembers it for the close.
// Always new: an id left by an earlier login on the same pid whose close never
// came belongs to that login, not this one. The caller passes the returned id
// down rather than looking it up again, so a close racing a slow open cannot
// make it mint another.
func (p *PamIntegration) beginLogin(ev pty.PamEvent) string {
	id := pamSessionID(ev)
	key := pamLoginKey{ev.Username, ev.PID}
	p.instancesMu.Lock()
	p.loginSeq++
	p.instances[key] = pamLoginEntry{id: id, seq: p.loginSeq}
	p.pruneLoginsLocked(&key)
	p.instancesMu.Unlock()
	return id
}

// endLogin forgets a closing login and returns the id its open gave it.
// Without one (the open came before a restart) it is computed afresh, which
// finds the session while the process that ran PAM can still report its
// start time.
func (p *PamIntegration) endLogin(ev pty.PamEvent) string {
	key := pamLoginKey{ev.Username, ev.PID}
	p.instancesMu.Lock()
	entry, ok := p.instances[key]
	delete(p.instances, key)
	p.pruneLoginsLocked(nil)
	p.instancesMu.Unlock()
	if ok {
		return entry.id
	}
	return pamSessionID(ev)
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
