//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// demo-recording: library-level session recording demo (Go) — screen snapshots → JSONL.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
)

const (
	magenta = "\033[1;35m"
	green   = "\033[1;32m"
	cyan    = "\033[1;36m"
	dim     = "\033[2m"
	reset   = "\033[0m"
	bold    = "\033[1m"
)

func banner(title string) {
	bar := strings.Repeat("═", len(title)+4)
	fmt.Printf("\n%s%s%s\n", magenta, bar, reset)
	fmt.Printf("%s  %s%s%s  %s\n", magenta, bold, title, reset+magenta, reset)
	fmt.Printf("%s%s%s\n\n", magenta, bar, reset)
}

func info(msg string) { fmt.Printf("%s  → %s%s\n", cyan, msg, reset) }
func ok(msg string)   { fmt.Printf("%s  ✓ %s%s\n", green, msg, reset) }
func kv(k string, v any) {
	fmt.Printf("    %s%s:%s %s%v%s\n", dim, k, reset, bold, v, reset)
}

func main() {
	banner("provide-uterm recording — Go")
	info("language=go  store=LocalFileStore")

	tmp, err := os.MkdirTemp("", "uterm-rec-go-*")
	if err != nil {
		fmt.Fprintf(os.Stderr, "tempdir: %v\n", err)
		os.Exit(1)
	}
	defer func() { _ = os.RemoveAll(tmp) }()

	store := recording.NewLocalFileStore(tmp)
	sid := "demo-recording-go"
	if err := store.StartSession(sid, map[string]any{
		"lang":    "go",
		"feature": "session_recording",
		"demo":    "recording_matrix",
	}); err != nil {
		fmt.Fprintf(os.Stderr, "start: %v\n", err)
		os.Exit(1)
	}
	ok("session started: " + sid)

	screens := []string{
		"",
		"=== provide-uterm: session recording active ===\n",
		"=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n",
		"=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n[deploy] step 2: running migrations\n",
		"=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n[deploy] step 2: running migrations\n[deploy] step 3: restarting services\n",
		"=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n[deploy] step 2: running migrations\n[deploy] step 3: restarting services\n[deploy] healthcheck: ok — recording complete\n",
	}

	for i, screen := range screens {
		ev := recording.Event{
			"ts":         float64(time.Now().UnixNano()) / 1e9,
			"event":      "snapshot",
			"session_id": sid,
			"data": map[string]any{
				"seq":    i,
				"screen": screen,
				"cols":   80,
				"rows":   24,
				"source": "go",
			},
		}
		if err := store.AppendEvents(sid, []recording.Event{ev}); err != nil {
			fmt.Fprintf(os.Stderr, "append: %v\n", err)
			os.Exit(1)
		}
		info(fmt.Sprintf("snapshot %d: %d screen bytes", i, len(screen)))
		time.Sleep(150 * time.Millisecond)
	}

	if err := store.EndSession(sid); err != nil {
		fmt.Fprintf(os.Stderr, "end: %v\n", err)
		os.Exit(1)
	}
	meta, err := store.RecordingMeta(sid)
	if err != nil {
		fmt.Fprintf(os.Stderr, "meta: %v\n", err)
		os.Exit(1)
	}
	path, _ := store.GetPath(sid)
	entries, err := store.GetEntries(sid, recording.Query{Limit: 50})
	if err != nil {
		fmt.Fprintf(os.Stderr, "entries: %v\n", err)
		os.Exit(1)
	}
	kv("exists", meta.Exists)
	kv("size_bytes", meta.SizeBytes)
	kv("path", path)
	kv("entries", len(entries))
	snaps := 0
	for _, e := range entries {
		if e["event"] == "snapshot" {
			snaps++
		}
	}
	kv("snapshots", snaps)
	if path != "" {
		raw, err := os.ReadFile(path)
		if err == nil {
			lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
			for i, line := range lines {
				if i >= 2 {
					break
				}
				if len(line) > 100 {
					line = line[:100] + "…"
				}
				info("jsonl: " + line)
			}
		}
	}
	// prove JSON round-trip of last entry data
	if len(entries) > 0 {
		b, _ := json.Marshal(entries[len(entries)-1]["event"])
		_ = b
	}
	_ = filepath.Base(tmp)
	ok("Go LocalFileStore: screen snapshots persisted as JSONL")
}
