//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import "time"

// nowSeconds returns the current wall-clock time as fractional Unix seconds,
// matching Python's time.time(). It is a package var so tests can pin it.
var nowSeconds = func() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}

// ScreenBuffer is a buffered screen snapshot with timing metadata. Faithful
// to the Python pydantic model.
type ScreenBuffer struct {
	Screen              string
	ScreenHash          string
	Snapshot            Snapshot
	CapturedAt          float64
	MatchedPromptID     string
	TimeSinceLastChange float64
}

// BufferManager manages a screen-history buffer with timing calculation.
type BufferManager struct {
	maxSize        int
	buffer         []*ScreenBuffer
	lastHash       string
	lastChangeTime float64
}

// NewBufferManager creates a buffer manager retaining at most maxSize screens.
func NewBufferManager(maxSize int) *BufferManager {
	return &BufferManager{maxSize: maxSize}
}

// AddScreen adds a screen snapshot to the buffer and calculates its timing
// metadata, returning the created ScreenBuffer.
func (m *BufferManager) AddScreen(snapshot Snapshot) *ScreenBuffer {
	now := nowSeconds()
	if v, ok := snapshot["captured_at"]; ok {
		if f, fok := toFloat(v); fok {
			now = f
		}
	}
	screenHash, _ := snapshot["screen_hash"].(string)

	var timeSinceChange float64
	if m.lastChangeTime > 0 {
		timeSinceChange = now - m.lastChangeTime
	}
	if screenHash != m.lastHash {
		m.lastHash = screenHash
		m.lastChangeTime = now
	}

	screenText, _ := snapshot["screen"].(string)
	buf := &ScreenBuffer{
		Screen:              screenText,
		ScreenHash:          screenHash,
		Snapshot:            snapshot,
		CapturedAt:          now,
		TimeSinceLastChange: timeSinceChange,
	}
	m.buffer = append(m.buffer, buf)
	if len(m.buffer) > m.maxSize {
		m.buffer = m.buffer[len(m.buffer)-m.maxSize:]
	}
	return buf
}

// GetRecent returns the n most recent buffered screens (oldest first). When
// n is >= the buffer length, all buffered screens are returned.
func (m *BufferManager) GetRecent(n int) []*ScreenBuffer {
	if n >= len(m.buffer) {
		out := make([]*ScreenBuffer, len(m.buffer))
		copy(out, m.buffer)
		return out
	}
	out := make([]*ScreenBuffer, n)
	copy(out, m.buffer[len(m.buffer)-n:])
	return out
}

// DetectIdleState reports whether the screen has been unchanged for at least
// thresholdSeconds. A fresh manager (no screens) is never idle.
func (m *BufferManager) DetectIdleState(thresholdSeconds float64) bool {
	if m.lastChangeTime == 0 || m.lastHash == "" {
		return false
	}
	return (nowSeconds() - m.lastChangeTime) >= thresholdSeconds
}

// Len returns the number of buffered screens.
func (m *BufferManager) Len() int { return len(m.buffer) }

// MaxSize returns the configured maximum buffer size.
func (m *BufferManager) MaxSize() int { return m.maxSize }

// Clear empties the buffer and resets change tracking.
func (m *BufferManager) Clear() {
	m.buffer = nil
	m.lastHash = ""
	m.lastChangeTime = 0
}

// toFloat coerces a decoded-JSON numeric value to float64.
func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	default:
		return 0, false
	}
}
