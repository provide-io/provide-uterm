//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package livedriver implements the Go side of the cross-language live
// conformance protocol described in conformance/live/PROTOCOL.md.
//
// The protocol's central rule is that a driver observes and never judges: it
// performs each scenario step and records what the server did, and the harness
// — one implementation, shared by every language — evaluates every
// expectation. Nothing in this package interprets a scenario's "expect" block.
//
// The binary in cmd/uterm-live-driver is a thin wrapper around [Execute]; the
// two roles live here so they stay unit-testable.
package livedriver

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// DefaultTimeoutMS is the scenario timeout applied when a scenario does not
// set one, matching scenario.schema.json's default.
const DefaultTimeoutMS = 15000

// Action names, the vocabulary every driver in every language implements.
const (
	ActionHealth          = "health"
	ActionListSessions    = "list_sessions"
	ActionGetSession      = "get_session"
	ActionSessionSnapshot = "session_snapshot"
	ActionHTTPGet         = "http_get"
	ActionHTTPPost        = "http_post"
)

// Per-step auth selectors.
const (
	AuthToken = "token"
	AuthNone  = "none"
	AuthBad   = "bad"
)

// Scenario is one scenario file. Only the fields a driver acts on are
// modelled: "expect" is deliberately absent, because a driver that could read
// its expectations could be tempted to evaluate them.
type Scenario struct {
	ID        string   `json:"id"`
	Title     string   `json:"title"`
	TimeoutMS int      `json:"timeout_ms"`
	Requires  []string `json:"requires"`
	Auth      string   `json:"auth"`
	Steps     []Step   `json:"steps"`
}

// Step is one action to perform against the server under test.
type Step struct {
	ID     string `json:"id"`
	Action string `json:"action"`
	// Auth selects the Authorization header; empty means "token".
	Auth string `json:"auth"`
	// Path is the request path for http_get/http_post.
	Path string `json:"path"`
	// SessionID names the session for get_session/session_snapshot.
	SessionID string `json:"session_id"`
	// Body is the http_post payload, kept as raw JSON so the bytes the
	// scenario wrote are the bytes that go on the wire.
	Body json.RawMessage `json:"body"`
}

// Timeout is the scenario's wall-clock budget for the whole run.
func (s *Scenario) Timeout() time.Duration {
	ms := s.TimeoutMS
	if ms <= 0 {
		ms = DefaultTimeoutMS
	}
	return time.Duration(ms) * time.Millisecond
}

// AuthMode is the auth mode the server driver should be started in.
func (s *Scenario) AuthMode() string {
	if mode := strings.TrimSpace(s.Auth); mode != "" {
		return mode
	}
	return "dev_token"
}

// ParseScenario decodes scenario JSON. Unknown top-level fields are tolerated
// so a harness can add metadata without breaking older drivers; what a driver
// must have — an id and at least one step — is checked.
func ParseScenario(data []byte) (*Scenario, error) {
	var sc Scenario
	if err := json.Unmarshal(data, &sc); err != nil {
		return nil, fmt.Errorf("scenario is not valid JSON: %w", err)
	}
	if strings.TrimSpace(sc.ID) == "" {
		return nil, errors.New("scenario has no id")
	}
	if len(sc.Steps) == 0 {
		return nil, errors.New("scenario has no steps")
	}
	return &sc, nil
}

// LoadScenario reads and parses a scenario file.
func LoadScenario(path string) (*Scenario, error) {
	data, err := os.ReadFile(path) //nolint:gosec // path is harness-supplied
	if err != nil {
		return nil, err
	}
	return ParseScenario(data)
}

// ScenarioIDFromPath derives a scenario id from a file name. It is the
// fallback used when a scenario could not be parsed at all, so an error result
// is still attributable to the scenario that produced it.
func ScenarioIDFromPath(path string) string {
	base := filepath.Base(path)
	if base == "." || base == string(filepath.Separator) {
		return ""
	}
	return strings.TrimSuffix(base, filepath.Ext(base))
}

// MissingCapabilities returns the entries of requires that have does not
// contain, preserving the order they were required in.
func MissingCapabilities(requires, have []string) []string {
	present := make(map[string]struct{}, len(have))
	for _, c := range have {
		present[c] = struct{}{}
	}
	var missing []string
	for _, req := range requires {
		if _, ok := present[req]; !ok {
			missing = append(missing, req)
		}
	}
	return missing
}
