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
