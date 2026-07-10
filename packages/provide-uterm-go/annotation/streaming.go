//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

// defaultMaxCarry is the longest fixed-shape secret expected to bridge a chunk
// boundary. It bounds how much of the previous chunk is retained (and
// re-scanned), capping memory and CPU.
const defaultMaxCarry = 512

// StreamingDetector is a stateful per-stream wrapper that bridges chunk
// boundaries for a PatternDetector.
//
// PatternDetector is stateless: it scans one chunk at a time, so a
// multi-character pattern (an AWS key, a URL) that straddles two Detect chunks
// is silently missed. This wrapper carries a small bounded tail of the previous
// chunk and prepends it to the next one, so a boundary-split match is still
// found.
//
// It is stateful — use one instance per logical stream (one per session, and
// not shared across event types whose text must not be concatenated). The
// wrapped PatternDetector stays stateless and may be shared.
type StreamingDetector struct {
	detector *PatternDetector
	maxCarry int
	carry    string
}

// NewStreamingDetector wraps detector with a per-stream boundary buffer. A
// maxCarry <= 0 selects the default carry bound (512 code points).
func NewStreamingDetector(detector *PatternDetector, maxCarry int) *StreamingDetector {
	if maxCarry <= 0 {
		maxCarry = defaultMaxCarry
	}
	return &StreamingDetector{detector: detector, maxCarry: maxCarry}
}

// Detect scans text (joined with the carried tail) and returns any matches.
//
// A match is owned by the chunk in which it completes — the returned
// annotation's span carries the seq passed for that chunk. The carried tail is
// the bounded window suffix after the furthest match: it bridges a secret
// straddling the next boundary (including a second one that begins right after
// a completed match) without re-reporting a match that already finished.
func (s *StreamingDetector) Detect(eventType, text string, seq int) []Annotation {
	if text == "" {
		return nil
	}
	window := text
	if s.carry != "" {
		window = s.carry + text
	}
	annotations, matchEnd := s.detector.Scan(eventType, window, seq)

	// Slice in code points to mirror Python's window[match_end:][-max_carry:].
	runes := []rune(window)
	tail := runes[matchEnd:]
	if len(tail) > s.maxCarry {
		tail = tail[len(tail)-s.maxCarry:]
	}
	s.carry = string(tail)
	return annotations
}

// Reset forgets the carried tail (e.g. on screen clear / session resync).
func (s *StreamingDetector) Reset() {
	s.carry = ""
}
