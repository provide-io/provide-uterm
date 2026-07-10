//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"regexp"
)

// idPattern is the `^[\w\-]+$` path-param constraint FastAPI applies to
// session/worker/profile ids.
var idPattern = regexp.MustCompile(`^[\w\-]+$`)

// hijackIDPattern is the `^[0-9a-f\-]{1,64}$` constraint on hijack ids.
var hijackIDPattern = regexp.MustCompile(`^[0-9a-f\-]{1,64}$`)

// validID reports whether v satisfies the standard id path constraint.
func validID(v string) bool { return idPattern.MatchString(v) }

// write422PathParam writes the FastAPI list-shaped 422 for a path param that
// violated its pattern constraint.
func write422PathParam(w http.ResponseWriter, name string) {
	writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
		"detail": []map[string]any{{
			"loc":  []string{"path", name},
			"msg":  "String should match pattern '^[\\w\\-]+$'",
			"type": "string_pattern_mismatch",
		}},
	})
}

// requireID validates a path id, writing a 422 and returning false on mismatch.
func requireID(w http.ResponseWriter, name, value string) bool {
	if !validID(value) {
		write422PathParam(w, name)
		return false
	}
	return true
}
