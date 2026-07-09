//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"crypto/sha256"
	"encoding/hex"
	"math/big"
	"strings"
)

// adjectives, animals and colors mirror the Python _names.py tables verbatim;
// order is load-bearing (the deterministic hash indexes into them).
var adjectives = []string{
	"red", "blue", "green", "amber", "silver", "coral", "jade", "onyx",
	"pearl", "ruby", "gold", "iron", "copper", "bronze", "crystal", "storm",
	"frost", "ember", "dusk", "dawn", "ash", "moss", "slate", "flint",
	"cedar", "birch", "maple", "sage", "thorn", "drift", "spark", "blaze",
}

var animals = []string{
	"fox", "hawk", "wolf", "otter", "lynx", "crane", "bear", "deer",
	"eagle", "raven", "heron", "viper", "shark", "whale", "tiger", "panther",
	"falcon", "condor", "bison", "moose", "cobra", "gecko", "puma", "osprey",
	"badger", "ferret", "marten", "jackal", "ibis", "newt", "pike", "wren",
	"tanuki",
}

var colors = []string{
	"#e74c3c", "#3498db", "#2ecc71", "#9b59b6", "#e67e22", "#1abc9c",
	"#f39c12", "#e91e63", "#00bcd4", "#8bc34a", "#ff5722", "#607d8b",
}

// hashInt is the Go equivalent of Python's
// int(hashlib.sha256(value.encode()).hexdigest(), 16): the full 256-bit
// SHA-256 digest read as a big-endian unsigned integer.
func hashInt(value string) *big.Int {
	sum := sha256.Sum256([]byte(value))
	n := new(big.Int)
	// SetString(hex, 16) matches int(hexdigest, 16) exactly.
	n.SetString(hex.EncodeToString(sum[:]), 16)
	return n
}

// modInt returns int(h % m) for a positive small modulus m.
func modInt(h *big.Int, m int) int {
	r := new(big.Int).Mod(h, big.NewInt(int64(m)))
	return int(r.Int64())
}

// GenerateName returns a deterministic two-word display name from a
// connection id (e.g. "Red Fox"), matching Python generate_name.
func GenerateName(connectionID string) string {
	h := hashInt(connectionID)
	adj := adjectives[modInt(h, len(adjectives))]
	// (h >> 8) % len(animals): shift the digest right one byte before the
	// second table lookup so the two words vary independently.
	shifted := new(big.Int).Rsh(h, 8)
	animal := animals[modInt(shifted, len(animals))]
	return titleWord(adj) + " " + titleWord(animal)
}

// GenerateColor returns a deterministic color hex, skipping colors already in
// taken; if every color is taken it falls back to the hash-based default.
// Mirrors Python generate_color.
func GenerateColor(connectionID string, taken map[string]struct{}) string {
	h := hashInt(connectionID)
	base := modInt(h, len(colors))
	for offset := range colors {
		color := colors[(base+offset)%len(colors)]
		if _, ok := taken[color]; !ok {
			return color
		}
	}
	return colors[base] // fallback if all taken
}

// GenerateInitials returns 2-character initials from a display name, matching
// Python generate_initials: first letters of the first two whitespace-split
// words, else the first two characters — always upper-cased.
func GenerateInitials(name string) string {
	parts := strings.Fields(name)
	if len(parts) >= 2 {
		return strings.ToUpper(firstRune(parts[0]) + firstRune(parts[1]))
	}
	return strings.ToUpper(firstRunes(name, 2))
}

// titleWord capitalises the first rune of an already-lowercase word, matching
// Python str.title() on a single lowercase word.
func titleWord(s string) string {
	if s == "" {
		return ""
	}
	r := []rune(s)
	return strings.ToUpper(string(r[0])) + string(r[1:])
}

// firstRune returns the first rune of s as a string ("" when empty).
func firstRune(s string) string {
	for _, r := range s {
		return string(r)
	}
	return ""
}

// firstRunes returns up to n leading runes of s, matching Python's s[:n]
// slice semantics over code points.
func firstRunes(s string, n int) string {
	runes := []rune(s)
	if len(runes) > n {
		runes = runes[:n]
	}
	return string(runes)
}
