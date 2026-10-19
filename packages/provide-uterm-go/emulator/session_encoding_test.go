//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package emulator

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// conformance/snapshot/session_encoding.json: the reads a session's emulator
// is fed, its receive encoding, and what the Python emulator holds after them.

type sessionEncodingRecord struct {
	Name     string   `json:"name"`
	Encoding string   `json:"encoding"`
	Cols     int      `json:"cols"`
	Rows     int      `json:"rows"`
	Reads    []string `json:"reads"`
	Screen   string   `json:"screen"`
	Cursor   struct {
		X, Y int
	} `json:"cursor"`
	ANSIScreen string `json:"ansi_screen"`
}

func loadSessionEncoding(t *testing.T) []sessionEncodingRecord {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	rel := filepath.Join("conformance", "snapshot", "session_encoding.json")
	for range 8 {
		if raw, readErr := os.ReadFile(filepath.Join(dir, rel)); readErr == nil {
			var corpus struct {
				Records []sessionEncodingRecord `json:"records"`
			}
			if err := json.Unmarshal(raw, &corpus); err != nil {
				t.Fatal(err)
			}
			return corpus.Records
		}
		dir = filepath.Dir(dir)
	}
	t.Fatalf("could not locate %s above the package directory", rel)
	return nil
}

func TestSessionEncodingCorpusMatchesTheReference(t *testing.T) {
	records := loadSessionEncoding(t)
	encodings := map[string]bool{}
	for _, record := range records {
		encodings[record.Encoding] = true
		encoding, ok := map[string]Encoding{"utf-8": UTF8, "cp437": CP437}[record.Encoding]
		if !ok {
			t.Fatalf("%s: unknown encoding %q", record.Name, record.Encoding)
		}
		e := NewWithEncoding(record.Cols, record.Rows, "", encoding)
		for _, read := range record.Reads {
			data, err := base64.StdEncoding.DecodeString(read)
			if err != nil {
				t.Fatal(err)
			}
			e.Process(data)
		}
		snap := e.GetSnapshot()
		if snap.Screen != record.Screen || snap.Cursor.X != record.Cursor.X || snap.Cursor.Y != record.Cursor.Y {
			t.Errorf("%s: screen %q cursor (%d,%d), want %q (%d,%d)", record.Name, snap.Screen, snap.Cursor.X,
				snap.Cursor.Y, record.Screen, record.Cursor.X, record.Cursor.Y)
		}
		if got := e.ANSIScreen(); got != record.ANSIScreen {
			t.Errorf("%s: ansi screen\n got %q\nwant %q", record.Name, got, record.ANSIScreen)
		}
	}
	if len(records) < 10 || !encodings["utf-8"] || !encodings["cp437"] {
		t.Fatalf("corpus has %d records over %v", len(records), encodings)
	}
}

func TestNewDecodesCP437(t *testing.T) {
	e := New(10, 1, "")
	e.Process([]byte{0xcd, 0xe4, 0xb8, 0xad})
	if got := e.GetSnapshot().Screen; got != "\u2550\u03a3\u2555\u00a1      " {
		t.Fatalf("screen %q", got)
	}
}
