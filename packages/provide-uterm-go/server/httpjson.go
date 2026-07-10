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
