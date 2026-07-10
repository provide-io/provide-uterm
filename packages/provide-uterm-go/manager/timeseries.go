//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

// TimeseriesPlugin lets a game customize row building and summaries. The bare
// manager uses a nil plugin. Mirrors manager/protocols.py TimeseriesPlugin.
type TimeseriesPlugin interface {
	BuildRow(status *SwarmStatus, reason string) map[string]any
	GetSummary(tm *TimeseriesManager, windowMinutes int) map[string]any
}

// TimeseriesManager records swarm status snapshots to a JSONL file and reads
// them back, mirroring manager/timeseries/manager.py TimeseriesManager.
type TimeseriesManager struct {
	getStatus    func() *SwarmStatus
	IntervalS    int
	Dir          string
	Path         string
	SamplesCount int
	plugin       TimeseriesPlugin
	maxBytes     int64

	now    func() float64
	logger *slog.Logger

	mu sync.Mutex
	fh *os.File
}

// NewTimeseriesManager constructs a manager rooted at timeseriesDir. now is
// injectable for testing (nil defaults to wall-clock).
func NewTimeseriesManager(getStatus func() *SwarmStatus, timeseriesDir string, intervalS int, plugin TimeseriesPlugin, now func() float64) *TimeseriesManager {
	if timeseriesDir == "" {
		timeseriesDir = "logs/metrics"
	}
	if now == nil {
		now = nowUnix
	}
	interval := intervalS
	if interval < 1 {
		interval = 1
	}
	tm := &TimeseriesManager{
		getStatus: getStatus,
		IntervalS: interval,
		Dir:       timeseriesDir,
		plugin:    plugin,
		maxBytes:  TimeseriesMaxBytes,
		now:       now,
		logger:    getLogger("provide.uterm.manager.timeseries"),
	}
	_ = os.MkdirAll(timeseriesDir, 0o755)
	stamp := time.Unix(int64(now()), 0).Format("20060102_150405")
	tm.Path = filepath.Join(timeseriesDir, "swarm_timeseries_"+stamp+".jsonl")
	tm.cleanupOld(TimeseriesRetentionS)
	return tm
}

// GetInfo returns timeseries metadata.
func (tm *TimeseriesManager) GetInfo() map[string]any {
	return map[string]any{
		"path":             tm.Path,
		"interval_seconds": tm.IntervalS,
		"samples":          tm.SamplesCount,
	}
}

// GetRecent returns recent timeseries rows trimmed to the latest epoch.
func (tm *TimeseriesManager) GetRecent(limit int) []map[string]any {
	capped := limit
	if capped < 1 {
		capped = 1
	}
	if capped > 5000 {
		capped = 5000
	}
	rows := tm.ReadTail(capped)
	return trimToLatestEpoch(rows)
}

// GetSummary builds a trailing-window summary (delegates to a plugin).
func (tm *TimeseriesManager) GetSummary(windowMinutes int) map[string]any {
	if tm.plugin != nil {
		return tm.plugin.GetSummary(tm, windowMinutes)
	}
	return map[string]any{
		"window_minutes": windowMinutes,
		"rows":           0,
		"error":          "no timeseries plugin configured",
	}
}

// readTailBytes reads the raw bytes from the tail of the timeseries file.
func (tm *TimeseriesManager) readTailBytes(capped int) ([]byte, error) {
	f, err := os.Open(tm.Path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }()
	end, err := f.Seek(0, io.SeekEnd)
	if err != nil || end <= 0 {
		return nil, err
	}
	pos := end
	const chunkSize = 64 * 1024
	targetLines := capped + 32
	var buf []byte
	newlineCount := 0
	for pos > 0 && newlineCount < targetLines {
		readSize := int64(chunkSize)
		if readSize > pos {
			readSize = pos
		}
		pos -= readSize
		if _, err := f.Seek(pos, io.SeekStart); err != nil {
			break
		}
		chunk := make([]byte, readSize)
		n, err := io.ReadFull(f, chunk)
		if err != nil && n == 0 {
			break
		}
		buf = append(chunk[:n], buf...)
		newlineCount = bytes.Count(buf, []byte("\n"))
	}
	return buf, nil
}

// ReadTail reads up to limit most recent JSONL rows.
func (tm *TimeseriesManager) ReadTail(limit int) []map[string]any {
	capped := limit
	if capped < 1 {
		capped = 1
	}
	if _, err := os.Stat(tm.Path); err != nil {
		return []map[string]any{}
	}
	buf, err := tm.readTailBytes(capped)
	if err != nil {
		return []map[string]any{}
	}
	lines := bytes.Split(buf, []byte("\n"))
	if len(lines) > capped {
		lines = lines[len(lines)-capped:]
	}
	rows := []map[string]any{}
	for _, raw := range lines {
		line := bytes.TrimSpace(raw)
		if len(line) == 0 {
			continue
		}
		var row map[string]any
		if err := json.Unmarshal(line, &row); err != nil {
			continue
		}
		rows = append(rows, row)
	}
	return rows
}

// asInt coerces a JSON number/int to int (JSON numbers decode as float64).
func asInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	}
	return 0
}

// trimToLatestEpoch trims rows to the latest continuous run epoch, mirroring
// the static method of the same name.
func trimToLatestEpoch(rows []map[string]any) []map[string]any {
	if len(rows) <= 1 {
		return rows
	}
	latestEpochStart := 0
	prevTurns := asInt(rows[0]["total_turns"])
	prevAgents := asInt(rows[0]["total_agents"])
	for idx := 1; idx < len(rows); idx++ {
		row := rows[idx]
		curTurns := asInt(row["total_turns"])
		curAgents := asInt(row["total_agents"])
		turnDrop := prevTurns - curTurns
		dropThreshold := EpochTurnDropMin
		if scaled := int(float64(prevTurns) * EpochTurnDropRatio); scaled > dropThreshold {
			dropThreshold = scaled
		}
		hardTurnReset := turnDrop > dropThreshold
		if hardTurnReset || (prevAgents > 0 && curAgents == 0) {
			latestEpochStart = idx
		}
		prevTurns = curTurns
		prevAgents = curAgents
	}
	return rows[latestEpochStart:]
}

// buildRow builds one timeseries sample row.
func (tm *TimeseriesManager) buildRow(status *SwarmStatus, reason string) map[string]any {
	if tm.plugin != nil {
		return tm.plugin.BuildRow(status, reason)
	}
	return map[string]any{
		"ts":             tm.now(),
		"reason":         reason,
		"total_agents":   status.TotalAgents,
		"running":        status.Running,
		"completed":      status.Completed,
		"errors":         status.Errors,
		"stopped":        status.Stopped,
		"uptime_seconds": status.UptimeSeconds,
	}
}

// cleanupOld deletes timeseries files older than retentionS.
func (tm *TimeseriesManager) cleanupOld(retentionS float64) {
	cutoff := tm.now() - retentionS
	entries, err := filepath.Glob(filepath.Join(tm.Dir, "swarm_timeseries_*.jsonl"))
	if err != nil {
		return
	}
	for _, f := range entries {
		if f == tm.Path {
			continue
		}
		info, err := os.Stat(f)
		if err != nil {
			continue
		}
		if float64(info.ModTime().UnixNano())/1e9 < cutoff {
			if err := os.Remove(f); err == nil {
				tm.logger.Info("timeseries_cleanup", "path", f)
			}
		}
	}
}

// ensureFh returns the persistent file handle, opening it if needed.
func (tm *TimeseriesManager) ensureFh() (*os.File, error) {
	if tm.fh == nil {
		f, err := os.OpenFile(tm.Path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
		if err != nil {
			return nil, err
		}
		tm.fh = f
	}
	return tm.fh, nil
}

// closeFh closes the persistent file handle if open.
func (tm *TimeseriesManager) closeFh() {
	if tm.fh != nil {
		_ = tm.fh.Close()
		tm.fh = nil
	}
}

// rotateIfNeeded rotates the current file if it exceeds the size limit.
func (tm *TimeseriesManager) rotateIfNeeded() {
	info, err := os.Stat(tm.Path)
	if err != nil {
		return
	}
	if info.Size() <= tm.maxBytes {
		return
	}
	tm.closeFh()
	stem := filepath.Base(tm.Path)
	if ext := filepath.Ext(stem); ext != "" {
		stem = stem[:len(stem)-len(ext)]
	}
	archived := filepath.Join(tm.Dir, stem+"_"+strconv.Itoa(tm.SamplesCount)+".jsonl")
	if err := os.Rename(tm.Path, archived); err == nil {
		tm.logger.Info("timeseries_rotated", "archived", archived)
	}
}

// WriteSample appends one timeseries sample row.
func (tm *TimeseriesManager) WriteSample(status *SwarmStatus, reason string) {
	tm.mu.Lock()
	defer tm.mu.Unlock()
	row := tm.buildRow(status, reason)
	fh, err := tm.ensureFh()
	if err != nil {
		tm.closeFh()
		tm.logger.Error("failed_to_write_timeseries_sample", "error", err.Error())
		return
	}
	b, err := json.Marshal(row)
	if err != nil {
		tm.closeFh()
		tm.logger.Error("failed_to_write_timeseries_sample", "error", err.Error())
		return
	}
	w := bufio.NewWriter(fh)
	if _, err := w.Write(append(b, '\n')); err != nil {
		tm.closeFh()
		tm.logger.Error("failed_to_write_timeseries_sample", "error", err.Error())
		return
	}
	_ = w.Flush()
	tm.rotateIfNeeded()
}

// Loop continuously writes timeseries samples until ctx is cancelled.
func (tm *TimeseriesManager) Loop(ctx context.Context) {
	tm.WriteSample(tm.getStatus(), "startup")
	tm.mu.Lock()
	tm.SamplesCount++
	tm.mu.Unlock()
	ticker := time.NewTicker(time.Duration(tm.IntervalS) * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			tm.WriteSample(tm.getStatus(), "interval")
			tm.mu.Lock()
			tm.SamplesCount++
			tm.mu.Unlock()
		}
	}
}
