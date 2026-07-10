//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"strings"
	"sync"

	"golang.org/x/crypto/ssh"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ctrlmsg"
)

// buildIdentityFrame constructs the upstream identity control frame for a
// resolved SSH identity. transport is always "ssh"; the gateway supplies the
// authoritative fingerprint. Returns nil on an invalid (empty-subject) identity.
func buildIdentityFrame(subject, fingerprint string, claims map[string]any) map[string]any {
	opts := []ctrlmsg.IdentityOption{ctrlmsg.WithTransport("ssh")}
	if fingerprint != "" {
		opts = append(opts, ctrlmsg.WithFingerprint(fingerprint))
	}
	if claims != nil {
		opts = append(opts, ctrlmsg.WithClaims(claims))
	}
	frame, err := ctrlmsg.MakeIdentity(subject, opts...)
	if err != nil {
		return nil
	}
	return frame
}

// handleConn completes the SSH handshake and dispatches session channels.
func (g *SshWsGateway) handleConn(ctx context.Context, conn net.Conn, cfg *ssh.ServerConfig) {
	sshConn, chans, reqs, err := ssh.NewServerConn(conn, cfg)
	if err != nil {
		_ = conn.Close()
		return
	}
	defer sshConn.Close() //nolint:errcheck // best-effort close
	go ssh.DiscardRequests(reqs)

	identityFrame := identityFrameFromPermissions(sshConn.Permissions)
	for newChan := range chans {
		if newChan.ChannelType() != "session" {
			_ = newChan.Reject(ssh.UnknownChannelType, "only session channels are supported")
			continue
		}
		go g.handleSession(ctx, newChan, identityFrame)
	}
}

// handleSession accepts a session channel, collects pty/env requests, then
// drives the bidirectional WebSocket pump until the session ends.
func (g *SshWsGateway) handleSession(ctx context.Context, newChan ssh.NewChannel, identityFrame map[string]any) {
	channel, requests, err := newChan.Accept()
	if err != nil {
		return
	}
	defer channel.Close() //nolint:errcheck // best-effort close

	var (
		mu    sync.Mutex
		term  string
		env   = map[string]string{}
		ready = make(chan struct{})
		once  sync.Once
	)
	go func() {
		for req := range requests {
			switch req.Type {
			case "pty-req":
				mu.Lock()
				term = parsePtyReqTerm(req.Payload)
				mu.Unlock()
				reply(req, true)
			case "env":
				if k, v, ok := parseEnvReq(req.Payload); ok {
					mu.Lock()
					env[k] = v
					mu.Unlock()
				}
				reply(req, true)
			case "shell", "exec":
				reply(req, true)
				once.Do(func() { close(ready) })
			default:
				reply(req, false)
			}
		}
	}()

	select {
	case <-ready:
	case <-ctx.Done():
		return
	}

	mu.Lock()
	wsURL := appendColormode(g.WSURL, DeriveColormode(term, env))
	mu.Unlock()

	drive(ctx, driveParams{
		wsURL:          wsURL,
		tlsConfig:      g.TLSConfig,
		client:         channel,
		identityFrame:  identityFrame,
		readTransform:  func(data []byte) (up, replyBytes []byte) { return data, nil },
		writeTransform: sshWriteTransform(g.ColorMode),
		showReconnect: func() {
			_, _ = channel.Write([]byte("\x1b7\x1b[999;1H\x1b[2;36m* reconnecting...\x1b[0m\x1b8"))
		},
		maxReconnects:  g.MaxReconnects,
		reconnectDelay: g.ReconnectDelay,
	})
	_, _ = channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{0}))
}

// appendColormode appends ?colormode=/&colormode= to a URL when derived != "".
func appendColormode(wsURL, derived string) string {
	if derived == "" {
		return wsURL
	}
	sep := "?"
	if strings.Contains(wsURL, "?") {
		sep = "&"
	}
	return fmt.Sprintf("%s%scolormode=%s", wsURL, sep, derived)
}

func reply(req *ssh.Request, ok bool) {
	if req.WantReply {
		_ = req.Reply(ok, nil)
	}
}

// parsePtyReqTerm extracts the TERM value (the first length-prefixed string)
// from a pty-req payload (RFC 4254 §6.2).
func parsePtyReqTerm(payload []byte) string {
	s, _, ok := readSSHString(payload)
	if !ok {
		return ""
	}
	return s
}

// parseEnvReq extracts (name, value) from an env request payload.
func parseEnvReq(payload []byte) (string, string, bool) {
	name, rest, ok := readSSHString(payload)
	if !ok {
		return "", "", false
	}
	value, _, ok := readSSHString(rest)
	if !ok {
		return "", "", false
	}
	return name, value, true
}

// readSSHString reads a uint32-length-prefixed string from the front of b.
func readSSHString(b []byte) (string, []byte, bool) {
	if len(b) < 4 {
		return "", nil, false
	}
	n := binary.BigEndian.Uint32(b[:4])
	if uint32(len(b)-4) < n {
		return "", nil, false
	}
	return string(b[4 : 4+n]), b[4+n:], true
}
