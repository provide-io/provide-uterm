//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

// dialSSHClient completes an SSH handshake against addr and registers cleanup.
func dialSSHClient(t *testing.T, addr string) *ssh.Client {
	t.Helper()
	cfg := &ssh.ClientConfig{
		User:            "tester",
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(newClientSigner(t))},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Timeout:         3 * time.Second,
	}
	client, err := ssh.Dial("tcp", addr, cfg)
	if err != nil {
		t.Fatalf("ssh dial: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}

// sshEchoWithAuth dials with the given auth method, opens a pty shell, writes
// payload, and returns the echoed bytes — used to drive the non-pubkey auth
// callbacks (password / keyboard-interactive).
func sshEchoWithAuth(t *testing.T, addr string, auth ssh.AuthMethod, payload string) string {
	t.Helper()
	cfg := &ssh.ClientConfig{
		User:            "tester",
		Auth:            []ssh.AuthMethod{auth},
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
	_ = sess.RequestPty("xterm", 24, 80, ssh.TerminalModes{})
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

// TestSSHGatewayPasswordAuth covers serverConfig's PasswordCallback: a
// non-fail-closed gateway accepts a password-only client.
func TestSSHGatewayPasswordAuth(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough}
	ln := startSSHGateway(t, gw)
	if got := sshEchoWithAuth(t, ln.Addr().String(), ssh.Password("pw"), "pw-ok"); got != "pw-ok" {
		t.Fatalf("password-auth echo = %q", got)
	}
}

// TestSSHGatewayKeyboardInteractiveAuth covers serverConfig's
// KeyboardInteractiveCallback: a keyboard-interactive client is accepted.
func TestSSHGatewayKeyboardInteractiveAuth(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough}
	ln := startSSHGateway(t, gw)
	challenge := ssh.KeyboardInteractive(func(string, string, []string, []bool) ([]string, error) {
		return nil, nil
	})
	if got := sshEchoWithAuth(t, ln.Addr().String(), challenge, "ki-ok"); got != "ki-ok" {
		t.Fatalf("keyboard-interactive echo = %q", got)
	}
}

// TestSSHGatewayRejectsNonSessionChannel covers handleConn's channel-type guard:
// only "session" channels are accepted; anything else is rejected.
func TestSSHGatewayRejectsNonSessionChannel(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL}
	ln := startSSHGateway(t, gw)
	client := dialSSHClient(t, ln.Addr().String())

	if _, _, err := client.OpenChannel("direct-tcpip", nil); err == nil {
		t.Fatal("a non-session channel type must be rejected")
	}
}

// TestSSHGatewayEnvAndUnknownRequests covers handleSession's env branch and the
// default (unhandled) request branch, alongside pty-req + shell. The client
// sets an env var, sends a bogus channel request, then opens a shell and echoes.
func TestSSHGatewayEnvAndUnknownRequests(t *testing.T) {
	wsURL, rec := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough}
	ln := startSSHGateway(t, gw)
	client := dialSSHClient(t, ln.Addr().String())

	sess, err := client.NewSession()
	if err != nil {
		t.Fatalf("new session: %v", err)
	}
	defer sess.Close() //nolint:errcheck

	// env request → gateway records COLORTERM (forwarded as ?colormode=passthrough).
	if err := sess.Setenv("COLORTERM", "truecolor"); err != nil {
		t.Fatalf("setenv: %v", err)
	}
	// An unknown channel request exercises the default reply(false) branch.
	if _, err := sess.SendRequest("bogus-request@example", true, nil); err != nil {
		t.Fatalf("bogus request: %v", err)
	}
	if err := sess.RequestPty("xterm", 24, 80, ssh.TerminalModes{}); err != nil {
		t.Fatalf("pty: %v", err)
	}
	stdin, _ := sess.StdinPipe()
	stdout, _ := sess.StdoutPipe()
	if err := sess.Shell(); err != nil {
		t.Fatalf("shell: %v", err)
	}
	if _, err := stdin.Write([]byte("hey")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if got := string(readN(t, stdout, 3)); got != "hey" {
		t.Fatalf("echo = %q", got)
	}
	// The COLORTERM env var should have driven the upstream colormode.
	select {
	case q := <-rec.queries:
		if q != "colormode=passthrough" {
			t.Fatalf("upstream query = %q, want colormode=passthrough", q)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no upstream connection observed")
	}
}

// TestSSHGatewayExecRequest covers the "exec" request branch of handleSession
// (as opposed to "shell"): starting a command opens the session and pumps.
func TestSSHGatewayExecRequest(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough}
	ln := startSSHGateway(t, gw)
	client := dialSSHClient(t, ln.Addr().String())

	sess, err := client.NewSession()
	if err != nil {
		t.Fatalf("new session: %v", err)
	}
	defer sess.Close() //nolint:errcheck
	stdin, _ := sess.StdinPipe()
	stdout, _ := sess.StdoutPipe()
	if err := sess.Start("run-me"); err != nil {
		t.Fatalf("exec start: %v", err)
	}
	if _, err := stdin.Write([]byte("xy")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if got := string(readN(t, stdout, 2)); got != "xy" {
		t.Fatalf("exec echo = %q", got)
	}
}

// TestSSHGatewaySessionContextCancel covers handleSession returning when the
// context is cancelled before a shell/exec request arrives: a channel that only
// requests a pty is torn down on shutdown without ever becoming ready.
func TestSSHGatewaySessionContextCancel(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL}
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go gw.Serve(ctx, ln) //nolint:errcheck

	client := dialSSHClient(t, ln.Addr().String())
	sess, err := client.NewSession()
	if err != nil {
		t.Fatalf("new session: %v", err)
	}
	// Request a pty but never a shell/exec, so handleSession blocks on <-ready.
	if err := sess.RequestPty("xterm", 24, 80, ssh.TerminalModes{}); err != nil {
		t.Fatalf("pty: %v", err)
	}
	// Cancelling the server context makes handleSession take the ctx.Done path.
	cancel()
	// The session channel should close shortly after; Wait returns some error.
	done := make(chan struct{})
	go func() { _ = sess.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("session did not end after context cancel")
	}
	_ = ln.Close()
}
