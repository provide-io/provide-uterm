//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"context"
	"encoding/base64"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// denylistedHeaders are headers an operator-controlled browser MUST NOT inject
// into a forwarded request. They split into three categories — hop-by-hop
// (RFC 7230 §6.1), length/framing (request-smuggling vector), and
// identity/authority (impersonation vector). Mirrors intercept.py's
// _DENYLISTED_HEADERS. Names are lowercased for case-insensitive matching.
var denylistedHeaders = map[string]struct{}{
	// Hop-by-hop
	"connection":          {},
	"keep-alive":          {},
	"proxy-authenticate":  {},
	"proxy-authorization": {},
	"te":                  {},
	"trailer":             {},
	"transfer-encoding":   {},
	"upgrade":             {},
	// Framing / smuggling
	"content-length": {},
	// Identity / authority
	"host":              {},
	"authorization":     {},
	"cookie":            {},
	"forwarded":         {},
	"x-forwarded-for":   {},
	"x-forwarded-host":  {},
	"x-forwarded-proto": {},
	"x-real-ip":         {},
}

// InterceptDecision is the browser's decision for a paused request.
type InterceptDecision struct {
	Action  string            // "forward" | "drop" | "modify"
	Headers map[string]string // non-nil only for a sanitized "modify"
	Body    []byte            // non-nil only when "modify" supplied a new body
}

func defaultDecision(action string) InterceptDecision {
	return InterceptDecision{Action: action}
}

// SanitizeHeaders strips denylisted headers from an operator-supplied header
// map. It returns a new map; the input is not mutated. Dropped names are
// returned (sorted) so the caller can log why a modify didn't take effect
// verbatim. Mirrors intercept.py's _sanitize_headers.
func SanitizeHeaders(raw map[string]string) (cleaned map[string]string, dropped []string) {
	cleaned = make(map[string]string, len(raw))
	for k, v := range raw {
		if _, bad := denylistedHeaders[strings.ToLower(k)]; bad {
			dropped = append(dropped, k)
			continue
		}
		cleaned[k] = v
	}
	sort.Strings(dropped)
	return cleaned, dropped
}

// ParseActionMessage parses an http_action message from the browser into an
// InterceptDecision. Browser-supplied headers are sanitized against the
// denylist. An unknown action falls back to "forward". Invalid base64 bodies
// are ignored (body stays nil). Mirrors intercept.py's parse_action_message.
func ParseActionMessage(msg map[string]any) InterceptDecision {
	action := "forward"
	if a, ok := msg["action"].(string); ok {
		action = a
	}
	if action != "forward" && action != "drop" && action != "modify" {
		action = "forward"
	}
	d := InterceptDecision{Action: action}
	if action != "modify" {
		return d
	}
	if rawHeaders, ok := msg["headers"].(map[string]any); ok {
		src := make(map[string]string, len(rawHeaders))
		for k, v := range rawHeaders {
			src[k] = stringifyHeaderValue(v)
		}
		cleaned, _ := SanitizeHeaders(src)
		d.Headers = cleaned
	}
	if b64, ok := msg["body_b64"].(string); ok {
		if body, err := base64.StdEncoding.DecodeString(b64); err == nil {
			d.Body = body
		}
	}
	return d
}

// stringifyHeaderValue coerces a JSON header value to string like Python's
// str(v). JSON numbers arrive as float64; format an integral value without a
// trailing ".0".
func stringifyHeaderValue(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case float64:
		if t == float64(int64(t)) {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'g', -1, 64)
	case bool:
		if t {
			return "True"
		}
		return "False"
	default:
		return fmt.Sprintf("%v", t)
	}
}

// InterceptGate manages pending intercepted HTTP requests. It is the Go port of
// intercept.py's InterceptGate, using buffered channels in place of asyncio
// Futures. All fields and the pending map are guarded by mu; AwaitDecision does
// not hold mu while blocking.
type InterceptGate struct {
	mu             sync.Mutex
	enabled        bool
	inspectEnabled bool
	timeoutS       float64
	timeoutAction  string
	pending        map[string]chan InterceptDecision
}

// NewInterceptGate builds a gate. timeoutS is clamped to >= 1.0 and
// timeoutAction is coerced to "forward" unless it is exactly "drop", matching
// the Python constructor.
func NewInterceptGate(timeoutS float64, timeoutAction string) *InterceptGate {
	if timeoutS < 1.0 {
		timeoutS = 1.0
	}
	if timeoutAction != "forward" && timeoutAction != "drop" {
		timeoutAction = "forward"
	}
	return &InterceptGate{
		inspectEnabled: true,
		timeoutS:       timeoutS,
		timeoutAction:  timeoutAction,
		pending:        make(map[string]chan InterceptDecision),
	}
}

// TimeoutS returns the configured per-request wait (seconds).
func (g *InterceptGate) TimeoutS() float64 { return g.timeoutS }

// TimeoutAction returns the configured on-timeout action.
func (g *InterceptGate) TimeoutAction() string { return g.timeoutAction }

// Enabled reports whether interception (pause-before-forward) is on.
func (g *InterceptGate) Enabled() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.enabled
}

// InspectEnabled reports whether inspection (event emission) is on.
func (g *InterceptGate) InspectEnabled() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.inspectEnabled
}

// SetEnabled toggles interception.
func (g *InterceptGate) SetEnabled(v bool) {
	g.mu.Lock()
	g.enabled = v
	g.mu.Unlock()
}

// SetInspectEnabled toggles inspection.
func (g *InterceptGate) SetInspectEnabled(v bool) {
	g.mu.Lock()
	g.inspectEnabled = v
	g.mu.Unlock()
}

// PendingCount is the number of requests awaiting a browser decision.
func (g *InterceptGate) PendingCount() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return len(g.pending)
}

// AwaitDecision blocks until the browser resolves rid, the per-request timeout
// expires, or ctx is cancelled. On timeout/cancel it returns the configured
// timeout-action decision.
func (g *InterceptGate) AwaitDecision(ctx context.Context, rid string) InterceptDecision {
	ch := make(chan InterceptDecision, 1)
	g.mu.Lock()
	g.pending[rid] = ch
	g.mu.Unlock()
	defer func() {
		g.mu.Lock()
		delete(g.pending, rid)
		g.mu.Unlock()
	}()

	timer := time.NewTimer(time.Duration(g.timeoutS * float64(time.Second)))
	defer timer.Stop()
	select {
	case d := <-ch:
		return d
	case <-timer.C:
		return defaultDecision(g.timeoutAction)
	case <-ctx.Done():
		return defaultDecision(g.timeoutAction)
	}
}

// Resolve delivers a browser decision to a pending request. It returns true if
// the request was found (and had not already been resolved).
func (g *InterceptGate) Resolve(rid string, decision InterceptDecision) bool {
	g.mu.Lock()
	ch, ok := g.pending[rid]
	if ok {
		delete(g.pending, rid)
	}
	g.mu.Unlock()
	if !ok {
		return false
	}
	ch <- decision // buffered (cap 1) — never blocks
	return true
}

// CancelAll resolves every pending request with action and returns the count.
func (g *InterceptGate) CancelAll(action string) int {
	g.mu.Lock()
	defer g.mu.Unlock()
	n := 0
	for rid, ch := range g.pending {
		ch <- defaultDecision(action)
		delete(g.pending, rid)
		n++
	}
	return n
}
