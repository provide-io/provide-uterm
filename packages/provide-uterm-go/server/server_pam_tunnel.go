//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// Port of provide.uterm.server.pam_tunnel.PamTunnelBridge — bridges a local
// PTY/capture session connector to a Cloudflare Durable Object WebSocket tunnel.
//
// Two modes, mirroring Python exactly:
//
//	PTY connector (bidirectional)
//	    outbound: raw PTY output → tunnel.SendData (Python os.read(master_fd))
//	    inbound:  tunnel.Recv CHANNEL_DATA → PTY input (Python os.write(master_fd))
//
//	capture connector (read-only, one-way)
//	    outbound: CHANNEL_STDOUT capture frames → tunnel.SendData; tunnel input
//	    is discarded (Python reads connector._capture.read_frame()).
//
// Deviation from Python's asyncio: pumping runs on context-driven goroutines
// (not add_reader callbacks + asyncio tasks). Stop() cancels the pump context
// and closes the tunnel; the cancellable pumps are joined via a WaitGroup.
//
// Deviation from Python's duck-typing: Python reaches into the connector's
// private _master_fd / _capture attributes. The Go connectors do not expose
// those, so the mode is selected by interface satisfaction — a connector that
// exposes raw duplex byte I/O (ptyBridgeConnector) drives the PTY bridge; one
// that exposes capture-frame reads (captureBridgeConnector) drives the capture
// bridge. Wiring the concrete registry connectors to expose these surfaces is a
// registry/CLI concern, exactly as the Python bridge relies on connector
// internals the registry layer supplies.

// tunnelConn is the tunnel-client surface the bridge drives. *tunnelclient.Client
// satisfies it; tests supply an in-memory fake so no live WebSocket is needed.
type tunnelConn interface {
	Connect(ctx context.Context) error
	OpenTerminal(ctx context.Context, cols, rows int) error
	SendData(ctx context.Context, data []byte, channel byte) error
	Recv(ctx context.Context) (tunnelclient.Frame, error)
	Close() error
}

// ptyBridgeConnector is the PTY-connector surface the bridge pumps: raw duplex
// byte I/O over the PTY master. Mirrors Python's connector._master_fd, which the
// bridge os.read()s (outbound) and os.write()s (inbound).
type ptyBridgeConnector interface {
	io.Reader // raw PTY output
	io.Writer // raw PTY input
}

// captureBridgeConnector is the capture-connector surface the bridge pumps: a
// source of capture frames. Mirrors Python's connector._capture.read_frame().
type captureBridgeConnector interface {
	ReadFrame(ctx context.Context) (pty.CaptureFrame, error)
}

// defaultNewTunnel builds the real tunnel client. Overridable per-bridge for tests.
func defaultNewTunnel(wsURL, token string) tunnelConn {
	return tunnelclient.NewClient(wsURL, token)
}

// PamTunnelBridge connects a local PTY or capture connector to a CF DO tunnel.
// Port of PamTunnelBridge.
type PamTunnelBridge struct {
	wsURL     string
	token     string
	connector any
	logger    loggerLike
	newTunnel func(wsURL, token string) tunnelConn // test seam

	mu     sync.Mutex
	tunnel tunnelConn
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// NewPamTunnelBridge builds a bridge for a connector. Nothing is dialled until
// Start. Port of PamTunnelBridge.__init__.
func NewPamTunnelBridge(wsURL, token string, connector any, logger loggerLike) *PamTunnelBridge {
	return &PamTunnelBridge{
		wsURL:     wsURL,
		token:     token,
		connector: connector,
		logger:    logger,
		newTunnel: defaultNewTunnel,
	}
}

// Start dials the tunnel, opens a terminal channel, and launches the pump
// goroutines for the connector's mode. On a connect/open failure it closes the
// tunnel and returns the error (the caller stops the bridge). Port of
// PamTunnelBridge.start.
func (b *PamTunnelBridge) Start(ctx context.Context) error {
	tunnel := b.newTunnel(b.wsURL, b.token)
	if err := tunnel.Connect(ctx); err != nil {
		return err
	}
	if err := tunnel.OpenTerminal(ctx, 80, 24); err != nil {
		_ = tunnel.Close()
		return err
	}

	// Pumps run on a detached context so they outlive the request-scoped ctx;
	// Stop() cancels this context (Python cancels the asyncio tasks).
	pumpCtx, cancel := context.WithCancel(context.Background())
	b.mu.Lock()
	b.tunnel = tunnel
	b.cancel = cancel
	b.mu.Unlock()

	switch c := b.connector.(type) {
	case ptyBridgeConnector:
		b.startPTY(pumpCtx, c, tunnel)
	case captureBridgeConnector:
		b.startCapture(pumpCtx, c, tunnel)
	default:
		// No pumpable surface: nothing to bridge (mirrors the Python capture
		// branch failing to read frames — logged, not fatal).
		b.logger.Warn("pam_tunnel_unpumpable_connector")
	}
	return nil
}

// startPTY launches the bidirectional PTY pumps. Port of _start_pty_bridge.
func (b *PamTunnelBridge) startPTY(ctx context.Context, conn ptyBridgeConnector, tunnel tunnelConn) {
	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		b.tunnelToPTY(ctx, conn, tunnel)
	}()
	// The outbound raw-read loop is not joined by Stop: a blocking PTY read is
	// not context-cancellable and unblocks only when the connector is closed by
	// the session runtime. It never sends after ctx is cancelled.
	b.ptyToTunnel(ctx, conn, tunnel)
}

// ptyToTunnel pumps raw PTY output → tunnel, spawning a detached blocking reader
// so the send loop stays context-cancellable. Port of the add_reader callback.
func (b *PamTunnelBridge) ptyToTunnel(ctx context.Context, conn io.Reader, tunnel tunnelConn) {
	dataCh := make(chan []byte)
	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := conn.Read(buf)
			if n > 0 {
				chunk := make([]byte, n)
				copy(chunk, buf[:n])
				select {
				case dataCh <- chunk:
				case <-ctx.Done():
					return
				}
			}
			if err != nil {
				close(dataCh)
				return
			}
		}
	}()
	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		for {
			select {
			case <-ctx.Done():
				return
			case chunk, ok := <-dataCh:
				if !ok {
					return
				}
				if err := tunnel.SendData(ctx, chunk, tunnelclient.ChannelData); err != nil {
					b.logger.Warn("pam_tunnel_pty_send_failed", "error", err)
					return
				}
			}
		}
	}()
}

// tunnelToPTY pumps inbound tunnel CHANNEL_DATA frames → PTY input, stopping on
// EOF, a recv error (ctx cancelled), or a write failure. Port of
// _tunnel_to_pty_loop.
func (b *PamTunnelBridge) tunnelToPTY(ctx context.Context, conn io.Writer, tunnel tunnelConn) {
	for {
		frame, err := tunnel.Recv(ctx)
		if err != nil {
			return
		}
		if frame.IsEOF() {
			return
		}
		if frame.Channel == tunnelclient.ChannelData && len(frame.Payload) > 0 {
			if _, err := conn.Write(frame.Payload); err != nil {
				b.logger.Warn("pam_tunnel_to_pty_write_failed", "error", err)
				return
			}
		}
	}
}

// startCapture launches the one-way capture pump. Port of the capture task.
func (b *PamTunnelBridge) startCapture(ctx context.Context, conn captureBridgeConnector, tunnel tunnelConn) {
	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		b.captureToTunnel(ctx, conn, tunnel)
	}()
}

// captureToTunnel pumps CHANNEL_STDOUT capture frames → tunnel; tunnel input is
// discarded. Port of _capture_to_tunnel_loop.
func (b *PamTunnelBridge) captureToTunnel(ctx context.Context, conn captureBridgeConnector, tunnel tunnelConn) {
	for {
		frame, err := conn.ReadFrame(ctx)
		if err != nil {
			return
		}
		if frame.Channel == pty.ChannelStdout {
			if err := tunnel.SendData(ctx, frame.Data, tunnelclient.ChannelData); err != nil {
				b.logger.Warn("pam_tunnel_capture_send_failed", "error", err)
				return
			}
		}
	}
}

// Stop cancels the pumps, closes the tunnel, and waits for the cancellable pumps
// to drain. Idempotent. Port of PamTunnelBridge.stop.
func (b *PamTunnelBridge) Stop() {
	b.mu.Lock()
	cancel := b.cancel
	tunnel := b.tunnel
	b.cancel = nil
	b.tunnel = nil
	b.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if tunnel != nil {
		_ = tunnel.Close() // unblocks any parked Recv
	}
	b.wg.Wait()
}

// ── relay tunnel provisioning + bridge wiring ───────────────────────────────

// connectorLookup is the optional registry surface the tunnel bridge uses to
// find a session's live connector (Python _get_connector → runtime.connector).
// A registry that does not implement it never provisions a bridge.
type connectorLookup interface {
	GetConnector(ctx context.Context, sessionID string) (any, bool)
}

// createRelayTunnel POSTs /api/tunnels and parses (worker_token, ws_endpoint),
// egress-guarding the URL first. Logs and returns an error on any failure (the
// caller swallows it) — the Go analogue of Python's `except: return None`. Port
// of _create_relay_tunnel.
func (p *PamIntegration) createRelayTunnel(ctx context.Context, sessionID, displayName string) (string, string, error) {
	relayURL := *p.cfg.RelayURL
	url := strings.TrimRight(relayURL, "/") + "/api/tunnels"
	// SSRF guard: refuse to POST tunnel provisioning (which carries the relay
	// bearer token) to a metadata IP or rebound internal host.
	if err := p.egress.AssertWebhookTargetAllowed(ctx, relayURL); err != nil {
		p.logger.Warn("create_relay_tunnel_failed", "url", url, "error", err)
		return "", "", err
	}
	body, err := json.Marshal(map[string]any{
		"session_id":   sessionID,
		"display_name": displayName,
		"tunnel_type":  "terminal",
	})
	if err != nil {
		p.logger.Warn("create_relay_tunnel_failed", "url", url, "error", err)
		return "", "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		p.logger.Warn("create_relay_tunnel_failed", "url", url, "error", err)
		return "", "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+*p.cfg.RelayToken)
	resp, err := p.client.Do(req)
	if err != nil {
		p.logger.Warn("create_relay_tunnel_failed", "url", url, "error", err)
		return "", "", err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= http.StatusBadRequest {
		err := fmt.Errorf("relay tunnel provisioning returned status %d", resp.StatusCode)
		p.logger.Warn("create_relay_tunnel_failed", "url", url, "error", err)
		return "", "", err
	}
	var parsed struct {
		WorkerToken string `json:"worker_token"`
		WSEndpoint  string `json:"ws_endpoint"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		p.logger.Warn("create_relay_tunnel_failed", "url", url, "error", err)
		return "", "", err
	}
	return parsed.WorkerToken, parsed.WSEndpoint, nil
}

// provisionTunnel provisions a relay tunnel, finds the session's connector, and
// starts a bridge (stored for onClose). Best-effort — every failure is logged
// and swallowed. Port of the relay-tunnel tail of _on_open.
func (p *PamIntegration) provisionTunnel(ctx context.Context, ev pty.PamEvent) {
	sessionID := pamSessionID(ev)
	displayName := ev.Username + " (" + ttyOrPam(ev.TTY) + ")"

	workerToken, wsEndpoint, err := p.createRelayTunnel(ctx, sessionID, displayName)
	if err != nil {
		return // already logged
	}
	connector := p.getConnector(ctx, sessionID)
	if connector == nil {
		return
	}
	bridge := NewPamTunnelBridge(wsEndpoint, workerToken, connector, p.logger)
	if p.newTunnel != nil {
		bridge.newTunnel = p.newTunnel
	}
	if err := bridge.Start(ctx); err != nil {
		p.logger.Warn("pam_tunnel_start_failed", "session_id", sessionID, "error", err)
		bridge.Stop()
		return
	}
	p.bridgesMu.Lock()
	p.bridges[sessionID] = bridge
	p.bridgesMu.Unlock()
}

// getConnector returns a session's live connector via the optional
// connectorLookup surface, or nil. Port of _get_connector.
func (p *PamIntegration) getConnector(ctx context.Context, sessionID string) any {
	cl, ok := p.registry.(connectorLookup)
	if !ok {
		return nil
	}
	conn, ok := cl.GetConnector(ctx, sessionID)
	if !ok {
		return nil
	}
	return conn
}

// stopBridge stops and removes the bridge for a session, if one exists. Port of
// the bridge-pop-and-stop head of _on_close.
func (p *PamIntegration) stopBridge(sessionID string) {
	p.bridgesMu.Lock()
	bridge := p.bridges[sessionID]
	delete(p.bridges, sessionID)
	p.bridgesMu.Unlock()
	if bridge != nil {
		bridge.Stop()
	}
}
