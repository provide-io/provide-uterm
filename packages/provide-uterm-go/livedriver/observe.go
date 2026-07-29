//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// observation is the raw truth about one HTTP exchange: the status line and
// the bytes of the body, before any client library folds them into its own
// return convention.
type observation struct {
	status int
	raw    []byte
}

// observer is a RoundTripper that remembers the last response it carried.
//
// It is the Go form of the recording transport PROTOCOL.md requires: the Go
// HijackClient, like every port's, answers (ok, body) and drops the status, so
// a 401, a 403 and a 404 would all reach the matrix as the same ok:false. The
// library still performs the call and still shapes ok and body; the observer
// only writes down the status that came back. Injection needs no change to the
// client — client.WithHTTPClient already takes the *http.Client this sits under.
type observer struct {
	base http.RoundTripper

	mu   sync.Mutex
	last *observation
}

// newObserver wraps rt, defaulting to the standard transport.
func newObserver(rt http.RoundTripper) *observer {
	if rt == nil {
		rt = http.DefaultTransport
	}
	return &observer{base: rt}
}

// RoundTrip carries the request, records the response, and hands the body back
// to the caller intact.
func (o *observer) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := o.base.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	raw, readErr := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if readErr != nil {
		return nil, readErr
	}
	o.mu.Lock()
	o.last = &observation{status: resp.StatusCode, raw: raw}
	o.mu.Unlock()
	resp.Body = io.NopCloser(bytes.NewReader(raw))
	return resp, nil
}

// reset forgets the previous response so a step never reports the one before it.
func (o *observer) reset() {
	o.mu.Lock()
	o.last = nil
	o.mu.Unlock()
}

// take returns the recorded response, or nil when the request never produced one.
func (o *observer) take() *observation {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.last
}

// rawFields turns whatever the observer saw into the fields for a step the
// driver performed itself, falling back to err when no response arrived.
func (o *observer) rawFields(err error) StepFields {
	obs := o.take()
	if obs == nil {
		return failFields(errMessage(err))
	}
	return rawFields(obs.status, obs.raw)
}

// libFields records a client-library call. The status is the one the observer
// saw underneath the library; ok and body are the library's own answer, except
// that a body the library could not parse collapses to the protocol's single
// placeholder instead of the Go client's {"raw": text} shape.
func (o *observer) libFields(value any, err error) StepFields {
	obs := o.take()
	if obs == nil {
		return failFields(errMessage(err))
	}
	status := obs.status
	return StepFields{Status: &status, OK: err == nil, Body: libraryBody(obs.raw, value, err)}
}

// libraryBody extracts the body the client library shaped for a call: the
// returned value on success, the APIError's decoded body on a refusal.
func libraryBody(raw []byte, value any, err error) any {
	if !json.Valid(raw) {
		return NonJSONBody
	}
	if err == nil {
		return value
	}
	var apiErr *client.APIError
	if errors.As(err, &apiErr) {
		return apiErr.Body
	}
	// Unreachable in practice: HijackClient only ever fails with *APIError.
	return nil
}

// errMessage renders the failure of a request that produced no response.
func errMessage(err error) string {
	if err != nil {
		return err.Error()
	}
	return "no response was received"
}
