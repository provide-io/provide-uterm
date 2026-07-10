//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/auth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

// SshWsGateway is an SSH server that proxies each inbound shell session to a
// WebSocket terminal server. Port of the Python SshWsGateway.
type SshWsGateway struct {
	WSURL     string
	ColorMode colors.ColorMode
	// ServerKey is a path to a PEM host key. Empty → an ephemeral ed25519 key.
	ServerKey string
	// KeyResolver, when set, resolves an inbound pubkey to an identity that is
	// forwarded upstream as the first (identity) control frame.
	KeyResolver auth.SSHKeyResolver
	// RequireResolver rejects any pubkey the resolver does not know (fail-closed;
	// no password/keyboard-interactive fallback).
	RequireResolver bool
	// AllowUnauthenticated permits binding a non-loopback address when the
	// listener is not fail-closed (no required key auth).
	AllowUnauthenticated bool
	TLSConfig            *tls.Config
	MaxReconnects        int
	ReconnectDelay       time.Duration
}

// extIdentity is the identity carried across the SSH handshake via
// ssh.Permissions.Extensions (values must be strings, so claims are JSON).
const (
	extSubject     = "uterm-identity-subject"
	extFingerprint = "uterm-identity-fingerprint"
	extClaims      = "uterm-identity-claims"
)

// Start binds host:port and returns the listener. An unauthenticated listener
// (no required key auth) on a non-loopback address requires
// AllowUnauthenticated=true (fail-closed gate mirroring the Python gateway).
func (g *SshWsGateway) Start(host string, port int) (net.Listener, error) {
	unauthenticated := g.KeyResolver == nil || !g.RequireResolver
	if unauthenticated && !g.AllowUnauthenticated && !isLoopbackBindHost(host) {
		return nil, errors.New(
			"refusing to start an unauthenticated SSH gateway on a non-loopback bind address; " +
				"set --allow-unauthenticated-ssh only when this listener is protected by another access-control layer")
	}
	return net.Listen("tcp", net.JoinHostPort(host, fmt.Sprintf("%d", port)))
}

// hostSigner loads the configured host key or generates an ephemeral ed25519 one.
func (g *SshWsGateway) hostSigner() (ssh.Signer, error) {
	if g.ServerKey != "" {
		info, err := os.Stat(g.ServerKey)
		if err != nil {
			return nil, fmt.Errorf("SSH host key not found: %s", g.ServerKey)
		}
		if info.IsDir() {
			return nil, fmt.Errorf("SSH host key path is not a file: %s", g.ServerKey)
		}
		pem, err := os.ReadFile(g.ServerKey)
		if err != nil {
			return nil, err
		}
		return ssh.ParsePrivateKey(pem)
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	return ssh.NewSignerFromKey(priv)
}

// serverConfig builds the ssh.ServerConfig honoring the auth/security flags.
func (g *SshWsGateway) serverConfig() (*ssh.ServerConfig, error) {
	signer, err := g.hostSigner()
	if err != nil {
		return nil, err
	}
	cfg := &ssh.ServerConfig{PublicKeyCallback: g.publicKeyCallback}
	if !g.RequireResolver {
		// Non-fail-closed: also accept password / keyboard-interactive so plain
		// clients without a key still land (mirrors the Python no-auth server).
		cfg.PasswordCallback = func(ssh.ConnMetadata, []byte) (*ssh.Permissions, error) {
			return &ssh.Permissions{}, nil
		}
		cfg.KeyboardInteractiveCallback = func(ssh.ConnMetadata, ssh.KeyboardInteractiveChallenge) (*ssh.Permissions, error) {
			return &ssh.Permissions{}, nil
		}
	}
	cfg.AddHostKey(signer)
	return cfg, nil
}

// publicKeyCallback validates a pubkey and, when a resolver is configured,
// resolves it to an identity stashed in Permissions.Extensions. The gateway is
// always authoritative for the fingerprint (a resolver cannot forge it).
func (g *SshWsGateway) publicKeyCallback(meta ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
	fp := ssh.FingerprintSHA256(key)
	if g.KeyResolver == nil {
		return &ssh.Permissions{}, nil
	}
	identity, err := g.KeyResolver.Resolve(context.Background(), fp, key.Marshal(), meta.User())
	if err != nil {
		return nil, err
	}
	if identity == nil {
		if g.RequireResolver {
			return nil, fmt.Errorf("public key rejected: unknown key %s", fp)
		}
		return &ssh.Permissions{}, nil
	}
	claimsJSON, _ := json.Marshal(identity.Claims)
	return &ssh.Permissions{Extensions: map[string]string{
		extSubject:     identity.Subject,
		extFingerprint: fp, // gateway-authoritative fingerprint
		extClaims:      string(claimsJSON),
	}}, nil
}

// Serve accepts SSH connections on ln until ctx is cancelled.
func (g *SshWsGateway) Serve(ctx context.Context, ln net.Listener) error {
	cfg, err := g.serverConfig()
	if err != nil {
		return err
	}
	go func() { <-ctx.Done(); _ = ln.Close() }()
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go g.handleConn(ctx, conn, cfg)
	}
}

// identityFrameFromPermissions rebuilds the upstream identity frame from the
// permissions stashed during pubkey auth, or nil when none was resolved.
func identityFrameFromPermissions(perms *ssh.Permissions) map[string]any {
	if perms == nil || perms.Extensions == nil {
		return nil
	}
	subject := perms.Extensions[extSubject]
	if subject == "" {
		return nil
	}
	var claims map[string]any
	if raw := perms.Extensions[extClaims]; raw != "" {
		_ = json.Unmarshal([]byte(raw), &claims)
	}
	return buildIdentityFrame(subject, perms.Extensions[extFingerprint], claims)
}
