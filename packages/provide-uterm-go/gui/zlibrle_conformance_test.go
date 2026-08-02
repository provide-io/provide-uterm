// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package gui

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestZlibRLEMatchesCPython checks this port against what CPython's zlib
// actually emits, on inputs chosen to reach the decisions that are invisible
// until they diverge: ties in the heap, block splits at 16383 symbols, and the
// static-versus-dynamic choice. See gen_zlibrle_golden.py for the shape of
// each case.
func TestZlibRLEMatchesCPython(t *testing.T) {
	path := corpusPath(t, "zlibrle_golden.json")
	blob, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		LCG struct {
			Mult int64 `json:"mult"`
			Add  int64 `json:"add"`
			Mod  int64 `json:"mod"`
		} `json:"lcg"`
		Cases []struct {
			Name      string `json:"name"`
			Kind      string `json:"kind"`
			Seed      int64  `json:"seed"`
			Size      int    `json:"size"`
			RawSha256 string `json:"raw_sha256"`
			Length    int    `json:"length"`
			Sha256    string `json:"sha256"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(blob, &golden); err != nil {
		t.Fatal(err)
	}
	if len(golden.Cases) == 0 {
		t.Fatal("empty corpus")
	}

	lcg := func(seed int64, size int) []byte {
		out := make([]byte, size)
		state := seed
		for i := 0; i < size; i++ {
			state = (state*golden.LCG.Mult + golden.LCG.Add) % golden.LCG.Mod
			out[i] = byte((state >> 16) & 0xFF)
		}
		return out
	}

	build := func(kind string, seed int64, size int) []byte {
		switch kind {
		case "zeros":
			return make([]byte, size)
		case "noise":
			return lcg(seed, size)
		case "runs":
			lengths := []int{1, 2, 3, 4, 5, 257, 258, 259, 260, 7, 128}
			out := make([]byte, 0, size+512)
			state := seed
			for index := 0; len(out) < size; index++ {
				state = (state*golden.LCG.Mult + golden.LCG.Add) % golden.LCG.Mod
				value := byte((state >> 16) & 0xFF)
				for n := 0; n < lengths[index%len(lengths)]; n++ {
					out = append(out, value)
				}
			}
			return out[:size]
		case "sparse":
			out := make([]byte, 0, size+2048)
			state := seed
			for len(out) < size {
				state = (state*golden.LCG.Mult + golden.LCG.Add) % golden.LCG.Mod
				value := byte((state >> 16) & 0xFF)
				if value&1 == 1 {
					for n := 0; n < int(value)*4+3; n++ {
						out = append(out, 0)
					}
				} else {
					out = append(out, value, value)
				}
			}
			return out[:size]
		}
		t.Fatalf("unknown kind %q", kind)
		return nil
	}

	for _, c := range golden.Cases {
		raw := build(c.Kind, c.Seed, c.Size)
		if c.RawSha256 != "" {
			if got := sha256Hex(raw); got != c.RawSha256 {
				t.Fatalf("%s: input differs from the reference's (%s vs %s) — the "+
					"generators disagree, so the comparison below would be meaningless",
					c.Name, got, c.RawSha256)
			}
		}
		encoded := zlibCompressRLE(raw)
		if len(encoded) != c.Length {
			t.Errorf("%s: length = %d, want %d", c.Name, len(encoded), c.Length)
			continue
		}
		if got := sha256Hex(encoded); got != c.Sha256 {
			t.Errorf("%s: sha256 = %s, want %s", c.Name, got, c.Sha256)
		}
	}
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func corpusPath(t *testing.T, name string) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		candidate := filepath.Join(dir, "packages", "provide-uterm-ts", "testdata", name)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("%s not found above cwd", name)
		}
		dir = parent
	}
}
