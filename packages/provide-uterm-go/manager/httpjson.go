//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// getLogger returns a namespaced logger, matching the Python get_logger(name).
func getLogger(name string) *slog.Logger {
	return ptel.GetLogger(context.Background(), name)
}

// writeJSON serializes v as JSON with the given status code, matching the
// FastAPI JSONResponse default (application/json, no HTML escaping).
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

// jsonError writes the {"error": msg} envelope used by the manager routes'
// JSONResponse({"error": ...}, status_code=...).
func jsonError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg})
}

// detailError writes the {"detail": msg} envelope. This is NOT the manager's
// route-level shape — the routes raise {"error": ...} via jsonError. It is the
// FastAPI default that the reference app emits for the refusals it answers
// before any route runs, and it exists only for routeFallback. Verified
// against the reference app (plain FastAPI, no custom exception handlers):
//
//	GET  /not-a-thing -> 404 {"detail":"Not Found"}
//	POST /health      -> 405 {"detail":"Method Not Allowed"}
//	GET  /agent/nope/status -> 404 {"error":"Agent nope not found"}
//
// The two envelopes are deliberately distinct; do not collapse them.
func detailError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"detail": msg})
}

// routeFallback wraps the ServeMux so the two refusals the mux answers on its
// own leave through detailError instead of net/http's plain-text defaults
// ("404 page not found\n" / "Method Not Allowed\n").
//
// The 404-vs-405 decision (and the Allow header a 405 must carry) stays with
// net/http: when Handler reports no matching pattern it hands back the very
// handler it would have run, so we run that handler against a header-only
// recorder and re-emit its verdict in the JSON envelope. That keeps one
// implementation of the routing rules rather than a second, drifting copy.
//
// A matched request is dispatched through mux.ServeHTTP, not through the
// handler Handler returned: only ServeHTTP populates the {wildcard} path values
// that r.PathValue reads.
func routeFallback(mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h, pattern := mux.Handler(r)
		if pattern != "" {
			mux.ServeHTTP(w, r)
			return
		}
		rec := &headerOnlyRecorder{header: http.Header{}, status: http.StatusNotFound}
		h.ServeHTTP(rec, r)
		if allow := rec.header.Get("Allow"); allow != "" {
			w.Header().Set("Allow", allow)
		}
		detailError(w, rec.status, http.StatusText(rec.status))
	})
}

// headerOnlyRecorder is an http.ResponseWriter that captures the status and
// headers of net/http's default not-found / method-not-allowed handlers and
// discards their plain-text body.
type headerOnlyRecorder struct {
	header http.Header
	status int
	wrote  bool
}

func (rec *headerOnlyRecorder) Header() http.Header { return rec.header }

func (rec *headerOnlyRecorder) WriteHeader(code int) {
	if !rec.wrote {
		rec.status = code
		rec.wrote = true
	}
}

func (rec *headerOnlyRecorder) Write(b []byte) (int, error) { return len(b), nil }

// decodeJSONMap decodes the request body into a map. A missing/empty body
// yields (nil-safe empty map, true); a malformed body yields (nil, false).
func decodeJSONMap(r *http.Request) (map[string]any, bool) {
	if r.Body == nil {
		return map[string]any{}, true
	}
	dec := json.NewDecoder(r.Body)
	var m map[string]any
	if err := dec.Decode(&m); err != nil {
		if err.Error() == "EOF" {
			return map[string]any{}, true
		}
		return nil, false
	}
	if m == nil {
		m = map[string]any{}
	}
	return m, true
}
