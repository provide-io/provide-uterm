//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fileio

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/ansi"
)

func TestSecureOpenAppendCreatesOwnerOnly(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "rec")
	path := filepath.Join(dir, "s.jsonl")
	f, err := SecureOpenAppend(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	if _, err := f.WriteString("x\n"); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("file perm = %o", perm)
	}
	dinfo, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if perm := dinfo.Mode().Perm(); perm != 0o700 {
		t.Fatalf("dir perm = %o", perm)
	}
}

func TestSecureOpenAppendRetightensExisting(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "loose.jsonl")
	if err := os.WriteFile(path, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	f, err := SecureOpenAppend(path)
	if err != nil {
		t.Fatal(err)
	}
	_ = f.Close()
	info, _ := os.Stat(path)
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("file perm = %o", perm)
	}
	dinfo, _ := os.Stat(dir)
	if perm := dinfo.Mode().Perm(); perm != 0o700 {
		t.Fatalf("dir perm = %o", perm)
	}
	// Append mode preserved existing content.
	raw, _ := os.ReadFile(path)
	if string(raw) != "old\n" {
		t.Fatalf("content = %q", raw)
	}
}

func TestSecureOpenAppendRefusesSymlink(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link.jsonl")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := SecureOpenAppend(link); err == nil {
		t.Fatal("expected ELOOP for symlinked recording path")
	}
}

func TestSecureOpenAppendRefusesNonRegular(t *testing.T) {
	dir := t.TempDir()
	fifo := filepath.Join(dir, "pipe.jsonl")
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		t.Fatal(err)
	}
	// A write-only fifo open blocks until a reader appears; hold a
	// non-blocking reader so SecureOpenAppend reaches the S_ISREG check.
	rfd, err := syscall.Open(fifo, syscall.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = syscall.Close(rfd) }()
	if _, err := SecureOpenAppend(fifo); err == nil {
		t.Fatal("expected error for non-regular sink")
	}
}

func TestSecureOpenAppendFdErrorPaths(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	origStat, origChmod := statFile, chmodFile
	t.Cleanup(func() { statFile, chmodFile = origStat, origChmod })

	statFile = func(*os.File) (os.FileInfo, error) { return nil, errors.New("stat failed") }
	if _, err := SecureOpenAppend(path); err == nil || err.Error() != "stat failed" {
		t.Fatalf("err = %v", err)
	}

	statFile = origStat
	chmodFile = func(*os.File, os.FileMode) error { return errors.New("chmod failed") }
	if _, err := SecureOpenAppend(path); err == nil || err.Error() != "chmod failed" {
		t.Fatalf("err = %v", err)
	}
}

func TestSecureOpenAppendDirCreateError(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "file")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	// Parent "directory" is a regular file → MkdirAll fails.
	if _, err := SecureOpenAppend(filepath.Join(blocker, "x.jsonl")); err == nil {
		t.Fatal("expected error")
	}
}

func TestLoadANSLatin1(t *testing.T) {
	path := filepath.Join(t.TempDir(), "art.ans")
	raw := []byte{0x1b, '[', '3', '1', 'm', 0xC9, 0xCD, 0xBB}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadANS(path)
	if err != nil {
		t.Fatal(err)
	}
	want := "\x1b[31mÉÍ»"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
	if _, err := LoadANS(filepath.Join(t.TempDir(), "missing.ans")); err == nil {
		t.Fatal("expected error")
	}
}

func TestLoadTxt(t *testing.T) {
	path := filepath.Join(t.TempDir(), "f.txt")
	if err := os.WriteFile(path, []byte("héllo\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadTxt(path)
	if err != nil || got != "héllo\n" {
		t.Fatalf("got %q err %v", got, err)
	}
	if _, err := LoadTxt(filepath.Join(t.TempDir(), "missing.txt")); err == nil {
		t.Fatal("expected error")
	}
}

func TestLoadPaletteDefault(t *testing.T) {
	got, err := LoadPalette("")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 16 || got[9] != ansi.DefaultPalette[9] {
		t.Fatalf("got %v", got)
	}
	// Mutating the copy must not touch the shared default.
	got[0] = 42
	if ansi.DefaultPalette[0] == 42 {
		t.Fatal("default palette mutated")
	}
}

func TestLoadPaletteFromFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "pal.json")
	if err := os.WriteFile(path, []byte("[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,255]"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadPalette(path)
	if err != nil {
		t.Fatal(err)
	}
	if got[15] != 255 || got[3] != 3 {
		t.Fatalf("got %v", got)
	}
}

func TestLoadPaletteErrors(t *testing.T) {
	dir := t.TempDir()
	cases := map[string]string{
		"short.json":    "[1,2,3]",
		"notlist.json":  `{"a":1}`,
		"badjson.json":  "{",
		"float.json":    "[0.5,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]",
		"range.json":    "[999,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]",
		"negative.json": "[-1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]",
		"string.json":   `["a",1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]`,
	}
	for name, content := range cases {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadPalette(path); err == nil {
			t.Fatalf("%s: expected error", name)
		}
	}
	if _, err := LoadPalette(filepath.Join(dir, "missing.json")); err == nil {
		t.Fatal("expected read error")
	}
}
