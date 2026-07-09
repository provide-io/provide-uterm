//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package auth

import (
	"context"
	"encoding/base64"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

const (
	testKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGJqWnLIvyDCzhpV1t2AggNKS3gDthWDiSpVLGSAzvHf"
	// Golden from Python fingerprint_from_openssh_blob(testKey).
	testFP = "SHA256:XqZpweMybaBjjjuoJQw5diErbEVG6s7OKwsUQseFXWg"
)

func TestFingerprintFromOpenSSHBlobTextForm(t *testing.T) {
	got, err := FingerprintFromOpenSSHBlob([]byte(testKey))
	if err != nil || got != testFP {
		t.Fatalf("got %q err %v", got, err)
	}
	// With comment and surrounding whitespace.
	got, err = FingerprintFromOpenSSHBlob([]byte("  " + testKey + " alice@laptop\n"))
	if err != nil || got != testFP {
		t.Fatalf("got %q err %v", got, err)
	}
}

func TestFingerprintFromOpenSSHBlobBinaryForm(t *testing.T) {
	payload := testKey[len("ssh-ed25519 "):]
	binary, err := base64.StdEncoding.DecodeString(payload)
	if err != nil {
		t.Fatal(err)
	}
	got, err := FingerprintFromOpenSSHBlob(binary)
	if err != nil || got != testFP {
		t.Fatalf("got %q err %v", got, err)
	}
}

func TestFingerprintErrors(t *testing.T) {
	if _, err := FingerprintFromOpenSSHBlob([]byte("ssh-ed25519")); err == nil {
		t.Fatal("expected malformed-line error")
	}
	if _, err := FingerprintFromOpenSSHBlob([]byte("ssh-ed25519 !!!notb64")); err == nil {
		t.Fatal("expected base64 error")
	}
	// Excess padding is rejected, matching Python validate=True.
	if _, err := FingerprintFromOpenSSHBlob([]byte("ssh-rsa AAAB=")); err == nil {
		t.Fatal("expected padding error")
	}
}

func TestNullResolver(t *testing.T) {
	ident, err := NullResolver{}.Resolve(context.Background(), testFP, nil, "alice")
	if err != nil || ident != nil {
		t.Fatalf("ident=%v err=%v", ident, err)
	}
}

func writeKeys(t *testing.T, lines ...string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "authorized_keys")
	content := ""
	for _, l := range lines {
		content += l + "\n"
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func resolveOne(t *testing.T, path, fp string) *ResolvedIdentity {
	t.Helper()
	ident, err := NewAuthorizedKeysFileResolver(path).Resolve(context.Background(), fp, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	return ident
}

func TestResolveSubjectFromComment(t *testing.T) {
	// Golden parity with Python: ('alice@laptop', {}).
	path := writeKeys(t, testKey+" alice@laptop")
	ident := resolveOne(t, path, testFP)
	if ident == nil || ident.Subject != "alice@laptop" || len(ident.Claims) != 0 || ident.Fingerprint != testFP {
		t.Fatalf("ident = %+v", ident)
	}
}

func TestResolveWithOptionsAndClaims(t *testing.T) {
	// Golden parity with Python:
	// ('sre:alice', {'role': 'oncall', '_options': {'no-pty': True, 'command': 'echo hi, there'}})
	line := `subject="sre:alice",claim-role="oncall",no-pty,command="echo hi, there" ` + testKey + " comment here"
	path := writeKeys(t, line)
	ident := resolveOne(t, path, testFP)
	if ident == nil || ident.Subject != "sre:alice" {
		t.Fatalf("ident = %+v", ident)
	}
	want := map[string]any{
		"role":     "oncall",
		"_options": map[string]any{"no-pty": true, "command": "echo hi, there"},
	}
	if !reflect.DeepEqual(ident.Claims, want) {
		t.Fatalf("claims = %#v want %#v", ident.Claims, want)
	}
}

func TestResolveSubjectFallsBackToKeyFingerprint(t *testing.T) {
	// Golden parity with Python: ('key:SHA256:XqZp…', {}).
	path := writeKeys(t, testKey)
	ident := resolveOne(t, path, testFP)
	if ident == nil || ident.Subject != "key:"+testFP {
		t.Fatalf("ident = %+v", ident)
	}
}

func TestResolveSkipsCommentsBlankAndMalformedLines(t *testing.T) {
	path := writeKeys(t,
		"# a comment",
		"",
		"ssh-ed25519",            // malformed: missing payload
		"ssh-ed25519 !!!bad b64", // malformed: bad base64
		"option-only-token",      // malformed: options but no key
		testKey+" survivor@host", // valid
	)
	ident := resolveOne(t, path, testFP)
	if ident == nil || ident.Subject != "survivor@host" {
		t.Fatalf("ident = %+v", ident)
	}
}

func TestResolveUnknownFingerprintAndMissingFile(t *testing.T) {
	path := writeKeys(t, testKey)
	if ident := resolveOne(t, path, "SHA256:unknown"); ident != nil {
		t.Fatalf("ident = %+v", ident)
	}
	missing := filepath.Join(t.TempDir(), "nope")
	if ident := resolveOne(t, missing, testFP); ident != nil {
		t.Fatalf("ident = %+v", ident)
	}
}

func TestFindFirstTokenEndQuoting(t *testing.T) {
	cases := []struct {
		in   string
		want int
	}{
		{`abc def`, 3},
		{`command="echo hi",no-pty rest`, 24},
		{`noquotes`, 8},
		{`a"unclosed quote`, 16},
	}
	for _, c := range cases {
		if got := findFirstTokenEnd(c.in); got != c.want {
			t.Fatalf("findFirstTokenEnd(%q) = %d want %d", c.in, got, c.want)
		}
	}
}

func TestParseOptionsFlagsAndValues(t *testing.T) {
	got := parseOptions(`flag,key="v",empty="",spaced = "x y"`)
	want := map[string]any{"flag": true, "key": "v", "empty": "", "spaced": "x y"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestSplitOptionsQuotedCommas(t *testing.T) {
	got := splitOptions(`a="1,2",b,,c`)
	want := []string{`a="1,2"`, "b", "c"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	if got := splitOptions(""); got != nil {
		t.Fatalf("got %#v", got)
	}
}
