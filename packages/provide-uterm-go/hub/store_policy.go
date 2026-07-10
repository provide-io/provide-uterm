//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"fmt"
	"sort"
)

// Principal is an authenticated subject. Port of the fields StateStore reads
// off provide.uterm.server.bridge.identity.Principal.
type Principal struct {
	SubjectID string
	Roles     map[string]bool
	Claims    map[string]any
}

// IdentityProvider resolves a principal for a browser connection. Port of the
// resolve_principal surface the store calls.
type IdentityProvider interface {
	ResolvePrincipal(ctx context.Context, ws BrowserConn) (any, error)
}

// RoleResolver resolves a browser role. ok is false for a "None" result (the
// Python resolver returning None). Port of the resolve_browser_role callback.
type RoleResolver func(ctx context.Context, ws BrowserConn, workerID string) (role string, ok bool, err error)

// principalCarrier is implemented by a browser conn that carries a
// pre-resolved principal (the Python ws.state.uterm_principal path).
type principalCarrier interface {
	UtermPrincipal() any
}

// BrowserRoleResolutionError is returned when a browser-role resolver fails and
// the WS should be rejected. Port of core_helpers.BrowserRoleResolutionError.
type BrowserRoleResolutionError struct {
	WorkerID string
}

func (e *BrowserRoleResolutionError) Error() string {
	return fmt.Sprintf("browser role resolution failed for worker %q", e.WorkerID)
}

// WebSocketRejection is the Go analogue of FastAPI's WebSocketException: a
// resolver returning it is re-raised as-is (not wrapped).
type WebSocketRejection struct {
	Code   int
	Reason string
}

func (e *WebSocketRejection) Error() string {
	return fmt.Sprintf("websocket rejected (code=%d): %s", e.Code, e.Reason)
}

// errRoleResolveTimeout marks an awaitable resolver exceeding its deadline.
var errRoleResolveTimeout = errors.New("role resolver timed out")

// PolicyContext is the context handed to a policy gate. Port of ext.PolicyContext.
type PolicyContext struct {
	WorkerID string
	ClientID string
	Role     *string
	Action   *string
	Metadata map[string]any
}

// validRoles is the accepted role set (mirrors {"viewer","operator","admin"}).
var validRoles = map[string]bool{"viewer": true, "operator": true, "admin": true}

// ResolveRoleForBrowser resolves a browser role via the configured resolver,
// defaulting to "viewer". Port of resolve_role_for_browser.
func (s *StateStore) ResolveRoleForBrowser(ctx context.Context, ws BrowserConn, workerID string) (string, error) {
	if s.resolveBrowserRole == nil {
		return "viewer", nil
	}
	role, ok, err := s.runResolver(ctx, ws, workerID)
	if err != nil {
		if errors.Is(err, errRoleResolveTimeout) {
			s.logger.Warn("resolve_browser_role_timeout", "worker_id", workerID)
			s.Metric("browser_role_resolution_timeout", 1)
			return "", &BrowserRoleResolutionError{WorkerID: workerID}
		}
		var wsRej *WebSocketRejection
		var bre *BrowserRoleResolutionError
		if errors.As(err, &wsRej) || errors.As(err, &bre) {
			return "", err
		}
		s.logger.Warn("resolve_browser_role_failed", "worker_id", workerID, "error", err)
		return "", &BrowserRoleResolutionError{WorkerID: workerID}
	}
	if ok && validRoles[role] {
		return role, nil
	}
	if ok {
		s.logger.Warn("resolve_browser_role_invalid", "worker_id", workerID, "role", role)
	}
	return "viewer", nil
}

type resolverResult struct {
	role string
	ok   bool
	err  error
}

// runResolver runs the resolver under a deadline. A resolver that exceeds the
// deadline yields errRoleResolveTimeout (the Python awaitable-timeout branch).
func (s *StateStore) runResolver(ctx context.Context, ws BrowserConn, workerID string) (string, bool, error) {
	rctx, cancel := context.WithTimeout(ctx, s.roleResolveTimeout)
	defer cancel()
	ch := make(chan resolverResult, 1)
	go func() {
		role, ok, err := s.resolveBrowserRole(rctx, ws, workerID)
		ch <- resolverResult{role, ok, err}
	}()
	select {
	case r := <-ch:
		return r.role, r.ok, r.err
	case <-rctx.Done():
		return "", false, errRoleResolveTimeout
	}
}

// PreparePolicyContext builds a [PolicyContext] for a browser WS + worker. Port
// of prepare_policy_context. action may be nil.
func (s *StateStore) PreparePolicyContext(
	ctx context.Context, ws BrowserConn, workerID string, action *string,
) (PolicyContext, error) {
	s.lock.Lock()
	var role *string
	if st := s.registry.Get(workerID); st != nil {
		if r, ok := st.Browsers[ws]; ok {
			role = strp(r)
		}
	}
	s.lock.Unlock()

	var principal any
	if s.identityProvider != nil {
		p, err := s.identityProvider.ResolvePrincipal(ctx, ws)
		if err != nil {
			return PolicyContext{}, err
		}
		principal = p
	} else {
		principal = principalFromWS(ws)
	}

	pr, isPrincipal := principalObj(principal)
	if principalTruthy(principal) && isPrincipal {
		roles := s.mapRoles(pr)
		switch {
		case roles["admin"]:
			role = strp("admin")
		case roles["operator"]:
			role = strp("operator")
		default:
			role = strp("viewer")
		}
	}

	clientID := "anonymous"
	if principalTruthy(principal) {
		if isPrincipal {
			clientID = pr.SubjectID
		} else if str, ok := principal.(string); ok {
			clientID = str
		}
	}

	var metadata map[string]any
	switch {
	case principalTruthy(principal) && isPrincipal:
		metadata = map[string]any{
			"principal": map[string]any{
				"subject_id": pr.SubjectID,
				"roles":      sortedRoles(pr.Roles),
			},
		}
	case principalTruthy(principal):
		metadata = map[string]any{"principal": fmt.Sprint(principal)}
	default:
		metadata = map[string]any{}
	}

	return PolicyContext{
		WorkerID: workerID,
		ClientID: clientID,
		Role:     role,
		Action:   action,
		Metadata: metadata,
	}, nil
}

// mapRoles maps a principal to a role set. Port of _map_roles.
func (s *StateStore) mapRoles(pr *Principal) map[string]bool {
	if s.delegateRoles {
		if len(pr.Roles) > 0 {
			out := make(map[string]bool, len(pr.Roles))
			for r := range pr.Roles {
				out[r] = true
			}
			return out
		}
		return map[string]bool{"viewer": true}
	}
	mapped := map[string]bool{}
	claims := pr.Claims
	switch {
	case isTruthy(claims["admin"]) || isTruthy(claims["is_admin"]):
		mapped["admin"] = true
	case isTruthy(claims["operator"]):
		mapped["operator"] = true
	}
	if len(mapped) == 0 {
		mapped["viewer"] = true
	}
	return mapped
}

// principalFromWS extracts ws.state.uterm_principal, or nil.
func principalFromWS(ws BrowserConn) any {
	if pc, ok := ws.(principalCarrier); ok {
		return pc.UtermPrincipal()
	}
	return nil
}

// principalObj returns the principal as *Principal (non-nil) plus ok.
func principalObj(principal any) (*Principal, bool) {
	pr, ok := principal.(*Principal)
	if ok && pr != nil {
		return pr, true
	}
	return nil, false
}

// principalTruthy applies Python truthiness: nil and "" are falsy; a non-nil
// *Principal or a non-empty string is truthy.
func principalTruthy(p any) bool {
	if p == nil {
		return false
	}
	if s, ok := p.(string); ok {
		return s != ""
	}
	if pr, ok := p.(*Principal); ok {
		return pr != nil
	}
	return true
}

// isTruthy applies Python truthiness to a claim value.
func isTruthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	default:
		return true
	}
}

// sortedRoles returns the role set as a sorted slice.
func sortedRoles(roles map[string]bool) []string {
	out := make([]string, 0, len(roles))
	for r := range roles {
		out = append(out, r)
	}
	sort.Strings(out)
	return out
}
