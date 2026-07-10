//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/auth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

// newClientSigner generates an ephemeral ed25519 SSH client key.
func newClientSigner(t *testing.T) ssh.Signer {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	signer, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatalf("signer: %v", err)
	}
	return signer
}

// startSSHGateway starts an SshWsGateway on loopback and returns its listener.
func startSSHGateway(t *testing.T, gw *SshWsGateway) net.Listener {
	t.Helper()
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start ssh gw: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go gw.Serve(ctx, ln) //nolint:errcheck
	return ln
}

// sshShellEcho dials the gateway, opens a pty shell, writes payload, and returns
// the first bytes echoed back.
func sshShellEcho(t *testing.T, addr string, signer ssh.Signer, payload string) string {
	t.Helper()
	cfg := &ssh.ClientConfig{
		User:            "tester",
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         3 * time.Second,
	}
	client, err := ssh.Dial("tcp", addr, cfg)
	if err != nil {
		t.Fatalf("ssh dial: %v", err)
	}
	defer client.Close() //nolint:errcheck
	sess, err := client.NewSession()
	if err != nil {
		t.Fatalf("new session: %v", err)
	}
	defer sess.Close() //nolint:errcheck
	_ = sess.RequestPty("xterm-256color", 24, 80, ssh.TerminalModes{})
	stdin, _ := sess.StdinPipe()
	stdout, _ := sess.StdoutPipe()
	if err := sess.Shell(); err != nil {
		t.Fatalf("shell: %v", err)
	}
	if _, err := stdin.Write([]byte(payload)); err != nil {
		t.Fatalf("write: %v", err)
	}
	return string(readN(t, stdout, len(payload)))
}

func TestSSHGatewayEcho(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough}
	ln := startSSHGateway(t, gw)
	if got := sshShellEcho(t, ln.Addr().String(), newClientSigner(t), "ping"); got != "ping" {
		t.Fatalf("ssh echo = %q, want %q", got, "ping")
	}
}

func TestSSHGatewayAuthorizedKeysIdentity(t *testing.T) {
	wsURL, rec := startEchoWS(t)
	signer := newClientSigner(t)

	// Write an authorized_keys file mapping the client key → subject user:alice.
	akLine := "subject=\"user:alice\" " + string(ssh.MarshalAuthorizedKey(signer.PublicKey()))
	akPath := filepath.Join(t.TempDir(), "authorized_keys")
	if err := os.WriteFile(akPath, []byte(akLine), 0o600); err != nil {
		t.Fatal(err)
	}
	gw := &SshWsGateway{
		WSURL:           wsURL,
		KeyResolver:     auth.NewAuthorizedKeysFileResolver(akPath),
		RequireResolver: true,
	}
	ln := startSSHGateway(t, gw)
	if got := sshShellEcho(t, ln.Addr().String(), signer, "hi"); got != "hi" {
		t.Fatalf("echo = %q", got)
	}
	// The first control frame the upstream saw must be the injected identity.
	select {
	case frame := <-rec.gotControl:
		if frame["type"] != "identity" || frame["subject"] != "user:alice" {
			t.Fatalf("first control frame = %v, want identity user:alice", frame)
		}
		if frame["transport"] != "ssh" {
			t.Errorf("identity transport = %v", frame["transport"])
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no control frame received")
	}
}

func TestSSHGatewayRequireResolverRejectsUnknown(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	akPath := filepath.Join(t.TempDir(), "authorized_keys")
	// authorized_keys for a DIFFERENT key, so the client key is unknown.
	other := newClientSigner(t)
	_ = os.WriteFile(akPath, ssh.MarshalAuthorizedKey(other.PublicKey()), 0o600)

	gw := &SshWsGateway{WSURL: wsURL, KeyResolver: auth.NewAuthorizedKeysFileResolver(akPath), RequireResolver: true}
	ln := startSSHGateway(t, gw)

	cfg := &ssh.ClientConfig{
		User:            "tester",
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(newClientSigner(t))},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         3 * time.Second,
	}
	if _, err := ssh.Dial("tcp", ln.Addr().String(), cfg); err == nil {
		t.Fatal("unknown key must be rejected under require-authorized-keys (fail-closed)")
	}
}

func TestSSHGatewaySecurityGate(t *testing.T) {
	// Unauthenticated (no required resolver) on a non-loopback bind must fail.
	gw := &SshWsGateway{WSURL: "ws://x/ws"}
	if _, err := gw.Start("0.0.0.0", 0); err == nil {
		t.Fatal("unauthenticated non-loopback SSH bind must fail")
	}
	// A fail-closed listener (resolver + require) may bind a non-loopback addr.
	gw2 := &SshWsGateway{WSURL: "ws://x/ws", KeyResolver: auth.NullResolver{}, RequireResolver: true}
	ln, err := gw2.Start("0.0.0.0", 0)
	if err != nil {
		t.Fatalf("fail-closed non-loopback bind should succeed: %v", err)
	}
	_ = ln.Close()
	// Explicit opt-in also allows it.
	gw3 := &SshWsGateway{WSURL: "ws://x/ws", AllowUnauthenticated: true}
	ln3, err := gw3.Start("0.0.0.0", 0)
	if err != nil {
		t.Fatalf("allow-unauthenticated bind should succeed: %v", err)
	}
	_ = ln3.Close()
}

func TestSSHGatewayBadHostKey(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://x/ws", ServerKey: filepath.Join(t.TempDir(), "missing")}
	if _, err := gw.serverConfig(); err == nil {
		t.Fatal("missing host key file should error")
	}
}

// readN reads exactly n bytes (or fails after 3s).
func readN(t *testing.T, r io.Reader, n int) []byte {
	t.Helper()
	buf := make([]byte, 0, n)
	done := make(chan struct{})
	tmp := make([]byte, 256)
	go func() {
		defer close(done)
		for len(buf) < n {
			m, err := r.Read(tmp)
			buf = append(buf, tmp[:m]...)
			if err != nil {
				return
			}
		}
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatalf("timed out reading %d bytes (got %q)", n, buf)
	}
	return buf
}
