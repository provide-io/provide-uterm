//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"fmt"
	"net"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

func TestSSHRemnantChunking(t *testing.T) {
	tr := NewSSHTransport()
	// stashAndTakeLocked: message larger than maxBytes stashes the overflow.
	tr.mu.Lock()
	out := tr.stashAndTakeLocked([]byte("abcdef"), 2)
	tr.mu.Unlock()
	if string(out) != "ab" {
		t.Errorf("first chunk = %q, want ab", out)
	}
	// takeRemnantLocked serves the stashed remainder in maxBytes chunks.
	tr.mu.Lock()
	out2 := tr.takeRemnantLocked(2)
	tr.mu.Unlock()
	if string(out2) != "cd" {
		t.Errorf("remnant chunk = %q, want cd", out2)
	}
	tr.mu.Lock()
	out3 := tr.takeRemnantLocked(10) // remaining "ef" <= maxBytes -> all
	tr.mu.Unlock()
	if string(out3) != "ef" {
		t.Errorf("remnant tail = %q, want ef", out3)
	}
	// stashAndTakeLocked with maxBytes<=0 or msg<=maxBytes returns whole msg.
	tr.mu.Lock()
	whole := tr.stashAndTakeLocked([]byte("xy"), 0)
	tr.mu.Unlock()
	if !bytes.Equal(whole, []byte("xy")) {
		t.Errorf("whole = %q", whole)
	}
}

func TestSSHReceiveServesRemnant(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	tr := NewSSHTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	// Pre-seed a remnant; the next Receive must serve it without a network read.
	tr.mu.Lock()
	tr.remnant = []byte("XYZ")
	tr.mu.Unlock()
	got, err := tr.Receive(ctx, 2, 100*time.Millisecond)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if string(got) != "XY" {
		t.Errorf("remnant serve = %q, want XY", got)
	}
}

// startSSHServerReject starts a server that replies false to the named session
// request type (e.g. "pty-req" or "shell") to exercise the client error paths.
func startSSHServerReject(t *testing.T, rejectType string) (string, int) {
	t.Helper()
	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	signer, _ := ssh.NewSignerFromKey(priv)
	cfg := &ssh.ServerConfig{NoClientAuth: true}
	cfg.AddHostKey(signer)
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			nConn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				_, chans, reqs, err := ssh.NewServerConn(c, cfg)
				if err != nil {
					return
				}
				go ssh.DiscardRequests(reqs)
				for newCh := range chans {
					if newCh.ChannelType() != "session" {
						_ = newCh.Reject(ssh.UnknownChannelType, "")
						continue
					}
					ch, requests, err := newCh.Accept()
					if err != nil {
						continue
					}
					go func() {
						for req := range requests {
							_ = req.Reply(req.Type != rejectType, nil)
						}
					}()
					_ = ch
				}
			}(nConn)
		}
	}()
	return "127.0.0.1", ln.Addr().(*net.TCPAddr).Port
}

func TestSSHPtyRejected(t *testing.T) {
	host, port := startSSHServerReject(t, "pty-req")
	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), host, port, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", InsecureSkipHostKeyVerify: true,
	}})
	if err == nil {
		t.Fatal("expected RequestPty error")
	}
}

func TestSSHShellRejected(t *testing.T) {
	host, port := startSSHServerReject(t, "shell")
	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), host, port, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", InsecureSkipHostKeyVerify: true,
	}})
	if err == nil {
		t.Fatal("expected Shell error")
	}
}

func TestSSHDisconnectUnblocksParkedReadLoop(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	tr := NewSSHTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	// Trigger an echo but never Receive: readLoop parks on the channel send.
	if err := tr.Send(ctx, []byte("x")); err != nil {
		t.Fatalf("send: %v", err)
	}
	time.Sleep(150 * time.Millisecond)
	// Disconnect must close quit and let the parked readLoop exit.
	if err := tr.Disconnect(ctx); err != nil {
		t.Fatalf("disconnect: %v", err)
	}
}

// startSSHServerNoSession rejects every channel so NewSession fails client-side.
func startSSHServerNoSession(t *testing.T) (string, int) {
	t.Helper()
	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	signer, _ := ssh.NewSignerFromKey(priv)
	cfg := &ssh.ServerConfig{NoClientAuth: true}
	cfg.AddHostKey(signer)
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			nConn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				_, chans, reqs, err := ssh.NewServerConn(c, cfg)
				if err != nil {
					return
				}
				go ssh.DiscardRequests(reqs)
				for newCh := range chans {
					_ = newCh.Reject(ssh.Prohibited, "no sessions")
				}
			}(nConn)
		}
	}()
	return "127.0.0.1", ln.Addr().(*net.TCPAddr).Port
}

// startSSHServerCloseAfterShell replies to shell then closes the channel, so the
// client readLoop sees EOF and Receive returns ErrConnectionClosed.
func startSSHServerCloseAfterShell(t *testing.T) (string, int) {
	t.Helper()
	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	signer, _ := ssh.NewSignerFromKey(priv)
	cfg := &ssh.ServerConfig{NoClientAuth: true}
	cfg.AddHostKey(signer)
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			nConn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				_, chans, reqs, err := ssh.NewServerConn(c, cfg)
				if err != nil {
					return
				}
				go ssh.DiscardRequests(reqs)
				for newCh := range chans {
					if newCh.ChannelType() != "session" {
						_ = newCh.Reject(ssh.UnknownChannelType, "")
						continue
					}
					ch, requests, err := newCh.Accept()
					if err != nil {
						continue
					}
					go func() {
						for req := range requests {
							isShell := req.Type == "shell"
							_ = req.Reply(req.Type == "pty-req" || isShell, nil)
							if isShell {
								_ = ch.Close()
							}
						}
					}()
				}
			}(nConn)
		}
	}()
	return "127.0.0.1", ln.Addr().(*net.TCPAddr).Port
}

func TestSSHNewSessionError(t *testing.T) {
	host, port := startSSHServerNoSession(t)
	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), host, port, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", InsecureSkipHostKeyVerify: true,
	}})
	if err == nil {
		t.Fatal("expected NewSession error")
	}
}

func TestSSHReceiveConnectionClosed(t *testing.T) {
	host, port := startSSHServerCloseAfterShell(t)
	tr := NewSSHTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	var lastErr error
	for i := 0; i < 20; i++ {
		if _, err := tr.Receive(ctx, 4096, 200*time.Millisecond); err != nil {
			lastErr = err
			break
		}
	}
	if lastErr != ErrConnectionClosed {
		t.Errorf("want ErrConnectionClosed, got %v", lastErr)
	}
}

func TestSSHReceiveContextCancel(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	tr := NewSSHTransport()
	base := context.Background()
	if err := tr.Connect(base, host, port, ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(base) }()
	ctx, cancel := context.WithCancel(base)
	cancel()
	if _, err := tr.Receive(ctx, 4096, time.Second); err != context.Canceled {
		t.Errorf("want context.Canceled, got %v", err)
	}
}

func TestSSHKnownHostsLoadError(t *testing.T) {
	tr := NewSSHTransport()
	missing := filepath.Join(t.TempDir(), "does-not-exist")
	err := tr.Connect(context.Background(), "127.0.0.1", 1, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", KnownHostsFiles: []string{missing},
	}})
	if err == nil {
		t.Fatal("expected known_hosts load error")
	}
}

func TestSSHHandshakeError(t *testing.T) {
	// Plain TCP server that is not an SSH server: the handshake must fail.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		_, _ = conn.Write([]byte("not-ssh\r\n"))
		_ = conn.Close()
	}()
	port := ln.Addr().(*net.TCPAddr).Port

	tr := NewSSHTransport()
	err = tr.Connect(context.Background(), "127.0.0.1", port, ConnectOptions{
		Timeout: time.Second,
		SSH:     SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true},
	})
	if err == nil {
		t.Fatal("expected handshake error against non-SSH server")
	}
}

func TestSSHKeyAuthWithPassphrase(t *testing.T) {
	_, clientPriv, _ := ed25519.GenerateKey(rand.Reader)
	clientSigner, _ := ssh.NewSignerFromKey(clientPriv)
	block, err := ssh.MarshalPrivateKeyWithPassphrase(clientPriv, "", []byte("hunter2"))
	if err != nil {
		t.Fatalf("marshal encrypted key: %v", err)
	}
	pemBytes := pem.EncodeToMemory(block)

	_, hostPriv, _ := ed25519.GenerateKey(rand.Reader)
	hostSigner, _ := ssh.NewSignerFromKey(hostPriv)
	cfg := &ssh.ServerConfig{
		PublicKeyCallback: func(_ ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
			if string(key.Marshal()) == string(clientSigner.PublicKey().Marshal()) {
				return nil, nil
			}
			return nil, fmt.Errorf("unknown key")
		},
	}
	cfg.AddHostKey(hostSigner)
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			nConn, err := ln.Accept()
			if err != nil {
				return
			}
			go handleSSHConn(nConn, cfg)
		}
	}()
	port := ln.Addr().(*net.TCPAddr).Port

	tr := NewSSHTransport()
	ctx := context.Background()
	opts := ConnectOptions{SSH: SSHOptions{
		User:                      "u",
		Key:                       SSHKeyAuth{PrivateKeyPEM: pemBytes, Passphrase: []byte("hunter2")},
		InsecureSkipHostKeyVerify: true,
	}}
	if err := tr.Connect(ctx, "127.0.0.1", port, opts); err != nil {
		t.Fatalf("passphrase key auth connect: %v", err)
	}
	_ = tr.Disconnect(ctx)
}
