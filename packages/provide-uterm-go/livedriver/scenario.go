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
	"strconv"
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
	ActionSessionEvents   = "session_events"
	ActionSetInputMode    = "set_input_mode"
	ActionHijackAcquire   = "hijack_acquire"
	ActionHijackHeartbeat = "hijack_heartbeat"
	ActionHijackSend      = "hijack_send"
	ActionHijackStep      = "hijack_step"
	ActionHijackSnapshot  = "hijack_snapshot"
	ActionHijackRelease   = "hijack_release"
	ActionHTTPGet         = "http_get"
	ActionHTTPPost        = "http_post"
)

// What a step means when it leaves an optional argument out. These are the
// reference driver's defaults, restated here rather than left to the client
// library, so the request a scenario produces is the same in every language
// even where two libraries disagree about their own defaults.
const (
	// DefaultOwner is who takes a lease when a step does not say.
	DefaultOwner = "operator"
	// DefaultLeaseS is how long a lease runs when a step does not say.
	DefaultLeaseS = 90
	// DefaultEventsLimit is how many session events are asked for by default.
	DefaultEventsLimit = 100
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
//
// Every string field may hold a reference to what an earlier step saw
// (${id.path}); see reference.go for the grammar and resolveStep for when the
// substitution happens.
type Step struct {
	ID     string `json:"id"`
	Action string `json:"action"`
	// Auth selects the Authorization header; empty means "token".
	Auth string `json:"auth"`
	// Path is the request path for http_get/http_post.
	Path string `json:"path"`
	// SessionID names the session for the session actions.
	SessionID string `json:"session_id"`
	// WorkerID names the worker whose lease a hijack action acts on.
	WorkerID string `json:"worker_id"`
	// HijackID is the lease itself, normally a reference to the acquiring step.
	HijackID string `json:"hijack_id"`
	// Owner is who takes the lease in hijack_acquire; empty means [DefaultOwner].
	Owner string `json:"owner"`
	// LeaseS is how long an acquired or extended lease runs. A pointer so a
	// step that omits it is distinguishable from one that asked for zero.
	LeaseS *int `json:"lease_s"`
	// Keys is the input hijack_send delivers.
	Keys string `json:"keys"`
	// InputMode is the mode set_input_mode puts a session in.
	InputMode string `json:"input_mode"`
	// Limit is how many events session_events asks for; nil means the default.
	Limit *int `json:"limit"`
	// Body is the http_post payload, kept as raw JSON so the bytes the
	// scenario wrote are the bytes that go on the wire.
	Body json.RawMessage `json:"body"`
	// Repeat is how many times the step is performed, each repetition recorded
	// as its own observation. It is not an action field: it changes how often
	// the step happens, nothing about what is sent. The schema admits 2..200;
	// absent (zero here) means once.
	Repeat int `json:"repeat"`
}

// observationIDs is the ids one step's observations are recorded under.
//
// A step that runs once keeps its own id; a repeated step numbers its
// repetitions from zero — flood.0, flood.1 — and the bare id records nothing.
// Every repetition is recorded, never just the last: a scenario repeats a step
// because it expects the answers to stop being the same, so which repetition
// changed is the thing being measured, and a driver keeping only the final
// answer would turn "the thirty-first request was refused" into "a request was
// refused".
//
// There is no repeat of 1 in the schema, so an explicit 1 means what an absent
// field means rather than renumbering a single run.
func (s *Step) observationIDs() []string {
	if s.Repeat < 2 {
		return []string{s.ID}
	}
	ids := make([]string, s.Repeat)
	for index := range ids {
		ids[index] = s.ID + "." + strconv.Itoa(index)
	}
	return ids
}

// owner is who hijack_acquire takes the lease as.
func (s *Step) owner() string {
	if s.Owner == "" {
		return DefaultOwner
	}
	return s.Owner
}

// leaseS is the lease length an acquire or heartbeat asks for.
func (s *Step) leaseS() int {
	if s.LeaseS == nil {
		return DefaultLeaseS
	}
	return *s.LeaseS
}

// limit is how many events session_events asks for.
func (s *Step) limit() int {
	if s.Limit == nil {
		return DefaultEventsLimit
	}
	return *s.Limit
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
