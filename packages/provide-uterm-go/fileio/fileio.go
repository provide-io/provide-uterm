//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package fileio provides file I/O helpers for secure recording sinks, BBS
// screen files, and color palettes. Port of provide.uterm.file_io.
package fileio

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"syscall"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ansi"
)

func ensureOwnerOnlyDir(directory string, mode os.FileMode) error {
	if err := os.MkdirAll(directory, mode); err != nil {
		return err
	}
	// MkdirAll only applies the mode when it creates the directory — a
	// pre-existing world-readable dir would stay that way. Re-tighten
	// unconditionally so recording filenames are never enumerable by other
	// local users.
	return os.Chmod(directory, mode)
}

// SecureOpenAppend creates/opens path for append with owner-only permissions
// and no symlink following:
//
//   - O_NOFOLLOW refuses to open a symlink at the target path, so an attacker
//     who pre-creates one cannot redirect writes; a symlinked recording path
//     is an attack or misconfiguration and fails loudly (ELOOP).
//   - The create mode only applies on creation, so a pre-existing loose file
//     would keep its mode; fchmod on the just-opened fd re-tightens it to
//     0600 with no TOCTOU race (it targets the fd, not the path).
//   - A regular-file check rejects a fifo/device pre-created at the path.
func SecureOpenAppend(path string) (*os.File, error) {
	return SecureOpenAppendMode(path, 0o600, 0o700)
}

// SecureOpenAppendMode is SecureOpenAppend with explicit file and directory
// modes.
func SecureOpenAppendMode(path string, mode, dirMode os.FileMode) (*os.File, error) {
	if err := ensureOwnerOnlyDir(filepath.Dir(path), dirMode); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_APPEND|syscall.O_NOFOLLOW, mode)
	if err != nil {
		return nil, err
	}
	info, err := statFile(f)
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	if !info.Mode().IsRegular() {
		_ = f.Close()
		return nil, fmt.Errorf("refusing to open non-regular recording sink: %s", path)
	}
	if err := chmodFile(f, mode); err != nil {
		_ = f.Close()
		return nil, err
	}
	return f, nil
}

// statFile and chmodFile are indirection points so tests can exercise the
// fd-level error paths, which cannot be triggered deterministically through
// the filesystem.
var (
	statFile  = func(f *os.File) (os.FileInfo, error) { return f.Stat() }
	chmodFile = func(f *os.File, mode os.FileMode) error { return f.Chmod(mode) }
)

// LoadANS loads a .ans file (BBS ANSI art) as latin-1 text: every byte maps
// one-to-one onto codepoints U+0000-U+00FF.
func LoadANS(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	runes := make([]rune, len(raw))
	for i, b := range raw {
		runes[i] = rune(b)
	}
	return string(runes), nil
}

// LoadTxt loads a plain UTF-8 text file.
func LoadTxt(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// LoadPalette loads a JSON 256-color palette (list of 16 ints 0-255). An
// empty path returns a copy of ansi.DefaultPalette.
func LoadPalette(path string) ([]int, error) {
	if path == "" {
		out := make([]int, len(ansi.DefaultPalette))
		copy(out, ansi.DefaultPalette)
		return out, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var data []any
	if err := json.Unmarshal(raw, &data); err != nil || len(data) != 16 {
		return nil, fmt.Errorf("palette map must be a JSON list of 16 integers")
	}
	out := make([]int, 0, 16)
	for _, v := range data {
		f, ok := v.(float64)
		if !ok || f != math.Trunc(f) || f < 0 || f > 255 {
			return nil, fmt.Errorf("palette map values must be integers in 0..255")
		}
		out = append(out, int(f))
	}
	return out, nil
}
