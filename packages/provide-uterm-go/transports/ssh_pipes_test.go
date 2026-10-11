//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

// TestSSHSessionPipesWrapsLibraryRefusals drives sessionPipes against sessions
// the library refuses to pipe, proving each refusal is wrapped with which pipe
// failed and nothing half-built is returned.
func TestSSHSessionPipesWrapsLibraryRefusals(t *testing.T) {
	in, out, err := sessionPipes(&ssh.Session{Stdin: strings.NewReader("")})
	if err == nil || !strings.HasPrefix(err.Error(), "ssh stdin pipe: ") || in != nil || out != nil {
		t.Fatalf("stdin refusal = %v, %v, %v", in, out, err)
	}
	in, out, err = sessionPipes(&ssh.Session{Stdout: &bytes.Buffer{}})
	if err == nil || !strings.HasPrefix(err.Error(), "ssh stdout pipe: ") || in != nil || out != nil {
		t.Fatalf("stdout refusal = %v, %v, %v", in, out, err)
	}
}

// TestSSHConnectPipeFailureLeavesTransportDisconnected proves a pipe failure on
// a live, authenticated session aborts Connect with that error and leaves the
// transport reporting not-connected.
func TestSSHConnectPipeFailureLeavesTransportDisconnected(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	boom := errors.New("pipe refused")
	orig := openSessionPipes
	var got *ssh.Session
	openSessionPipes = func(s *ssh.Session) (io.WriteCloser, io.Reader, error) {
		got = s
		return nil, nil, boom
	}
	t.Cleanup(func() { openSessionPipes = orig })

	tr := NewSSHTransport()
	err := tr.Connect(context.Background(), host, port, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", InsecureSkipHostKeyVerify: true,
	}})
	if !errors.Is(err, boom) {
		t.Fatalf("Connect err = %v, want %v", err, boom)
	}
	if tr.IsConnected() {
		t.Fatal("transport must not report connected after a pipe failure")
	}
	// The session was closed on the way out: a request on it now fails.
	if got == nil {
		t.Fatal("pipes were never requested")
	}
	if _, rErr := got.SendRequest("keepalive", true, nil); rErr == nil {
		t.Fatal("session must be closed after a pipe failure")
	}
}

// TestSSHSetSizeOnDeadConnection proves a window-change request that cannot be
// delivered (the SSH channel is closed) is reported, not swallowed.
func TestSSHSetSizeOnDeadConnection(t *testing.T) {
	host, port, _ := startSSHServer(t, "pw")
	tr := NewSSHTransport()
	ctx := context.Background()
	if err := tr.Connect(ctx, host, port, ConnectOptions{SSH: SSHOptions{
		User: "u", Password: "pw", InsecureSkipHostKeyVerify: true,
	}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	// Close the SSH channel underneath the transport: the library then refuses
	// any further request on it (sentClose), so the window change cannot go out.
	tr.mu.Lock()
	session := tr.session
	tr.mu.Unlock()
	_ = session.Close()

	err := tr.SetSize(ctx, 100, 30)
	if err == nil || !strings.HasPrefix(err.Error(), "ssh window change: ") {
		t.Fatalf("SetSize err = %v, want a wrapped window-change failure", err)
	}
}

// TestSSHReceiveTimeoutReturnsEmpty proves an idle connected transport returns
// an empty, non-nil chunk when the per-call timeout expires.
func TestSSHReceiveTimeoutReturnsEmpty(t *testing.T) {
	tr := &SSHTransport{rxCh: make(chan []byte), closed: make(chan struct{}), end: &TransportClose{}}
	got, err := tr.Receive(context.Background(), 4096, time.Millisecond)
	if err != nil || got == nil || len(got) != 0 {
		t.Fatalf("Receive = %#v, %v; want empty non-nil chunk", got, err)
	}
}
