//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/crypto/ssh"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/auth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

// writeHostKeyFile marshals a fresh ed25519 key to an OpenSSH PEM file.
func writeHostKeyFile(t *testing.T) string {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	block, err := ssh.MarshalPrivateKey(priv, "")
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}
	path := filepath.Join(t.TempDir(), "host_key")
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatalf("write key: %v", err)
	}
	return path
}

// TestHostSignerFromFile loads a real PEM host key from disk (the configured
// ServerKey branch of hostSigner).
func TestHostSignerFromFile(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://x/ws", ServerKey: writeHostKeyFile(t)}
	if _, err := gw.hostSigner(); err != nil {
		t.Fatalf("loading a valid host key should succeed: %v", err)
	}
}

// TestHostSignerErrors covers the three ServerKey failure modes: a directory
// path, a missing file, and an unparseable file.
func TestHostSignerErrors(t *testing.T) {
	// A directory is not a valid key file.
	gwDir := &SshWsGateway{ServerKey: t.TempDir()}
	if _, err := gwDir.hostSigner(); err == nil {
		t.Error("directory host key path should error")
	}
	// A missing file.
	gwMissing := &SshWsGateway{ServerKey: filepath.Join(t.TempDir(), "nope")}
	if _, err := gwMissing.hostSigner(); err == nil {
		t.Error("missing host key should error")
	}
	// A present but unparseable file.
	bad := filepath.Join(t.TempDir(), "bad_key")
	if err := os.WriteFile(bad, []byte("not a key"), 0o600); err != nil {
		t.Fatal(err)
	}
	gwBad := &SshWsGateway{ServerKey: bad}
	if _, err := gwBad.hostSigner(); err == nil {
		t.Error("unparseable host key should error")
	}
}

// TestHostSignerUnreadableFile covers the os.ReadFile error branch: the path
// exists (Stat succeeds) but is not readable. Skipped when running as root,
// which bypasses file permissions.
func TestHostSignerUnreadableFile(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses file permission checks")
	}
	path := filepath.Join(t.TempDir(), "unreadable_key")
	if err := os.WriteFile(path, []byte("x"), 0o000); err != nil {
		t.Fatal(err)
	}
	gw := &SshWsGateway{ServerKey: path}
	if _, err := gw.hostSigner(); err == nil {
		t.Error("unreadable host key should error")
	}
}

// TestServeWithHostKeyFileEcho exercises Serve end-to-end using a real on-disk
// host key, confirming the file-based signer wires through to a live session.
func TestServeWithHostKeyFileEcho(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough, ServerKey: writeHostKeyFile(t)}
	ln := startSSHGateway(t, gw)
	if got := sshShellEcho(t, ln.Addr().String(), newClientSigner(t), "ok"); got != "ok" {
		t.Fatalf("echo = %q", got)
	}
}

// fakeConnMeta is a minimal ssh.ConnMetadata for driving publicKeyCallback
// directly without a full handshake.
type fakeConnMeta struct{ user string }

func (m fakeConnMeta) User() string        { return m.user }
func (fakeConnMeta) SessionID() []byte     { return nil }
func (fakeConnMeta) ClientVersion() []byte { return nil }
func (fakeConnMeta) ServerVersion() []byte { return nil }
func (fakeConnMeta) RemoteAddr() net.Addr  { return nil }
func (fakeConnMeta) LocalAddr() net.Addr   { return nil }

// errResolver is an SSHKeyResolver that always fails, to drive the resolver
// error branch of publicKeyCallback.
type errResolver struct{}

func (errResolver) Resolve(context.Context, string, []byte, string) (*auth.ResolvedIdentity, error) {
	return nil, errors.New("resolver boom")
}

// TestPublicKeyCallbackResolverError covers the branch where the resolver
// returns an error: auth fails and the client cannot connect.
func TestPublicKeyCallbackResolverError(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://x/ws", KeyResolver: errResolver{}, RequireResolver: true}
	if _, err := gw.publicKeyCallback(fakeConnMeta{user: "u"}, newClientSigner(t).PublicKey()); err == nil {
		t.Fatal("resolver error should propagate as an auth failure")
	}
}

// TestPublicKeyCallbackNullResolverPasses covers the branch where a resolver is
// configured but returns no identity while RequireResolver is false: the key is
// accepted with empty permissions (no identity frame forwarded).
func TestPublicKeyCallbackNullResolverPasses(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://x/ws", KeyResolver: auth.NullResolver{}, RequireResolver: false}
	perms, err := gw.publicKeyCallback(fakeConnMeta{user: "u"}, newClientSigner(t).PublicKey())
	if err != nil {
		t.Fatalf("unknown key under non-fail-closed should pass, got %v", err)
	}
	if perms == nil || len(perms.Extensions) != 0 {
		t.Fatalf("expected empty permissions, got %+v", perms)
	}
}

// TestIdentityFrameEmptySubject covers the guard that returns nil when the
// permissions carry extensions but no subject.
func TestIdentityFrameEmptySubject(t *testing.T) {
	perms := &ssh.Permissions{Extensions: map[string]string{"unrelated": "x"}}
	if identityFrameFromPermissions(perms) != nil {
		t.Error("extensions without a subject should yield a nil frame")
	}
}

// TestServeConfigError covers Serve returning early when serverConfig fails
// (an unreadable host key path).
func TestServeConfigError(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://x/ws", ServerKey: filepath.Join(t.TempDir(), "missing")}
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	defer ln.Close() //nolint:errcheck
	if err := gw.Serve(context.Background(), ln); err == nil {
		t.Fatal("Serve should fail when the host key cannot be loaded")
	}
}
