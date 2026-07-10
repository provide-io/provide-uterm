//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package conformance holds the cross-language wire-compatibility proof.
//
// The vectors in testdata/vectors.json are produced by the Python reference
// implementation (gen_vectors.py). Each test replays a Python-authored input
// through the Go port and asserts the Go output equals Python's byte-for-byte.
// This is the single authoritative "Go and Python agree" gate across every
// shared wire surface.
//
// When a Python toolchain is reachable (uv at the repo root) the suite
// regenerates the vectors live before comparing, so it proves current
// agreement rather than agreement with a frozen snapshot. Otherwise it runs
// against the committed golden. Set UTERM_CONFORMANCE_NO_REGEN=1 to force the
// golden path.
package conformance

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ansi"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ctrlmsg"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/emulator"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

type vectors struct {
	ControlFrames []struct {
		Payload map[string]any `json:"payload"`
		WireB64 string         `json:"wire_b64"`
	} `json:"control_frames"`
	TerminalData []struct {
		Raw     string `json:"raw"`
		WireB64 string `json:"wire_b64"`
	} `json:"terminal_data"`
	NormalizeTerminalText []struct {
		In  string `json:"in"`
		Out string `json:"out"`
	} `json:"normalize_terminal_text"`
	CP437Roundtrip []struct {
		BytesB64 string `json:"bytes_b64"`
		Decoded  string `json:"decoded"`
	} `json:"cp437_roundtrip"`
	NormalizeColors []struct {
		In  string `json:"in"`
		Out string `json:"out"`
	} `json:"normalize_colors"`
	Upgrade256 []struct {
		In  string `json:"in"`
		Out string `json:"out"`
	} `json:"upgrade_256"`
	WebhookHMAC []struct {
		Secret string  `json:"secret"`
		Body   string  `json:"body"`
		Sig    *string `json:"sig"`
	} `json:"webhook_hmac"`
	IdentitySignature []struct {
		Subject string         `json:"subject"`
		Claims  map[string]any `json:"claims"`
		Secret  string         `json:"secret"`
		Frame   map[string]any `json:"frame"`
	} `json:"identity_signature"`
	EmulatorSnapshot struct {
		FeedB64     string         `json:"feed_b64"`
		Cols        int            `json:"cols"`
		Rows        int            `json:"rows"`
		Screen      string         `json:"screen"`
		ScreenHash  string         `json:"screen_hash"`
		Cursor      map[string]int `json:"cursor"`
		CursorAtEnd bool           `json:"cursor_at_end"`
	} `json:"emulator_snapshot"`
}

// loadVectors regenerates the vectors from Python when uv is reachable,
// otherwise falls back to the committed golden. It reports which path ran.
func loadVectors(t *testing.T) (vectors, string) {
	t.Helper()
	goldenPath := filepath.Join("testdata", "vectors.json")

	if os.Getenv("UTERM_CONFORMANCE_NO_REGEN") == "" {
		if raw, source, ok := regenFromPython(t); ok {
			return decodeVectors(t, raw), source
		}
	}

	raw, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("read golden vectors: %v", err)
	}
	return decodeVectors(t, raw), "committed golden (testdata/vectors.json)"
}

// decodeVectors unmarshals with UseNumber so JSON integers stay json.Number
// ("1") rather than collapsing to float64 (1.0). This mirrors what a correct
// cross-language verifier must do — controlchannel decodes wire numbers the
// same way — so integer claims canonicalize identically to Python's int.
func decodeVectors(t *testing.T, raw []byte) vectors {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v vectors
	if err := dec.Decode(&v); err != nil {
		t.Fatalf("vectors invalid: %v", err)
	}
	return v
}

// regenFromPython runs gen_vectors.py at the monorepo root via uv. Returns
// ok=false (never fatal) when uv or the repo root is unavailable, so the
// suite still runs against the golden in a Python-less CI.
func regenFromPython(t *testing.T) ([]byte, string, bool) {
	t.Helper()
	if _, err := exec.LookPath("uv"); err != nil {
		return nil, "", false
	}
	// Walk up to the repo root (the dir containing pyproject.toml + packages/).
	root, err := os.Getwd()
	if err != nil {
		return nil, "", false
	}
	for i := 0; i < 8; i++ {
		if _, err := os.Stat(filepath.Join(root, "packages", "provide-uterm", "src")); err == nil {
			break
		}
		parent := filepath.Dir(root)
		if parent == root {
			return nil, "", false
		}
		root = parent
	}
	script := filepath.Join("packages", "provide-uterm-go", "conformance", "gen_vectors.py")
	if _, err := os.Stat(filepath.Join(root, script)); err != nil {
		return nil, "", false
	}
	cmd := exec.Command("uv", "run", "python", script) //nolint:gosec // fixed script path under the repo
	cmd.Dir = root
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		t.Logf("live Python regen unavailable (%v): %s — using committed golden", err, strings.TrimSpace(errb.String()))
		return nil, "", false
	}
	return out.Bytes(), "live Python regeneration (uv run gen_vectors.py)", true
}

func mustB64(t *testing.T, s string) []byte {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatalf("bad base64: %v", err)
	}
	return b
}

func TestConformanceReportsSource(t *testing.T) {
	_, source := loadVectors(t)
	t.Logf("cross-language conformance vectors: %s", source)
}

// TestControlFrameDecode: Go decodes every Python-encoded control frame back
// to the exact payload.
func TestControlFrameDecode(t *testing.T) {
	v, _ := loadVectors(t)
	if len(v.ControlFrames) == 0 {
		t.Fatal("no control-frame vectors")
	}
	for i, c := range v.ControlFrames {
		wire := string(mustB64(t, c.WireB64))
		dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
		events, err := dec.Feed(wire)
		if err != nil {
			t.Fatalf("case %d: decode: %v", i, err)
		}
		fin, err := dec.Finish()
		if err != nil {
			t.Fatalf("case %d: finish: %v", i, err)
		}
		events = append(events, fin...)
		if len(events) != 1 {
			t.Fatalf("case %d: got %d events", i, len(events))
		}
		ctrl, ok := events[0].(controlchannel.ControlChunk)
		if !ok {
			t.Fatalf("case %d: not a control chunk", i)
		}
		if !jsonEqual(ctrl.Control, c.Payload) {
			t.Fatalf("case %d: payload mismatch\n go: %#v\n py: %#v", i, ctrl.Control, c.Payload)
		}
	}
}

// TestControlFrameEncode: a frame Go encodes decodes back to the same payload
// Python encoded. Byte-identity is NOT asserted for map inputs — Go maps have
// no insertion order, so key order can differ from Python's dict order; JSON
// objects are unordered, so this is not a wire contract. (The struct-based
// frames package DOES reproduce Python's field order byte-for-byte; see its
// golden test.) The round-trip proves semantic equality of the encoding.
func TestControlFrameEncode(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.ControlFrames {
		wire, err := controlchannel.EncodeControlFrame(c.Payload)
		if err != nil {
			t.Fatalf("case %d: encode: %v", i, err)
		}
		dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
		events, err := dec.Feed(wire)
		if err != nil {
			t.Fatalf("case %d: re-decode: %v", i, err)
		}
		fin, _ := dec.Finish()
		events = append(events, fin...)
		if len(events) != 1 {
			t.Fatalf("case %d: got %d events", i, len(events))
		}
		if !jsonEqual(events[0].(controlchannel.ControlChunk).Control, c.Payload) {
			t.Fatalf("case %d: round-trip payload mismatch", i)
		}
	}
}

func TestTerminalDataEncode(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.TerminalData {
		got := controlchannel.EncodeTerminalData(c.Raw)
		if want := string(mustB64(t, c.WireB64)); got != want {
			t.Fatalf("case %d: %q vs %q", i, got, want)
		}
	}
}

func TestNormalizeTerminalText(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.NormalizeTerminalText {
		if got := screen.NormalizeTerminalText(c.In); got != c.Out {
			t.Fatalf("case %d: %q -> %q, want %q", i, c.In, got, c.Out)
		}
	}
}

func TestCP437Roundtrip(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.CP437Roundtrip {
		raw := mustB64(t, c.BytesB64)
		if got := screen.DecodeCP437(raw); got != c.Decoded {
			t.Fatalf("case %d decode mismatch", i)
		}
		if got := screen.EncodeCP437(c.Decoded); !bytes.Equal(got, raw) {
			t.Fatalf("case %d re-encode mismatch", i)
		}
	}
}

func TestNormalizeColors(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.NormalizeColors {
		if got := ansi.NormalizeColors(c.In); got != c.Out {
			t.Fatalf("case %d: %q -> %q, want %q", i, c.In, got, c.Out)
		}
	}
}

func TestUpgrade256(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.Upgrade256 {
		if got := ansi.UpgradeTo256(c.In, nil); got != c.Out {
			t.Fatalf("case %d: %q -> %q, want %q", i, c.In, got, c.Out)
		}
	}
}

// webhookSig reproduces serverauth's sha256=<hex> signature (kept local so
// this package does not pull the whole server graph).
func webhookSig(secret, body string) *string {
	if secret == "" {
		return nil
	}
	mac := hmacSHA256([]byte(secret), []byte(body))
	s := "sha256=" + hex.EncodeToString(mac)
	return &s
}

func TestWebhookHMAC(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.WebhookHMAC {
		got := webhookSig(c.Secret, c.Body)
		switch {
		case got == nil && c.Sig == nil:
			// both fail closed on empty secret — correct
		case got == nil || c.Sig == nil:
			t.Fatalf("case %d: fail-closed mismatch go=%v py=%v", i, got, c.Sig)
		case *got != *c.Sig:
			t.Fatalf("case %d: sig mismatch\n go: %s\n py: %s", i, *got, *c.Sig)
		}
	}
}

func TestIdentitySignature(t *testing.T) {
	v, _ := loadVectors(t)
	for i, c := range v.IdentitySignature {
		opts := []ctrlmsg.IdentityOption{ctrlmsg.WithSecret([]byte(c.Secret))}
		if c.Claims != nil {
			opts = append(opts, ctrlmsg.WithClaims(c.Claims))
		}
		frame, err := ctrlmsg.MakeIdentity(c.Subject, opts...)
		if err != nil {
			t.Fatalf("case %d: MakeIdentity: %v", i, err)
		}
		if !jsonEqual(frame, c.Frame) {
			t.Fatalf("case %d: identity frame mismatch\n go: %#v\n py: %#v", i, frame, c.Frame)
		}
	}
}

func TestEmulatorSnapshot(t *testing.T) {
	v, _ := loadVectors(t)
	s := v.EmulatorSnapshot
	emu := emulator.New(s.Cols, s.Rows, "")
	emu.Process(mustB64(t, s.FeedB64))
	snap := emu.GetSnapshot()
	if snap.Screen != s.Screen {
		t.Fatalf("screen mismatch\n go:\n%q\n py:\n%q", snap.Screen, s.Screen)
	}
	if snap.ScreenHash != s.ScreenHash {
		t.Fatalf("hash mismatch go=%s py=%s", snap.ScreenHash, s.ScreenHash)
	}
	if snap.Cursor.X != s.Cursor["x"] || snap.Cursor.Y != s.Cursor["y"] {
		t.Fatalf("cursor mismatch go=%+v py=%v", snap.Cursor, s.Cursor)
	}
	if snap.CursorAtEnd != s.CursorAtEnd {
		t.Fatalf("cursor_at_end mismatch go=%v py=%v", snap.CursorAtEnd, s.CursorAtEnd)
	}
	// Independently confirm the hash is a real SHA-256 of the screen text.
	want := sha256.Sum256([]byte(snap.Screen))
	if hex.EncodeToString(want[:]) != snap.ScreenHash {
		t.Fatal("screen hash is not SHA-256 of screen")
	}
}

// jsonEqual compares two decoded-JSON values for deep equality after a
// canonical round-trip (so int vs float64 numeric drift can't create a false
// mismatch — both sides go through the same json decoder).
func jsonEqual(a, b any) bool {
	ab, _ := json.Marshal(a)
	bb, _ := json.Marshal(b)
	var av, bv any
	_ = json.Unmarshal(ab, &av)
	_ = json.Unmarshal(bb, &bv)
	return reflect.DeepEqual(av, bv)
}
