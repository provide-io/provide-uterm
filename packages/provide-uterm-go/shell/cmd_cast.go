//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// cmdCast fetches and replays an asciicast v2 (.cast) file. Port of
// commands/cast.py:cmd_cast.
func cmdCast(ctx context.Context, client *http.Client, arg string) Result {
	tokens := pyFields(arg)
	url := ""
	loop := false
	var fpsOverride *float64

	for i := 0; i < len(tokens); {
		tok := tokens[i]
		switch {
		case tok == "--loop":
			loop = true
			i++
		case tok == "--fps" && i+1 < len(tokens):
			f, err := strconv.ParseFloat(tokens[i+1], 64)
			if err != nil {
				return textResult(ErrorMsg("invalid --fps value: "+tokens[i+1]) + Prompt)
			}
			fpsOverride = &f
			i += 2
		case !strings.HasPrefix(tok, "--"):
			url = tok
			i++
		default:
			return textResult(ErrorMsg("unknown flag: "+tok) + Prompt)
		}
	}

	if url == "" {
		return textResult(ErrorMsg("usage: cast [--fps N] [--loop] <url>") + Prompt)
	}

	text, errRes, ok := fetchText(ctx, client, url)
	if !ok {
		return errRes
	}

	rawLines := nonBlankLines(text)
	if len(rawLines) == 0 {
		return textResult(ErrorMsg("empty cast file") + Prompt)
	}

	var header any
	if err := json.Unmarshal([]byte(rawLines[0]), &header); err != nil {
		return textResult(ErrorMsg("invalid cast header: "+err.Error()) + Prompt)
	}
	hm, isMap := header.(map[string]any)
	if !isMap {
		return textResult(ErrorMsg("invalid cast header: header is not an object") + Prompt)
	}
	if !versionIsTwo(hm["version"]) {
		return textResult(ErrorMsg("unsupported asciicast version: "+pyValue(hm["version"])) + Prompt)
	}

	type event struct {
		ts   float64
		data string
	}
	var events []event
	for _, raw := range rawLines[1:] {
		var ev any
		if err := json.Unmarshal([]byte(raw), &ev); err != nil {
			continue
		}
		arr, isArr := ev.([]any)
		if !isArr || len(arr) < 3 || fmt.Sprint(arr[1]) != "o" {
			continue
		}
		ts, tsOK := toFloat(arr[0])
		if !tsOK {
			continue
		}
		events = append(events, event{ts: ts, data: fmt.Sprint(arr[2])})
	}

	if len(events) == 0 {
		return textResult(ErrorMsg("no output events in cast file") + Prompt)
	}

	targetFPS := 15.0
	if fpsOverride != nil {
		targetFPS = *fpsOverride
	}
	frameDur := 1.0 / targetFPS
	totalDur := events[len(events)-1].ts + frameDur
	nFrames := int(totalDur / frameDur)
	if nFrames < 1 {
		nFrames = 1
	}

	buckets := make([]string, nFrames)
	for _, e := range events {
		idx := int(e.ts / frameDur)
		if idx > nFrames-1 {
			idx = nFrames - 1
		}
		// Defensive: clamp a negative index (only reachable via a malformed
		// negative timestamp). Python relies on negative-index wrap here; Go
		// would panic, so clamp to the first bucket instead.
		if idx < 0 {
			idx = 0
		}
		buckets[idx] += e.data
	}

	frames := []string{ClearScreen}
	started := false
	for _, bucket := range buckets {
		if bucket != "" || started {
			started = true
			frames = append(frames, bucket)
		}
	}

	if len(frames) <= 1 {
		return textResult(ErrorMsg("cast file has no displayable output") + Prompt)
	}

	return animatedResult(AnimatedResult{Frames: frames, FPS: targetFPS, Loop: loop})
}

// fetchText fetches the body of url as text, handling file://, http://, and
// https:// (else "unsupported URL scheme"). Shared by the cast and render
// commands' scheme handling. On failure it returns a completed error Result and
// ok=false.
func fetchText(ctx context.Context, client *http.Client, url string) (string, Result, bool) {
	data, errRes, ok := fetchBytes(ctx, client, url)
	if !ok {
		return "", errRes, false
	}
	return strings.ToValidUTF8(string(data), "�"), Result{}, true
}

// fetchBytes fetches the raw body of url. See fetchText for scheme handling.
func fetchBytes(ctx context.Context, client *http.Client, url string) ([]byte, Result, bool) {
	switch {
	case strings.HasPrefix(url, "file://"):
		path := url[7:]
		info, err := os.Stat(path)
		if err != nil || info.IsDir() {
			return nil, textResult(ErrorMsg("file not found: "+path) + Prompt), false
		}
		data, err := os.ReadFile(path) //nolint:gosec // single-tenant shell; arbitrary local path by design (parity with urllib file://)
		if err != nil {
			return nil, textResult(ErrorMsg("cannot fetch: "+err.Error()) + Prompt), false
		}
		return data, Result{}, true
	case strings.HasPrefix(url, "http://"), strings.HasPrefix(url, "https://"):
		_, data, err := doHTTP(ctx, client, http.MethodGet, url, nil, 30*time.Second, 0, "provide-uterm/1.0")
		if err != nil {
			return nil, textResult(ErrorMsg("cannot fetch: "+err.Error()) + Prompt), false
		}
		return data, Result{}, true
	default:
		return nil, textResult(ErrorMsg("unsupported URL scheme (use http://, https://, or file://)") + Prompt), false
	}
}

// nonBlankLines splits text into lines (handling \r\n, \r, \n) and drops blank
// ones, mirroring [ln for ln in text.splitlines() if ln.strip()].
func nonBlankLines(text string) []string {
	normalized := strings.ReplaceAll(text, "\r\n", "\n")
	normalized = strings.ReplaceAll(normalized, "\r", "\n")
	var out []string
	for _, ln := range strings.Split(normalized, "\n") {
		if pyStrip(ln) != "" {
			out = append(out, ln)
		}
	}
	return out
}

// versionIsTwo reports whether the JSON header version equals 2.
func versionIsTwo(v any) bool {
	f, ok := toFloat(v)
	return ok && f == 2
}

// toFloat coerces a JSON scalar (or numeric string, like Python float()) to a
// float64.
func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case int:
		return float64(x), true
	case string:
		f, err := strconv.ParseFloat(x, 64)
		return f, err == nil
	default:
		return 0, false
	}
}

// pyValue formats a JSON scalar the way Python's str() would for the cast
// header version (None for nil; integral floats without a decimal point).
func pyValue(v any) string {
	if v == nil {
		return "None"
	}
	if f, ok := v.(float64); ok {
		if f == float64(int64(f)) {
			return strconv.FormatInt(int64(f), 10)
		}
	}
	return fmt.Sprint(v)
}
