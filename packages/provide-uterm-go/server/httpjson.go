//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
)

// writeJSON serializes v as JSON with the given status code. It matches the
// FastAPI default: Content-Type application/json, no HTML escaping of the body
// beyond Go's standard encoder (which is byte-compatible for the shapes here).
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	// A write/encode failure at this point means the client hung up; there is
	// nothing more the handler can do, so the error is intentionally dropped.
	_ = enc.Encode(v)
}

// detailError writes the FastAPI hand-raised error envelope {"detail": msg}
// with the given status. This is the shape every HTTPException(detail=str)
// serializes to in the Python server.
func detailError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"detail": msg})
}

// routeFallback wraps the ServeMux so the two refusals the mux answers on its
// own — a path in no route table, and a known path with an unregistered method
// — leave through detailError like every hand-raised refusal, instead of
// net/http's plain-text defaults ("404 page not found\n" / "Method Not
// Allowed\n"). The reference (FastAPI) server answers {"detail": "Not Found"}
// and {"detail": "Method Not Allowed"}; pinned by conformance/live scenario
// 003_error_shapes.
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

// bridgeError writes the bridge-route error envelope {"error": msg}. The
// bridge REST hijack routes use this shape (JSONResponse({"error": ...})),
// distinct from the {"detail": ...} shape of the /api routes.
func bridgeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg})
}

// decodeJSONBody decodes the request body into a map. A missing/empty body
// yields an empty map (ok=true) so optional-body routes behave like FastAPI's
// `request: Model | None = None`. A malformed body yields ok=false.
func decodeJSONBody(r *http.Request) (map[string]any, bool) {
	if r.Body == nil {
		return map[string]any{}, true
	}
	dec := json.NewDecoder(r.Body)
	var m map[string]any
	if err := dec.Decode(&m); err != nil {
		// EOF (empty body) is treated as an empty object, matching the
		// optional-body Pydantic default; any other error is a real parse
		// failure.
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
