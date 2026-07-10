//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"context"
	"net/url"
	"strconv"
	"time"
)

// Endpoint default values, mirroring the Python HijackClient method defaults.
// A zero-valued option field selects the corresponding default so callers can
// leave optional knobs unset.
const (
	defaultLeaseS         = 90
	defaultSendTimeoutMS  = 2000
	defaultPollIntervalMS = 120
	defaultSnapshotWaitMS = 1500
	defaultEventsLimit    = 200
	defaultSessionLimit   = 100
	defaultWatchTimeoutMS = 5000
	defaultWatchMaxEvents = 50
)

func orDefault(v, dflt int) int {
	if v <= 0 {
		return dflt
	}
	return v
}

// -- hijack lifecycle -----------------------------------------------------

// AcquireOptions configures Acquire. Owner defaults to "operator" and LeaseS to
// 90 when left empty/zero.
type AcquireOptions struct {
	Owner  string
	LeaseS int
}

// Acquire acquires a lease-based hijack session for a worker.
func (c *HijackClient) Acquire(ctx context.Context, workerID string, opts AcquireOptions) (map[string]any, error) {
	path, err := c.wp(workerID)
	if err != nil {
		return nil, err
	}
	owner := opts.Owner
	if owner == "" {
		owner = "operator"
	}
	return c.requestObject(ctx, "POST", path+"/hijack/acquire", map[string]any{
		"owner":   owner,
		"lease_s": orDefault(opts.LeaseS, defaultLeaseS),
	}, nil, 0)
}

// Heartbeat extends a hijack lease. leaseS<=0 selects the default (90).
func (c *HijackClient) Heartbeat(ctx context.Context, workerID, hijackID string, leaseS int) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/heartbeat", map[string]any{
		"lease_s": orDefault(leaseS, defaultLeaseS),
	}, nil, 0)
}

// SendOptions configures Send. Keys is required; the two *MS knobs default to
// 2000 and 120 respectively when zero. Expect* fields are only sent when set.
type SendOptions struct {
	Keys           string
	ExpectPromptID string
	ExpectRegex    string
	TimeoutMS      int
	PollIntervalMS int
}

// Send sends input to a hijacked worker, optionally guarded by a prompt-id or
// regex expectation.
func (c *HijackClient) Send(ctx context.Context, workerID, hijackID string, opts SendOptions) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	body := map[string]any{
		"keys":             opts.Keys,
		"timeout_ms":       orDefault(opts.TimeoutMS, defaultSendTimeoutMS),
		"poll_interval_ms": orDefault(opts.PollIntervalMS, defaultPollIntervalMS),
	}
	if opts.ExpectPromptID != "" {
		body["expect_prompt_id"] = opts.ExpectPromptID
	}
	if opts.ExpectRegex != "" {
		body["expect_regex"] = opts.ExpectRegex
	}
	return c.requestObject(ctx, "POST", path+"/send", body, nil, 0)
}

// Step single-steps a hijacked worker loop.
func (c *HijackClient) Step(ctx context.Context, workerID, hijackID string) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/step", nil, nil, 0)
}

// Release releases a hijack session and resumes worker automation.
func (c *HijackClient) Release(ctx context.Context, workerID, hijackID string) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/release", nil, nil, 0)
}

// Snapshot reads a terminal snapshot from an active hijack session. waitMS<=0
// selects the default (1500).
func (c *HijackClient) Snapshot(ctx context.Context, workerID, hijackID string, waitMS int) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	params := url.Values{"wait_ms": {strconv.Itoa(orDefault(waitMS, defaultSnapshotWaitMS))}}
	return c.requestObject(ctx, "GET", path+"/snapshot", nil, params, 0)
}

// EventsOptions configures Events. AfterSeq defaults to 0 (valid); Limit
// defaults to 200 when zero.
type EventsOptions struct {
	AfterSeq int
	Limit    int
}

// Events reads events from an active hijack session.
func (c *HijackClient) Events(ctx context.Context, workerID, hijackID string, opts EventsOptions) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	params := url.Values{
		"after_seq": {strconv.Itoa(opts.AfterSeq)},
		"limit":     {strconv.Itoa(orDefault(opts.Limit, defaultEventsLimit))},
	}
	return c.requestObject(ctx, "GET", path+"/events", nil, params, 0)
}

// -- worker control -------------------------------------------------------

// SetInputMode sets a worker's input mode ("hijack" or "open").
func (c *HijackClient) SetInputMode(ctx context.Context, workerID, mode string) (map[string]any, error) {
	path, err := c.wp(workerID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/input_mode", map[string]any{"input_mode": mode}, nil, 0)
}

// DisconnectWorker forcibly drops a worker's WebSocket connection.
func (c *HijackClient) DisconnectWorker(ctx context.Context, workerID string) (map[string]any, error) {
	path, err := c.wp(workerID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/disconnect_worker", nil, nil, 0)
}

// -- session API (/api prefix) --------------------------------------------

// Health performs a health check.
func (c *HijackClient) Health(ctx context.Context) (map[string]any, error) {
	return c.requestObject(ctx, "GET", "/api/health", nil, nil, 0)
}

// ListSessions lists all sessions (a JSON array).
func (c *HijackClient) ListSessions(ctx context.Context) (any, error) {
	return c.requestAny(ctx, "GET", "/api/sessions", nil, 0)
}

// GetSession returns a single session's status.
func (c *HijackClient) GetSession(ctx context.Context, sessionID string) (map[string]any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "GET", path, nil, nil, 0)
}

// SessionSnapshot returns a terminal snapshot for a session.
func (c *HijackClient) SessionSnapshot(ctx context.Context, sessionID string) (any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	return c.requestAny(ctx, "GET", path+"/snapshot", nil, 0)
}

// SessionEvents returns events for a session. limit<=0 selects the default (100).
func (c *HijackClient) SessionEvents(ctx context.Context, sessionID string, limit int) (any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	params := url.Values{"limit": {strconv.Itoa(orDefault(limit, defaultSessionLimit))}}
	return c.requestAny(ctx, "GET", path+"/events", params, 0)
}

// WatchOptions configures WatchSessionEvents. TimeoutMS defaults to 5000 and
// MaxEvents to 50 when zero; EventTypes/Pattern are only sent when set.
type WatchOptions struct {
	EventTypes string
	Pattern    string
	TimeoutMS  int
	MaxEvents  int
}

// WatchSessionEvents long-polls the session event stream. The HTTP request
// timeout is set to timeout_ms + 5s to accommodate the server-side wait plus
// network overhead, matching the Python client.
func (c *HijackClient) WatchSessionEvents(ctx context.Context, sessionID string, opts WatchOptions) (any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	timeoutMS := orDefault(opts.TimeoutMS, defaultWatchTimeoutMS)
	params := url.Values{
		"timeout_ms": {strconv.Itoa(timeoutMS)},
		"max_events": {strconv.Itoa(orDefault(opts.MaxEvents, defaultWatchMaxEvents))},
	}
	if opts.EventTypes != "" {
		params.Set("event_types", opts.EventTypes)
	}
	if opts.Pattern != "" {
		params.Set("pattern", opts.Pattern)
	}
	reqTimeout := time.Duration(timeoutMS)*time.Millisecond + 5*time.Second
	return c.requestAny(ctx, "GET", path+"/events/watch", params, reqTimeout)
}

// SetSessionMode sets a session's input mode.
func (c *HijackClient) SetSessionMode(ctx context.Context, sessionID, mode string) (map[string]any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/mode", map[string]any{"input_mode": mode}, nil, 0)
}

// ConnectSession starts/connects a session.
func (c *HijackClient) ConnectSession(ctx context.Context, sessionID string) (map[string]any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/connect", nil, nil, 0)
}

// DisconnectSession stops/disconnects a session.
func (c *HijackClient) DisconnectSession(ctx context.Context, sessionID string) (map[string]any, error) {
	path, err := c.sp(sessionID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "POST", path+"/disconnect", nil, nil, 0)
}

// QuickConnectOptions configures QuickConnect. DisplayName is only sent when
// set; Config carries extra connector configuration merged into the body.
type QuickConnectOptions struct {
	DisplayName string
	Config      map[string]any
}

// QuickConnect creates an ephemeral session via quick-connect.
func (c *HijackClient) QuickConnect(ctx context.Context, connectorType string, opts QuickConnectOptions) (map[string]any, error) {
	body := map[string]any{"connector_type": connectorType}
	if opts.DisplayName != "" {
		body["display_name"] = opts.DisplayName
	}
	for k, v := range opts.Config {
		body[k] = v
	}
	return c.requestObject(ctx, "POST", "/api/connect", body, nil, 0)
}

// Post is a generic POST helper for API paths not covered by a dedicated
// method.
func (c *HijackClient) Post(ctx context.Context, path string, body map[string]any) (any, error) {
	return c.doRequest(ctx, "POST", path, body, nil, 0)
}
