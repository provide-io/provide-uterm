//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"fmt"
	"io"
	"net"
	"strconv"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

// SSHTransport is an SSH client implementing ConnectionTransport, using the
// canonical golang.org/x/crypto/ssh library. It requests a PTY, opens an
// interactive shell, and pipes bytes over stdin/stdout.
//
// Host-key policy mirrors the security posture of the Python SSH module, which
// refuses insecure defaults: Connect fails closed unless the caller supplies a
// known_hosts file or explicitly opts into InsecureSkipHostKeyVerify.
//
// Deviation from Python: the Python transports/ssh.py is a *server* (asyncssh
// SSHServer + stream adapters); there is no client there. This is a canonical
// client written against the same interface as the other transports.
type SSHTransport struct {
	mu      sync.Mutex
	client  *ssh.Client
	session *ssh.Session
	stdin   io.WriteCloser

	rxCh    chan []byte
	closed  chan struct{} // closed by readLoop when stdout ends
	quit    chan struct{} // closed by Disconnect to unblock a parked readLoop
	remnant []byte
}

// NewSSHTransport returns an unconnected SSHTransport.
func NewSSHTransport() *SSHTransport {
	return &SSHTransport{}
}

// hostKeyCallback builds the host-key verification callback per the security
// policy: explicit insecure opt-out, else known_hosts files, else fail closed.
func hostKeyCallback(o SSHOptions) (ssh.HostKeyCallback, error) {
	if o.InsecureSkipHostKeyVerify {
		return ssh.InsecureIgnoreHostKey(), nil //nolint:gosec // explicit opt-in
	}
	if len(o.KnownHostsFiles) == 0 {
		return nil, fmt.Errorf("ssh: no known_hosts files and InsecureSkipHostKeyVerify is false (refusing to trust unknown host)")
	}
	cb, err := knownhosts.New(o.KnownHostsFiles...)
	if err != nil {
		return nil, fmt.Errorf("ssh: load known_hosts: %w", err)
	}
	return cb, nil
}

// authMethods assembles the auth methods from the options (key then password).
func authMethods(o SSHOptions) ([]ssh.AuthMethod, error) {
	var methods []ssh.AuthMethod
	if len(o.Key.PrivateKeyPEM) > 0 {
		var signer ssh.Signer
		var err error
		if len(o.Key.Passphrase) > 0 {
			signer, err = ssh.ParsePrivateKeyWithPassphrase(o.Key.PrivateKeyPEM, o.Key.Passphrase)
		} else {
			signer, err = ssh.ParsePrivateKey(o.Key.PrivateKeyPEM)
		}
		if err != nil {
			return nil, fmt.Errorf("ssh: parse private key: %w", err)
		}
		methods = append(methods, ssh.PublicKeys(signer))
	}
	if o.Password != "" {
		methods = append(methods, ssh.Password(o.Password))
	}
	if len(methods) == 0 {
		return nil, fmt.Errorf("ssh: no auth method (set SSHOptions.Password or SSHOptions.Key)")
	}
	return methods, nil
}

// Connect dials SSH, authenticates, requests a PTY, and starts a shell.
func (t *SSHTransport) Connect(ctx context.Context, host string, port int, opts ConnectOptions) error {
	logger := ptel.GetLogger(ctx, "provide.uterm.transports.ssh")
	opts = opts.withDefaults()

	hkcb, err := hostKeyCallback(opts.SSH)
	if err != nil {
		return err
	}
	methods, err := authMethods(opts.SSH)
	if err != nil {
		return err
	}

	cfg := &ssh.ClientConfig{
		User:            opts.SSH.User,
		Auth:            methods,
		HostKeyCallback: hkcb,
		Timeout:         opts.Timeout,
	}

	addr := net.JoinHostPort(host, strconv.Itoa(port))
	dialer := net.Dialer{Timeout: opts.Timeout}
	netConn, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("failed to connect to %s: %w", addr, err)
	}
	sshConn, chans, reqs, err := ssh.NewClientConn(netConn, addr, cfg)
	if err != nil {
		_ = netConn.Close()
		return fmt.Errorf("ssh handshake to %s failed: %w", addr, err)
	}
	client := ssh.NewClient(sshConn, chans, reqs)

	session, err := client.NewSession()
	if err != nil {
		_ = client.Close()
		return fmt.Errorf("ssh new session: %w", err)
	}

	modes := ssh.TerminalModes{ssh.ECHO: 1, ssh.TTY_OP_ISPEED: 14400, ssh.TTY_OP_OSPEED: 14400}
	if err := session.RequestPty(opts.Term, opts.Rows, opts.Cols, modes); err != nil {
		_ = session.Close()
		_ = client.Close()
		return fmt.Errorf("ssh request pty: %w", err)
	}
	stdin, err := session.StdinPipe()
	if err != nil {
		_ = session.Close()
		_ = client.Close()
		return fmt.Errorf("ssh stdin pipe: %w", err)
	}
	stdout, err := session.StdoutPipe()
	if err != nil {
		_ = session.Close()
		_ = client.Close()
		return fmt.Errorf("ssh stdout pipe: %w", err)
	}
	if err := session.Shell(); err != nil {
		_ = session.Close()
		_ = client.Close()
		return fmt.Errorf("ssh shell: %w", err)
	}

	t.mu.Lock()
	t.client = client
	t.session = session
	t.stdin = stdin
	t.rxCh = make(chan []byte)
	t.closed = make(chan struct{})
	t.quit = make(chan struct{})
	t.remnant = nil
	rxCh, closed, quit := t.rxCh, t.closed, t.quit
	t.mu.Unlock()

	go t.readLoop(stdout, rxCh, closed, quit)

	logger.Debug("ssh_transport connected", "host", host, "port", port, "user", opts.SSH.User)
	return nil
}

// readLoop pumps stdout chunks into rxCh until EOF/error or Disconnect. It
// selects on quit so a Disconnect while the loop is parked on a channel send
// does not leak the goroutine.
func (t *SSHTransport) readLoop(stdout io.Reader, rxCh chan []byte, closed, quit chan struct{}) {
	defer close(closed)
	buf := make([]byte, 32*1024)
	for {
		n, err := stdout.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			select {
			case rxCh <- chunk:
			case <-quit:
				return
			}
		}
		if err != nil {
			return
		}
	}
}

// Disconnect closes the session and client. Idempotent.
func (t *SSHTransport) Disconnect(ctx context.Context) error {
	t.mu.Lock()
	session := t.session
	client := t.client
	quit := t.quit
	t.session = nil
	t.client = nil
	t.stdin = nil
	t.quit = nil
	t.mu.Unlock()

	if quit != nil {
		close(quit) // unblock a parked readLoop goroutine
	}
	if session == nil && client == nil {
		return nil
	}
	if session != nil {
		_ = session.Close()
	}
	if client != nil {
		_ = client.Close()
	}
	ptel.GetLogger(ctx, "provide.uterm.transports.ssh").Debug("ssh_transport disconnected")
	return nil
}

// Send writes data to the session stdin.
func (t *SSHTransport) Send(ctx context.Context, data []byte) error {
	t.mu.Lock()
	stdin := t.stdin
	t.mu.Unlock()
	if stdin == nil {
		return fmt.Errorf("%w: ssh send", ErrNotConnected)
	}
	if _, err := stdin.Write(data); err != nil {
		_ = t.Disconnect(ctx)
		return fmt.Errorf("send failed: %w", err)
	}
	return nil
}

// Receive returns up to maxBytes from stdout, an empty slice on timeout, or
// ErrConnectionClosed when the remote closes.
func (t *SSHTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	t.mu.Lock()
	rxCh := t.rxCh
	closed := t.closed
	if rxCh == nil {
		t.mu.Unlock()
		return nil, fmt.Errorf("%w: ssh receive", ErrNotConnected)
	}
	// Serve any buffered remnant from a previous oversized message first.
	if len(t.remnant) > 0 {
		out := t.takeRemnantLocked(maxBytes)
		t.mu.Unlock()
		return out, nil
	}
	t.mu.Unlock()

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case msg := <-rxCh:
		t.mu.Lock()
		out := t.stashAndTakeLocked(msg, maxBytes)
		t.mu.Unlock()
		return out, nil
	case <-timer.C:
		return []byte{}, nil
	case <-closed:
		_ = t.Disconnect(ctx)
		return nil, ErrConnectionClosed
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// stashAndTakeLocked returns up to maxBytes of msg, stashing any overflow.
func (t *SSHTransport) stashAndTakeLocked(msg []byte, maxBytes int) []byte {
	if maxBytes <= 0 || len(msg) <= maxBytes {
		return msg
	}
	out := make([]byte, maxBytes)
	copy(out, msg[:maxBytes])
	t.remnant = append(t.remnant, msg[maxBytes:]...)
	return out
}

// takeRemnantLocked returns up to maxBytes from the stashed remnant.
func (t *SSHTransport) takeRemnantLocked(maxBytes int) []byte {
	if maxBytes <= 0 || len(t.remnant) <= maxBytes {
		out := t.remnant
		t.remnant = nil
		return out
	}
	out := make([]byte, maxBytes)
	copy(out, t.remnant[:maxBytes])
	t.remnant = t.remnant[maxBytes:]
	return out
}

// IsConnected reports whether the SSH session is active.
func (t *SSHTransport) IsConnected() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.session != nil
}

// SetSize sends an SSH window-change request to resize the PTY.
func (t *SSHTransport) SetSize(_ context.Context, cols, rows int) error {
	t.mu.Lock()
	session := t.session
	t.mu.Unlock()
	if session == nil {
		return fmt.Errorf("%w: ssh set_size", ErrNotConnected)
	}
	if err := session.WindowChange(rows, cols); err != nil {
		return fmt.Errorf("ssh window change: %w", err)
	}
	return nil
}

// Compile-time assertion that SSHTransport implements ConnectionTransport.
var _ ConnectionTransport = (*SSHTransport)(nil)
