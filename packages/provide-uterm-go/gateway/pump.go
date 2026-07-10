//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"crypto/tls"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// helloFrame is the capability hello the gateways send upstream on every
// (re)connect to advertise their own redirect-follow support. A redirect-aware
// server hands off via a redirect control frame instead of proxying; servers
// that don't understand it ignore it. Mirrors _GATEWAY_HELLO_FRAME.
func helloFrame() map[string]any {
	return map[string]any{"type": "hello", "v": 1, "features": []any{"supports_redirect"}}
}

// tokenRec is an in-memory resume token (+ optional player id).
type tokenRec struct {
	token    string
	playerID int64
	hasPID   bool
}

// controlState is the mutable per-connection control-channel state shared
// between the reconnect loop and the ws→client pump.
type controlState struct {
	token    *tokenRec
	redirect string
}

// handleControlFrame intercepts a gateway control frame. Returns true when the
// frame was a recognised gateway control message. Mirrors
// _handle_ws_control_frame.
func handleControlFrame(frame map[string]any, st *controlState, writeClient func([]byte) error) bool {
	msgType, _ := frame["type"].(string)
	switch msgType {
	case "session_token":
		if _, ok := frame["token"]; !ok {
			return false
		}
		rec := &tokenRec{token: anyToString(frame["token"])}
		if pid, ok := asInt64(frame["player_id"]); ok {
			rec.playerID = pid
			rec.hasPID = true
		}
		st.token = rec
		return true
	case "resume_ok":
		_ = writeClient([]byte("\r\n[Session resumed]\r\n"))
		return true
	case "resume_failed":
		st.token = nil
		return true
	case "redirect":
		if path, ok := frame["path"].(string); ok {
			st.redirect = path
			return true
		}
	}
	return false
}

// resumeFrame builds a resume control message from the held token.
func resumeFrame(t *tokenRec) map[string]any {
	m := map[string]any{"type": "resume", "token": t.token}
	if t.hasPID {
		m["player_id"] = t.playerID
	}
	return m
}

// normalizeCRLF normalizes bare \n → \r\n for telnet clients.
func normalizeCRLF(raw []byte) []byte {
	s := strings.ReplaceAll(string(raw), "\r\n", "\n")
	s = strings.ReplaceAll(s, "\n", "\r\n")
	return []byte(s)
}

// telnetWriteTransform applies the telnet-side output transforms: DEL→BS, CRLF
// normalization, and the ANSI color downgrade.
func telnetWriteTransform(mode colors.ColorMode) func([]byte) []byte {
	return func(raw []byte) []byte {
		raw = replaceByte(raw, 0x7f, 0x08)
		raw = normalizeCRLF(raw)
		return colors.ApplyColorModeBytes(raw, mode)
	}
}

// sshWriteTransform applies only the color downgrade (SSH keeps its own line
// discipline; no CRLF/DEL rewriting — mirrors _ws_to_ssh).
func sshWriteTransform(mode colors.ColorMode) func([]byte) []byte {
	return func(raw []byte) []byte { return colors.ApplyColorModeBytes(raw, mode) }
}

func replaceByte(b []byte, from, to byte) []byte {
	out := make([]byte, len(b))
	for i, c := range b {
		if c == from {
			out[i] = to
		} else {
			out[i] = c
		}
	}
	return out
}

// applyRedirect validates and applies a same-origin redirect path to
// currentURL, keeping the scheme+netloc and replacing the path+query. Returns
// ok=false for a relative, protocol-relative, or absolute (cross-origin) path.
// Mirrors _apply_redirect.
func applyRedirect(currentURL, path string) (string, bool) {
	if path == "" || strings.HasPrefix(path, "//") || strings.Contains(path, "://") || !strings.HasPrefix(path, "/") {
		return "", false
	}
	u, err := url.Parse(currentURL)
	if err != nil {
		return "", false
	}
	newPath, newQuery, _ := strings.Cut(path, "?")
	u.Path = newPath
	u.RawQuery = newQuery
	u.Fragment = ""
	return u.String(), true
}

// pumpConfig carries everything one WebSocket pump attempt needs. The client
// side is decoupled behind a persistent reader (clientRx / clientDone) so a
// redirect or reconnect can end one pump and start another WITHOUT closing the
// long-lived telnet/SSH client connection — mirroring the Python design where
// only the per-attempt _tcp_to_ws task is cancelled, not the socket.
type pumpConfig struct {
	tlsConfig      *tls.Config
	header         http.Header
	identityFrame  map[string]any // sent first when non-nil (SSH identity)
	clientRx       <-chan []byte
	clientDone     <-chan struct{}
	writeClient    func([]byte) error
	readTransform  func([]byte) (up, reply []byte)
	writeTransform func([]byte) []byte
	st             *controlState
}

// pumpOnce dials wsURL, performs one bidirectional pump, and returns the WS
// close code (or -1 when unavailable). Mirrors _pipe_ws / _ssh_pump.
func pumpOnce(ctx context.Context, wsURL string, cfg pumpConfig) (int, error) {
	dialOpts := &websocket.DialOptions{HTTPHeader: cfg.header}
	if cfg.tlsConfig != nil {
		dialOpts.HTTPClient = &http.Client{Transport: &http.Transport{TLSClientConfig: cfg.tlsConfig}}
	}
	conn, _, err := websocket.Dial(ctx, wsURL, dialOpts)
	if err != nil {
		return -1, err
	}
	conn.SetReadLimit(-1)
	defer conn.CloseNow() //nolint:errcheck // best-effort close on pump exit

	if cfg.identityFrame != nil {
		if err := sendFrame(ctx, conn, cfg.identityFrame); err != nil {
			return -1, err
		}
	}
	if cfg.st.token != nil {
		if err := sendFrame(ctx, conn, resumeFrame(cfg.st.token)); err != nil {
			return -1, err
		}
	}
	if err := sendFrame(ctx, conn, helloFrame()); err != nil {
		return -1, err
	}

	pctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	wg.Add(2)

	go func() { defer wg.Done(); defer cancel(); pumpClientToWS(pctx, conn, cfg) }()

	closeCode := -1
	go func() { defer wg.Done(); defer cancel(); closeCode = pumpWSToClient(pctx, conn, cfg) }()

	wg.Wait()
	return closeCode, nil
}

// pumpClientToWS forwards raw client bytes to the upstream WebSocket until the
// client closes or the context is cancelled.
func pumpClientToWS(ctx context.Context, conn *websocket.Conn, cfg pumpConfig) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-cfg.clientDone:
			return
		case data := <-cfg.clientRx:
			up, reply := cfg.readTransform(data)
			if len(reply) > 0 {
				_ = cfg.writeClient(reply)
			}
			if len(up) > 0 {
				enc := controlchannel.EncodeTerminalData(controlchannel.WSBytesToChannelStr(up))
				if err := conn.Write(ctx, websocket.MessageText, []byte(enc)); err != nil {
					return
				}
			}
		}
	}
}

// pumpWSToClient forwards WebSocket messages to the client, returning the WS
// close code (or -1) once the stream ends.
func pumpWSToClient(ctx context.Context, conn *websocket.Conn, cfg pumpConfig) int {
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	for {
		typ, msg, err := conn.Read(ctx)
		if err != nil {
			if cs := websocket.CloseStatus(err); cs != -1 {
				return int(cs)
			}
			return -1
		}
		if typ == websocket.MessageText {
			if dispatchTextMessage(string(msg), dec, cfg) {
				return -1
			}
			continue
		}
		_ = cfg.writeClient(cfg.writeTransform(msg))
	}
}

// dispatchTextMessage decodes one WS text message and routes its chunks to the
// client. Returns done=true when a redirect frame was seen (the pump should
// end so the reconnect loop can follow it).
func dispatchTextMessage(msg string, dec *controlchannel.Decoder, cfg pumpConfig) bool {
	events, err := dec.Feed(msg)
	if err != nil {
		return false
	}
	for _, ev := range events {
		switch e := ev.(type) {
		case controlchannel.ControlChunk:
			handleControlFrame(e.Control, cfg.st, cfg.writeClient)
			if cfg.st.redirect != "" {
				return true
			}
		case controlchannel.DataChunk:
			raw := controlchannel.ChannelStrToBytes(e.Data)
			_ = cfg.writeClient(cfg.writeTransform(raw))
		}
	}
	return false
}

func sendFrame(ctx context.Context, conn *websocket.Conn, frame map[string]any) error {
	enc, err := controlchannel.EncodeControlFrame(frame)
	if err != nil {
		return err
	}
	return conn.Write(ctx, websocket.MessageText, []byte(enc))
}

// driveParams configures drive.
type driveParams struct {
	wsURL          string
	header         http.Header
	tlsConfig      *tls.Config
	client         io.ReadWriter
	identityFrame  map[string]any
	readTransform  func([]byte) (up, reply []byte)
	writeTransform func([]byte) []byte
	showReconnect  func()
	maxReconnects  int
	reconnectDelay time.Duration
}

// drive runs the full gateway session for one accepted client: it spins up a
// persistent reader goroutine over the client connection, then runs the
// reconnect/redirect loop, re-dialing the upstream WebSocket as needed. It
// returns when the client disconnects, the upstream closes deliberately, or
// ctx is cancelled.
func drive(ctx context.Context, p driveParams) {
	st := &controlState{}
	clientRx := make(chan []byte)
	clientDone := make(chan struct{})

	readerCtx, readerCancel := context.WithCancel(ctx)
	defer readerCancel()
	go func() {
		defer close(clientDone)
		buf := make([]byte, 4096)
		for {
			n, err := p.client.Read(buf)
			if n > 0 {
				select {
				case clientRx <- append([]byte(nil), buf[:n]...):
				case <-readerCtx.Done():
					return
				}
			}
			if err != nil {
				return
			}
		}
	}()

	cfg := pumpConfig{
		tlsConfig:      p.tlsConfig,
		header:         p.header,
		identityFrame:  p.identityFrame,
		clientRx:       clientRx,
		clientDone:     clientDone,
		writeClient:    func(b []byte) error { _, err := p.client.Write(b); return err },
		readTransform:  p.readTransform,
		writeTransform: p.writeTransform,
		st:             st,
	}

	clientConnected := func() bool {
		select {
		case <-clientDone:
			return false
		default:
			return true
		}
	}

	runGatewaySession(ctx, sessionParams{
		wsURL:           p.wsURL,
		pump:            func(c context.Context, u string) (int, error) { return pumpOnce(c, u, cfg) },
		clientConnected: clientConnected,
		showReconnect:   p.showReconnect,
		st:              st,
		maxReconnects:   p.maxReconnects,
		reconnectDelay:  p.reconnectDelay,
		maxRedirects:    5,
	})
}

// sessionParams configures runGatewaySession.
type sessionParams struct {
	wsURL           string
	pump            func(ctx context.Context, url string) (int, error)
	clientConnected func() bool
	showReconnect   func()
	st              *controlState
	maxReconnects   int
	reconnectDelay  time.Duration
	maxRedirects    int
}

// runGatewaySession is the shared reconnect/redirect loop for the telnet and
// SSH gateways. Mirrors _run_gateway_session.
func runGatewaySession(ctx context.Context, p sessionParams) {
	current := p.wsURL
	attempt := 0
	redirects := 0
	for attempt <= p.maxReconnects {
		if !p.clientConnected() {
			return
		}
		p.st.redirect = ""
		closeCode, _ := p.pump(ctx, current)
		if !p.clientConnected() || ctx.Err() != nil {
			return
		}
		if p.st.redirect != "" {
			next, ok := applyRedirect(current, p.st.redirect)
			if !ok {
				return
			}
			redirects++
			if redirects > p.maxRedirects {
				return
			}
			current = next
			attempt = 0
			continue
		}
		if closeCode == int(websocket.StatusNormalClosure) {
			return
		}
		attempt++
		if attempt > p.maxReconnects {
			return
		}
		if p.showReconnect != nil {
			p.showReconnect()
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(p.reconnectDelay):
		}
	}
}

// anyToString coerces a decoded JSON value to its string form.
func anyToString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case interface{ String() string }:
		return x.String()
	default:
		return ""
	}
}

// asInt64 extracts an integer from a decoded JSON number (json.Number).
func asInt64(v any) (int64, bool) {
	if n, ok := v.(interface{ Int64() (int64, error) }); ok {
		if i, err := n.Int64(); err == nil {
			return i, true
		}
	}
	return 0, false
}
