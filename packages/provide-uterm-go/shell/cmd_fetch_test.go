//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// serveFetch starts a test server returning status/body and recording the last
// method and body; it returns a dispatcher wired to the server's client.
func serveFetch(t *testing.T, status int, body string, lastMethod, lastBody *string) (*CommandDispatcher, string) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if lastMethod != nil {
			*lastMethod = r.Method
		}
		if lastBody != nil {
			b, _ := io.ReadAll(r.Body)
			*lastBody = string(b)
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	d := newDispatcher(nil)
	d.client = srv.Client()
	return d, srv.URL
}

func TestFetchNoURL(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "fetch"); !strings.Contains(got, "usage: fetch") {
		t.Fatalf("= %q", got)
	}
}

func TestFetchStatuses(t *testing.T) {
	tests := []struct {
		status int
		body   string
		want   string
		color  string
	}{
		{200, "Hello world", "HTTP 200", Green},
		{404, "Not Found", "HTTP 404", Yellow},
		{500, "Server Error", "HTTP 500", Red},
	}
	for _, tt := range tests {
		d, url := serveFetch(t, tt.status, tt.body, nil, nil)
		got := dispatchText(t, d, "fetch "+url)
		if !strings.Contains(got, tt.want) || !strings.Contains(got, tt.body) || !strings.Contains(got, tt.color) {
			t.Fatalf("status %d → %q", tt.status, got)
		}
	}
}

func TestFetchBodyTruncated(t *testing.T) {
	d, url := serveFetch(t, 200, strings.Repeat("X", 900), nil, nil)
	if got := dispatchText(t, d, "fetch "+url); !strings.Contains(got, "…") {
		t.Fatalf("= %q", got)
	}
}

func TestFetchNotTruncated(t *testing.T) {
	d, url := serveFetch(t, 200, strings.Repeat("X", 10), nil, nil)
	if got := dispatchText(t, d, "fetch "+url); strings.Contains(got, "…") {
		t.Fatalf("unexpected truncation marker: %q", got)
	}
}

func TestFetchPost(t *testing.T) {
	var method, body string
	d, url := serveFetch(t, 201, "Created", &method, &body)
	got := dispatchText(t, d, "fetch -X POST "+url+` {"key": "val"}`)
	if !strings.Contains(got, "HTTP 201") || !strings.Contains(got, "Created") {
		t.Fatalf("= %q", got)
	}
	if method != "POST" {
		t.Fatalf("method = %q", method)
	}
	if body != `{"key": "val"}` {
		t.Fatalf("body = %q", body)
	}
}

func TestFetchMinusXNoMethod(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "fetch -X"); !strings.Contains(got, "usage: fetch") {
		t.Fatalf("= %q", got)
	}
}

func TestFetchMinusXNoURL(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "fetch -X POST"); !strings.Contains(got, "usage: fetch") {
		t.Fatalf("= %q", got)
	}
}

func TestFetchError(t *testing.T) {
	// Start then close a server so the request is refused.
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	client := srv.Client()
	srv.Close()
	d := newDispatcher(nil)
	d.client = client
	got := dispatchText(t, d, "fetch "+url)
	if !strings.Contains(got, "error:") {
		t.Fatalf("= %q", got)
	}
}

func TestFetchInvalidUTF8(t *testing.T) {
	d, url := serveFetch(t, 200, "\xff\xfe bad bytes", nil, nil)
	// Should not panic and should still render an HTTP line.
	if got := dispatchText(t, d, "fetch "+url); !strings.Contains(got, "HTTP 200") {
		t.Fatalf("= %q", got)
	}
}
