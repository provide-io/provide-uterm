//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// roundTripFunc adapts a function to http.RoundTripper.
type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// badBody fails on Read, standing in for a connection that dies mid-body.
type badBody struct{}

func (badBody) Read([]byte) (int, error) { return 0, errors.New("body read failed") }
func (badBody) Close() error             { return nil }

func TestObserverRecordsStatusAndLeavesBodyReadable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTeapot)
		_, _ = io.WriteString(w, `{"hello":"world"}`)
	}))
	defer srv.Close()

	obs := newObserver(nil)
	hc := &http.Client{Transport: obs}
	resp, err := hc.Get(srv.URL) //nolint:noctx // test
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if string(body) != `{"hello":"world"}` {
		t.Fatalf("caller lost the body: %q", body)
	}
	rec := obs.take()
	if rec == nil || rec.status != http.StatusTeapot || string(rec.raw) != `{"hello":"world"}` {
		t.Fatalf("observation = %+v", rec)
	}
}

func TestObserverResetForgetsThePreviousStep(t *testing.T) {
	obs := newObserver(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
	}))
	req, err := http.NewRequest(http.MethodGet, "http://example.invalid/x", nil) //nolint:noctx // test
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if _, err := obs.RoundTrip(req); err != nil {
		t.Fatalf("round trip: %v", err)
	}
	if obs.take() == nil {
		t.Fatal("expected an observation")
	}
	obs.reset()
	if obs.take() != nil {
		t.Fatal("reset did not clear the observation")
	}
}

func TestObserverTransportAndBodyFailures(t *testing.T) {
	req, err := http.NewRequest(http.MethodGet, "http://example.invalid/x", nil) //nolint:noctx // test
	if err != nil {
		t.Fatalf("new request: %v", err)
	}

	dead := newObserver(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, errors.New("dial failed")
	}))
	if _, err := dead.RoundTrip(req); err == nil || dead.take() != nil {
		t.Fatalf("a transport failure must record nothing: err=%v", err)
	}

	torn := newObserver(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: badBody{}}, nil
	}))
	if _, err := torn.RoundTrip(req); err == nil || torn.take() != nil {
		t.Fatalf("an unreadable body must record nothing: err=%v", err)
	}
}

func TestObserverRawFieldsWithoutAResponse(t *testing.T) {
	obs := newObserver(nil)
	f := obs.rawFields(errors.New("connection refused"))
	if f.Status != nil || f.OK || f.Error == nil || *f.Error != "connection refused" {
		t.Fatalf("fields = %+v", f)
	}
	f = obs.rawFields(nil)
	if f.Error == nil || *f.Error != "no response was received" {
		t.Fatalf("nil error fallback = %+v", f)
	}
}

func TestObserverLibFields(t *testing.T) {
	obs := newObserver(nil)

	// A refusal: the status comes from underneath, the body from the library.
	obs.last = &observation{status: 403, raw: []byte(`{"error":"forbidden"}`)}
	apiErr := &client.APIError{StatusCode: 403, Body: map[string]any{"error": "forbidden"}}
	f := obs.libFields(nil, apiErr)
	if f.Status == nil || *f.Status != 403 || f.OK || f.Error != nil {
		t.Fatalf("403 fields = %+v", f)
	}
	body, ok := f.Body.(map[string]any)
	if !ok || body["error"] != "forbidden" {
		t.Fatalf("body should be the library's: %#v", f.Body)
	}

	// A success: ok comes from the library's nil error.
	obs.last = &observation{status: 200, raw: []byte(`{"status":"ok"}`)}
	f = obs.libFields(map[string]any{"status": "ok"}, nil)
	if !f.OK || *f.Status != 200 {
		t.Fatalf("200 fields = %+v", f)
	}

	// No response at all.
	obs.reset()
	f = obs.libFields(nil, errors.New("boom"))
	if f.Status != nil || f.Error == nil || *f.Error != "boom" {
		t.Fatalf("transport fields = %+v", f)
	}
}

func TestLibraryBody(t *testing.T) {
	// A body the library could not parse collapses to the protocol placeholder
	// rather than the Go client's {"raw": text} shape.
	got := libraryBody([]byte(`<!doctype html>`), map[string]any{"raw": "<!doctype html>"}, nil)
	if got != NonJSONBody {
		t.Fatalf("non-json body = %#v, want %q", got, NonJSONBody)
	}
	if got := libraryBody(nil, nil, nil); got != NonJSONBody {
		t.Fatalf("empty body = %#v, want %q", got, NonJSONBody)
	}
	if got := libraryBody([]byte(`[1]`), []any{1}, nil); len(got.([]any)) != 1 {
		t.Fatalf("success body = %#v", got)
	}
	// A non-APIError failure cannot happen through HijackClient, but must not
	// invent a body if it ever does.
	if got := libraryBody([]byte(`{}`), nil, errors.New("other")); got != nil {
		t.Fatalf("unknown error body = %#v, want nil", got)
	}
}

func TestErrMessage(t *testing.T) {
	if got := errMessage(errors.New("x")); got != "x" {
		t.Fatalf("errMessage = %q", got)
	}
	if got := errMessage(nil); got != "no response was received" {
		t.Fatalf("errMessage(nil) = %q", got)
	}
}
