//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import "fmt"

// APIError is returned by HijackClient methods for every non-success outcome.
//
// It preserves the information the Python client folds into its
// “(ok, body)“ return convention: the decoded response Body is always
// available (even on failure), StatusCode carries the HTTP status (0 for a
// transport-level failure), and the IsXxx predicates distinguish the
// meaningful status codes the server emits (400/403/404/409/429).
type APIError struct {
	// StatusCode is the HTTP status of the response. It is 0 when the request
	// never produced an HTTP response (Transport == true).
	StatusCode int
	// Body is the decoded JSON body (map, slice, or {"raw": text} fallback),
	// or {"error": <transport error>} for a transport failure.
	Body any
	// Message is the extracted "error" field, or a rendering of Body.
	Message string
	// Transport is true when the failure happened before an HTTP response was
	// received (connection refused, timeout, malformed URL, ...).
	Transport bool
}

// Error implements the error interface.
func (e *APIError) Error() string {
	if e.Transport {
		return fmt.Sprintf("hijack transport error: %s", e.Message)
	}
	return fmt.Sprintf("hijack request failed (status %d): %s", e.StatusCode, e.Message)
}

// IsRateLimited reports whether the server rejected the request with 429.
func (e *APIError) IsRateLimited() bool { return e.StatusCode == 429 }

// IsConflict reports whether the server responded 409 (e.g. already hijacked,
// no worker connected, prompt guard not satisfied, open-mode switch refused).
func (e *APIError) IsConflict() bool { return e.StatusCode == 409 }

// IsNotFound reports whether the server responded 404 (invalid or expired
// hijack session, unknown worker/session).
func (e *APIError) IsNotFound() bool { return e.StatusCode == 404 }

// IsForbidden reports whether the server responded 403 (not the lease owner,
// insufficient privileges).
func (e *APIError) IsForbidden() bool { return e.StatusCode == 403 }

// IsBadRequest reports whether the server responded 400 (empty/oversized keys).
func (e *APIError) IsBadRequest() bool { return e.StatusCode == 400 }
