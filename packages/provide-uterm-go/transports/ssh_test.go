//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

// startSSHServer starts a loopback SSH server echoing stdin to stdout. It
// returns host, port and the generated ed25519 host public key.
func startSSHServer(t *testing.T, password string) (string, int, ssh.PublicKey) {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	signer, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatalf("signer: %v", err)
	}
	cfg := &ssh.ServerConfig{
		PasswordCallback: func(_ ssh.ConnMetadata, pass []byte) (*ssh.Permissions, error) {
			if string(pass) == password {
				return nil, nil
			}
			return nil, fmt.Errorf("password rejected")
		},
	}
	cfg.AddHostKey(signer)

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
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

	addr := ln.Addr().(*net.TCPAddr)
	return "127.0.0.1", addr.Port, signer.PublicKey()
}

func handleSSHConn(nConn net.Conn, cfg *ssh.ServerConfig) {
	_, chans, reqs, err := ssh.NewServerConn(nConn, cfg)
	if err != nil {
		return
	}
	go ssh.DiscardRequests(reqs)
	for newCh := range chans {
		if newCh.ChannelType() != "session" {
			_ = newCh.Reject(ssh.UnknownChannelType, "only sessions")
			continue
		}
		ch, requests, err := newCh.Accept()
		if err != nil {
			continue
		}
		go func() {
			for req := range requests {
				switch req.Type {
				case "shell", "pty-req", "window-change":
					_ = req.Reply(true, nil)
				default:
					_ = req.Reply(false, nil)
				}
			}
		}()
		go func() {
			_, _ = io.Copy(ch, ch)
			_ = ch.Close()
		}()
	}
}

func TestSSHConnectEchoInsecure(t *testing.T) {
	host, port, _ := startSSHServer(t, "secret")
	tr := NewSSHTransport()
	ctx := context.Background()
	opts := ConnectOptions{Cols: 100, Rows: 40, Term: "xterm", SSH: SSHOptions{
		User: "tim", Password: "secret", InsecureSkipHostKeyVerify: true,
	}}
	if err := tr.Connect(ctx, host, port, opts); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if !tr.IsConnected() {
		t.Fatal("expected connected")
	}
	if err := tr.Send(ctx, []byte("ping\n")); err != nil {
		t.Fatalf("send: %v", err)
	}
	// Read until we accumulate the echoed data.
	var acc []byte
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && len(acc) < 5 {
		out, err := tr.Receive(ctx, 4096, 200*time.Millisecond)
		if err != nil {
			t.Fatalf("receive: %v", err)
		}
		acc = append(acc, out...)
	}
	if string(acc[:5]) != "ping\n" {
		t.Errorf("echoed %q, want ping\\n", acc)
	}
	if err := tr.SetSize(ctx, 120, 50); err != nil {
		t.Errorf("setsize: %v", err)
	}
	_ = tr.Disconnect(ctx)
	if tr.IsConnected() {
		t.Error("expected disconnected")
	}
}

func TestSSHReceiveMaxBytesChunking(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	tr := NewSSHTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	if err := tr.Send(ctx, []byte("abcdef")); err != nil {
		t.Fatalf("send: %v", err)
	}
	// Drain in 2-byte chunks; the remnant buffer must serve the overflow.
	var acc []byte
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && len(acc) < 6 {
		out, err := tr.Receive(ctx, 2, 200*time.Millisecond)
		if err != nil {
			t.Fatalf("receive: %v", err)
		}
		if len(out) > 2 {
			t.Errorf("chunk larger than maxBytes: %v", out)
		}
		acc = append(acc, out...)
	}
	if string(acc[:6]) != "abcdef" {
		t.Errorf("acc = %q", acc)
	}
}

func TestSSHKnownHostsAccept(t *testing.T) {
	host, port, hostKey := startSSHServer(t, "pw")
	// Write a known_hosts file with the server's key.
	dir := t.TempDir()
	khPath := filepath.Join(dir, "known_hosts")
	addr := knownhosts.Normalize(net.JoinHostPort(host, strconv.Itoa(port)))
	line := knownhosts.Line([]string{addr}, hostKey)
	if err := os.WriteFile(khPath, []byte(line+"\n"), 0o600); err != nil {
		t.Fatalf("write known_hosts: %v", err)
	}

	tr := NewSSHTransport()
	ctx := context.Background()
	opts := ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", KnownHostsFiles: []string{khPath}}}
	if err := tr.Connect(ctx, host, port, opts); err != nil {
		t.Fatalf("known-hosts connect: %v", err)
	}
	_ = tr.Disconnect(ctx)
}

func TestSSHKnownHostsRejectUnknown(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	// known_hosts contains a DIFFERENT key -> verification fails.
	_, otherPriv, _ := ed25519.GenerateKey(rand.Reader)
	otherSigner, _ := ssh.NewSignerFromKey(otherPriv)
	dir := t.TempDir()
	khPath := filepath.Join(dir, "known_hosts")
	addr := knownhosts.Normalize(net.JoinHostPort(host, strconv.Itoa(port)))
	line := knownhosts.Line([]string{addr}, otherSigner.PublicKey())
	_ = os.WriteFile(khPath, []byte(line+"\n"), 0o600)

	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), host, port, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", KnownHostsFiles: []string{khPath},
	}})
	if err == nil {
		t.Fatal("expected host-key verification failure")
	}
}

func TestSSHRefusesInsecureDefault(t *testing.T) {
	tr := NewSSHTransport()
	// No known_hosts and InsecureSkipHostKeyVerify=false -> fail closed.
	err := tr.Connect(context.Background(), "127.0.0.1", 1, ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw"}})
	if err == nil {
		t.Fatal("expected refusal without known_hosts")
	}
}

func TestSSHNoAuthMethod(t *testing.T) {
	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), "127.0.0.1", 1, ConnectOptions{SSH: SSHOptions{User: "u", InsecureSkipHostKeyVerify: true}})
	if err == nil {
		t.Fatal("expected no-auth-method error")
	}
}

func TestSSHKeyAuth(t *testing.T) {
	// Generate a client key; server accepts any public key.
	_, clientPriv, _ := ed25519.GenerateKey(rand.Reader)
	clientSigner, _ := ssh.NewSignerFromKey(clientPriv)
	block, err := ssh.MarshalPrivateKey(clientPriv, "")
	if err != nil {
		t.Fatalf("marshal key: %v", err)
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
		Key:                       SSHKeyAuth{PrivateKeyPEM: ssh.MarshalAuthorizedKey(clientSigner.PublicKey())}, // wrong: not a private key
		InsecureSkipHostKeyVerify: true,
	}}
	// The above intentionally passes a public key as the private PEM to exercise
	// the parse-error path.
	if err := tr.Connect(ctx, "127.0.0.1", port, opts); err == nil {
		t.Fatal("expected private-key parse error")
	}

	// Now the real private key.
	opts.SSH.Key = SSHKeyAuth{PrivateKeyPEM: pemBytes}
	if err := tr.Connect(ctx, "127.0.0.1", port, opts); err != nil {
		t.Fatalf("key auth connect: %v", err)
	}
	_ = tr.Disconnect(ctx)
}

func TestSSHConnectFailure(t *testing.T) {
	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), "127.0.0.1", 1, ConnectOptions{
		Timeout: 200 * time.Millisecond,
		SSH:     SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true},
	})
	if err == nil {
		t.Fatal("expected dial failure")
	}
}

func TestSSHNotConnected(t *testing.T) {
	tr := NewSSHTransport()
	ctx := context.Background()
	if err := tr.Send(ctx, []byte("x")); !errors.Is(err, ErrNotConnected) {
		t.Errorf("send: %v", err)
	}
	if _, err := tr.Receive(ctx, 10, time.Millisecond); !errors.Is(err, ErrNotConnected) {
		t.Errorf("receive: %v", err)
	}
	if err := tr.SetSize(ctx, 80, 25); !errors.Is(err, ErrNotConnected) {
		t.Errorf("setsize: %v", err)
	}
	if err := tr.Disconnect(ctx); err != nil {
		t.Errorf("disconnect noop: %v", err)
	}
	if tr.IsConnected() {
		t.Error("should not be connected")
	}
}
