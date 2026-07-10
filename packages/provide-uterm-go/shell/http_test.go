//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"
)

// errBody is a response body whose Read always errors.
type errBody struct{}

func (errBody) Read([]byte) (int, error) { return 0, errors.New("read boom") }
func (errBody) Close() error             { return nil }

// errTransport returns a response with a failing body.
type errTransport struct{}

func (errTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return &http.Response{StatusCode: 200, Body: errBody{}, Header: make(http.Header)}, nil
}

func TestDoHTTPReadError(t *testing.T) {
	client := &http.Client{Transport: errTransport{}}
	_, _, err := doHTTP(context.Background(), client, http.MethodGet, "http://x", nil, time.Second, 0, "")
	if err == nil {
		t.Fatal("expected read error")
	}
}

func TestDoHTTPRequestBuildError(t *testing.T) {
	// A method containing a space is not a valid HTTP token → build error.
	_, _, err := doHTTP(context.Background(), http.DefaultClient, "BAD METHOD", "http://x", nil, time.Second, 0, "")
	if err == nil {
		t.Fatal("expected request-build error")
	}
}
