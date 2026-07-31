//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bytes"
	"encoding/json"
	"io"
)

// Language is this driver's entry in result.schema.json's language enum.
const Language = "go"

// Roles a driver can report.
const (
	RoleClient = "client"
	RoleServer = "server"
)

// Run statuses. They describe the run, never a verdict: a step that got an
// HTTP 500 still completed, because the 500 is the observation.
const (
	// StatusCompleted means every step ran.
	StatusCompleted = "completed"
	// StatusUnsupported means the scenario needs a capability this driver lacks.
	StatusUnsupported = "unsupported"
	// StatusError means the driver itself failed.
	StatusError = "error"
)

// NonJSONBody is recorded in place of a response body that is not JSON. A body
// nobody can parse is the same observation in every language; the bytes are not.
const NonJSONBody = "<non-json>"

// StepFields is what a driver observed performing one step.
type StepFields struct {
	// Status is the HTTP status, or nil when no response was received.
	Status *int `json:"status"`
	// OK is true for a 2xx status.
	OK bool `json:"ok"`
	// Body is the parsed JSON body, or [NonJSONBody].
	Body any `json:"body"`
	// Error is set only when the driver got no HTTP response at all — a
	// transport failure or a step it could not perform. A 4xx/5xx is an
	// observation, not an error, and leaves this nil so languages whose client
	// libraries raise on non-2xx stay comparable with those that do not.
	Error *string `json:"error"`
}

// StepResult pairs a step id with what was observed.
type StepResult struct {
	ID     string     `json:"id"`
	Fields StepFields `json:"fields"`
}

// Result is one driver's report for one scenario (result.schema.json).
type Result struct {
	ScenarioID   string       `json:"scenario_id"`
	Language     string       `json:"language"`
	Role         string       `json:"role"`
	Status       string       `json:"status"`
	Capabilities []string     `json:"capabilities"`
	Steps        []StepResult `json:"steps"`
	Error        *string      `json:"error"`
}

// ServerLine is the single line a `serve` driver writes to stdout.
type ServerLine struct {
	Role         string   `json:"role"`
	Language     string   `json:"language"`
	BaseURL      string   `json:"base_url"`
	Token        string   `json:"token"`
	Capabilities []string `json:"capabilities"`
}

// Capabilities is what the Go driver reports it can do:
//
//   - hijack.rest      the lease/send/step/release REST surface of HijackClient
//   - sessions.rest    session list/get/snapshot through the client library
//   - http.raw         arbitrary http_get/http_post steps
//   - auth.dev_token   `serve --auth dev_token` mints a presentable bearer token
//   - status.observed  the HTTP status a client-library call returned is
//     recorded, not just the library's (ok, body) — the Go HijackClient takes
//     an injected *http.Client (WithHTTPClient), so an http.RoundTripper can
//     write down the status the library discards
//   - fanout.rest.strict  the served fan-out REST surface rejects dormant
//     members by default
//
// It returns a fresh slice so a caller cannot mutate the driver's answer.
func Capabilities() []string {
	return []string{"hijack.rest", "sessions.rest", "http.raw", "auth.dev_token", "status.observed", "fanout.rest.strict"}
}

// newResult seeds a result with the fields every outcome shares.
func newResult(scenarioID, status string) Result {
	return Result{
		ScenarioID:   scenarioID,
		Language:     Language,
		Role:         RoleClient,
		Status:       status,
		Capabilities: Capabilities(),
		Steps:        []StepResult{},
	}
}

// errorResult is a result for a driver-level failure.
func errorResult(scenarioID string, err error) Result {
	r := newResult(scenarioID, StatusError)
	msg := err.Error()
	r.Error = &msg
	return r
}

// rawFields records a response the driver made itself (http_get/http_post):
// status, whether it was 2xx, and the parsed body.
func rawFields(status int, raw []byte) StepFields {
	return StepFields{Status: &status, OK: status >= 200 && status < 300, Body: decodeBody(raw)}
}

// failFields records a step that produced no HTTP response.
func failFields(msg string) StepFields {
	return StepFields{Status: nil, OK: false, Body: nil, Error: &msg}
}

// decodeBody parses response bytes as JSON, falling back to [NonJSONBody].
// Numbers are kept as json.Number so an integer stays an integer when the
// result is re-encoded and compared against another language's.
//
// The whole body must be one JSON value. Decoding straight from a stream would
// accept a leading token and drop the rest — net/http's plain-text
// "404 page not found" would be recorded as the number 404, which is exactly
// the kind of language-specific reading the "<non-json>" placeholder exists to
// prevent.
func decodeBody(raw []byte) any {
	if !json.Valid(raw) {
		return NonJSONBody
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	// Cannot fail: json.Valid already vetted the bytes.
	_ = dec.Decode(&v)
	return v
}

// WriteLine writes v as exactly one line of JSON — the driver's whole output
// contract. HTML escaping is off so a body containing "<" survives verbatim.
func WriteLine(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(v)
}
