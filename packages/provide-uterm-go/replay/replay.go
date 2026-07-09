//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package replay rebuilds and replays terminal sessions from JSONL logs.
// Port of provide.uterm.replay (raw.py + viewer.py).
package replay

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"
)

// RebuildRawStream concatenates all "read" event raw bytes from a JSONL log
// into a single file. Port of replay.raw.rebuild_raw_stream.
func RebuildRawStream(logPath, outPath string) error {
	raw, err := os.ReadFile(logPath)
	if err != nil {
		return err
	}
	var out []byte
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			return err
		}
		if record["event"] != "read" {
			continue
		}
		data, _ := record["data"].(map[string]any)
		rawB64, _ := data["raw_bytes_b64"].(string)
		if rawB64 == "" {
			continue
		}
		decoded, err := base64.StdEncoding.DecodeString(rawB64)
		if err != nil {
			return err
		}
		out = append(out, decoded...)
	}
	return os.WriteFile(outPath, out, 0o600)
}

// ReplayOptions configure ReplayLog.
type ReplayOptions struct {
	// Speed is the playback multiplier (1.0 = real time), clamped to
	// 0.01..100. Zero selects 1.0.
	Speed float64
	// Step pauses between frames waiting for a line on Input.
	Step bool
	// Events selects which event names render; nil selects ["read","screen"].
	Events []string
	// Output receives the rendered frames; nil selects os.Stdout.
	Output io.Writer
	// Input supplies step-mode line reads; nil selects os.Stdin.
	Input io.Reader
	// Logger receives corrupt-line warnings; nil selects slog.Default().
	Logger *slog.Logger
	// Sleep is the inter-frame delay function; nil selects time.Sleep.
	// Injectable for tests.
	Sleep func(time.Duration)
}

// ReplayLog replays a JSONL session log to the terminal. Port of
// replay.viewer.replay_log.
func ReplayLog(logPath string, opts ReplayOptions) error {
	if opts.Speed == 0 {
		opts.Speed = 1.0
	}
	if opts.Output == nil {
		opts.Output = os.Stdout
	}
	if opts.Input == nil {
		opts.Input = os.Stdin
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.Sleep == nil {
		opts.Sleep = time.Sleep
	}
	events := opts.Events
	if len(events) == 0 {
		events = []string{"read", "screen"}
	}
	wanted := make(map[string]bool, len(events))
	for _, e := range events {
		wanted[e] = true
	}

	f, err := os.Open(logPath)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()

	stepReader := bufio.NewReader(opts.Input)
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	var lastTS float64
	haveTS := false
	lineno := 0
	for scanner.Scan() {
		lineno++
		line := scanner.Text()
		if strings.TrimSpace(line) == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			opts.Logger.Warn("replay_log corrupt line skipped", "path", logPath, "line", lineno)
			continue
		}
		event, _ := record["event"].(string)
		if !wanted[event] {
			continue
		}
		data, _ := record["data"].(map[string]any)
		screenVal, ok := data["screen"]
		if !ok || screenVal == nil {
			continue
		}
		screen, _ := screenVal.(string)

		ts, tsOK := record["ts"].(float64)
		if haveTS && !opts.Step && tsOK {
			delta := (ts - lastTS) / min(max(opts.Speed, 0.01), 100.0)
			if delta > 0 {
				opts.Sleep(time.Duration(delta * float64(time.Second)))
			}
		}
		if _, err := fmt.Fprint(opts.Output, "\x1b[2J\x1b[H", screen); err != nil {
			return err
		}
		if opts.Step {
			if _, err := fmt.Fprint(opts.Output, "-- next --"); err != nil {
				return err
			}
			if _, err := stepReader.ReadString('\n'); err != nil && err != io.EOF {
				return err
			}
		}
		if tsOK {
			lastTS = ts
			haveTS = true
		}
	}
	return scanner.Err()
}
