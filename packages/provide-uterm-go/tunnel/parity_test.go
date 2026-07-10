//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnel

import (
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// goldenFile pins the Python BLAKE2b-256 digests of a fixed corpus. It is
// generated from provide.uterm.tunnel.token_hash.hash_token. Regenerate with:
//
//	uv run python -c "...hash_token..." > tunnel/testdata/token_hash_golden.json
const goldenFile = "testdata/token_hash_golden.json"

type goldenDoc struct {
	Cases []struct {
		Plain      string `json:"plain"`
		Blake2bHex string `json:"blake2b_hex"`
	} `json:"cases"`
}

func loadGolden(t *testing.T) goldenDoc {
	t.Helper()
	raw, err := os.ReadFile(goldenFile)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var doc goldenDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("golden has no cases")
	}
	return doc
}

// TestHashParityGolden proves Go's HashToken matches Python byte-for-byte for a
// committed corpus of tokens hashed by the Python token_hash module. This is the
// always-on parity guarantee (no network / interpreter needed).
func TestHashParityGolden(t *testing.T) {
	for _, c := range loadGolden(t).Cases {
		if got := HashToken(c.Plain); got != c.Blake2bHex {
			t.Fatalf("HashToken(%q) = %q, Python = %q", c.Plain, got, c.Blake2bHex)
		}
	}
}

// pythonAvailable reports whether `uv run python` can import the token_hash
// module in this checkout. When it cannot (CI without uv/env), the live
// differential subtests are skipped — the golden test still enforces parity.
func pythonHashCmd(t *testing.T, plain string) (string, bool) {
	t.Helper()
	cmd := exec.Command("uv", "run", "python", "-c",
		"import sys;from provide.uterm.tunnel.token_hash import hash_token;"+
			"sys.stdout.write(hash_token(sys.argv[1]))", plain)
	// Run from the server package root where the Python env is defined.
	cmd.Dir = "../../provide-uterm-server"
	out, err := cmd.Output()
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(out)), true
}

// TestHashParityLive round-trips a Go-minted token through the live Python
// hasher and vice versa, proving cross-compatibility beyond the frozen golden.
func TestHashParityLive(t *testing.T) {
	// Go mints → Python hashes → Go VerifyToken must accept.
	goToken := GenerateToken()
	pyHash, ok := pythonHashCmd(t, goToken)
	if !ok {
		t.Skip("uv/python unavailable; golden test covers parity")
	}
	if !VerifyToken(goToken, pyHash) {
		t.Fatalf("Go token %q: Python hash %q not accepted by Go VerifyToken", goToken, pyHash)
	}
	if HashToken(goToken) != pyHash {
		t.Fatalf("HashToken(%q)=%q != Python %q", goToken, HashToken(goToken), pyHash)
	}

	// Python-shaped known token → both sides agree.
	const known = "cross-compat-token-abc123"
	pyKnown, ok := pythonHashCmd(t, known)
	if !ok {
		t.Skip("uv/python unavailable")
	}
	if HashToken(known) != pyKnown {
		t.Fatalf("known token parity mismatch: Go %q Python %q", HashToken(known), pyKnown)
	}
}
