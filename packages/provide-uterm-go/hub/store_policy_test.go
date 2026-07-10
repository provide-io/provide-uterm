//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

// wsWithPrincipal is a browser conn carrying a pre-resolved principal.
type wsWithPrincipal struct{ principal any }

func (w *wsWithPrincipal) UtermPrincipal() any { return w.principal }

type fakeIdP struct {
	principal any
	captured  []BrowserConn
}

func (p *fakeIdP) ResolvePrincipal(_ context.Context, ws BrowserConn) (any, error) {
	p.captured = append(p.captured, ws)
	return p.principal, nil
}

func roleP(role *string) string {
	if role == nil {
		return "<nil>"
	}
	return *role
}

func TestResolveRoleNoResolver(t *testing.T) {
	f := makeStore(StateStoreConfig{})
	role, err := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	mustTrue(t, err == nil && role == "viewer", "default viewer")
}

func TestResolveRoleSyncValidPassthrough(t *testing.T) {
	for _, want := range []string{"viewer", "operator", "admin"} {
		want := want
		f := makeStore(StateStoreConfig{ResolveBrowserRole: func(context.Context, BrowserConn, string) (string, bool, error) {
			return want, true, nil
		}})
		role, _ := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
		mustEqual(t, role, want, "passthrough")
	}
}

func TestResolveRolePassesArgs(t *testing.T) {
	var gotWS BrowserConn
	var gotWID string
	ws := newBrowser("ws")
	f := makeStore(StateStoreConfig{ResolveBrowserRole: func(_ context.Context, w BrowserConn, wid string) (string, bool, error) {
		gotWS = w
		gotWID = wid
		return "operator", true, nil
	}})
	_, _ = f.store.ResolveRoleForBrowser(context.Background(), ws, "w")
	mustTrue(t, gotWS == ws, "ws passed")
	mustEqual(t, gotWID, "w", "worker id passed")
}

func TestResolveRoleInvalidFallsBackAndWarns(t *testing.T) {
	f := makeStore(StateStoreConfig{ResolveBrowserRole: func(context.Context, BrowserConn, string) (string, bool, error) {
		return "root", true, nil
	}})
	role, _ := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	mustEqual(t, role, "viewer", "invalid -> viewer")
	mustTrue(t, logContains(f.logbuf(), "resolve_browser_role_invalid"), "warns")
}

func TestResolveRoleNoneResultNoWarning(t *testing.T) {
	f := makeStore(StateStoreConfig{ResolveBrowserRole: func(context.Context, BrowserConn, string) (string, bool, error) {
		return "", false, nil // None
	}})
	role, _ := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	mustEqual(t, role, "viewer", "none -> viewer")
	mustFalse(t, logContains(f.logbuf(), "resolve_browser_role_invalid"), "no invalid warning")
}

func TestResolveRoleTimeout(t *testing.T) {
	f := makeStore(StateStoreConfig{
		RoleResolveTimeout: 10 * time.Millisecond,
		ResolveBrowserRole: func(ctx context.Context, _ BrowserConn, _ string) (string, bool, error) {
			<-ctx.Done()
			return "", false, ctx.Err()
		},
	})
	var metrics []string
	f.store.onMetric = func(name string, _ int) { metrics = append(metrics, name) }
	_, err := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	var bre *BrowserRoleResolutionError
	mustTrue(t, asError(err, &bre) && bre.WorkerID == "w", "timeout -> resolution error naming worker")
	mustTrue(t, contains(metrics, "browser_role_resolution_timeout"), "metric emitted")
	mustTrue(t, logContains(f.logbuf(), "resolve_browser_role_timeout"), "timeout logged")
}

func TestResolveRoleGenericExceptionWrapped(t *testing.T) {
	f := makeStore(StateStoreConfig{ResolveBrowserRole: func(context.Context, BrowserConn, string) (string, bool, error) {
		return "", false, errString("nope")
	}})
	_, err := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	var bre *BrowserRoleResolutionError
	mustTrue(t, asError(err, &bre) && bre.WorkerID == "w", "wrapped in resolution error")
	mustTrue(t, logContains(f.logbuf(), "resolve_browser_role_failed"), "failure logged")
}

func TestResolveRoleWebSocketReraised(t *testing.T) {
	f := makeStore(StateStoreConfig{ResolveBrowserRole: func(context.Context, BrowserConn, string) (string, bool, error) {
		return "", false, &WebSocketRejection{Code: 1008, Reason: "denied"}
	}})
	_, err := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	var wsRej *WebSocketRejection
	mustTrue(t, asError(err, &wsRej), "websocket rejection re-raised as-is")
}

func TestResolveRoleResolutionErrorReraised(t *testing.T) {
	orig := &BrowserRoleResolutionError{WorkerID: "orig"}
	f := makeStore(StateStoreConfig{ResolveBrowserRole: func(context.Context, BrowserConn, string) (string, bool, error) {
		return "", false, orig
	}})
	_, err := f.store.ResolveRoleForBrowser(context.Background(), newBrowser("x"), "w")
	mustTrue(t, err == orig, "resolution error propagates unchanged")
}

func TestMapRolesDelegate(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{SubjectID: "u", Roles: set("admin", "ops")})),
		[]string{"admin", "ops"}, "delegate uses roles")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{SubjectID: "u", Roles: set()})),
		[]string{"viewer"}, "empty roles -> viewer")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{SubjectID: "u"})),
		[]string{"viewer"}, "missing roles -> viewer")
}

func TestMapRolesClaims(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: false})
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{Claims: map[string]any{"admin": true}})), []string{"admin"}, "admin claim")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{Claims: map[string]any{"is_admin": true}})), []string{"admin"}, "is_admin alias")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{Claims: map[string]any{"operator": true}})), []string{"operator"}, "operator claim")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{Claims: map[string]any{"admin": true, "operator": true}})), []string{"admin"}, "admin beats operator")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{Claims: map[string]any{}})), []string{"viewer"}, "no claims -> viewer")
	mustDeepEqual(t, sortedKeys(f.store.mapRoles(&Principal{})), []string{"viewer"}, "nil claims -> viewer")
}

func TestPrepareContextNoPrincipalUsesBrowserRole(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	ws := &wsWithPrincipal{principal: nil}
	st := NewWorkerTermState()
	st.Browsers = map[BrowserConn]string{ws: "operator"}
	f.registry.Put("w", st)
	ctx, err := f.store.PreparePolicyContext(context.Background(), ws, "w", strp("send"))
	mustTrue(t, err == nil, "no error")
	mustEqual(t, ctx.WorkerID, "w", "worker id")
	mustEqual(t, roleP(ctx.Role), "operator", "browser role")
	mustEqual(t, ctx.ClientID, "anonymous", "client id")
	mustEqual(t, roleP(ctx.Action), "send", "action")
	mustDeepEqual(t, ctx.Metadata, map[string]any{}, "empty metadata")
}

func TestPrepareContextMissingWorkerRoleNil(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	ctx, _ := f.store.PreparePolicyContext(context.Background(), &wsWithPrincipal{}, "ghost", nil)
	mustTrue(t, ctx.Role == nil, "role nil for missing worker")
}

func TestPrepareContextWsWithoutCarrierClean(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	ctx, _ := f.store.PreparePolicyContext(context.Background(), newBrowser("plain"), "ghost", nil)
	mustTrue(t, ctx.Role == nil, "role nil")
	mustEqual(t, ctx.ClientID, "anonymous", "anonymous")
	mustDeepEqual(t, ctx.Metadata, map[string]any{}, "empty metadata")
}

func TestPrepareContextPrincipalAdminOverrides(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	ws := &wsWithPrincipal{principal: &Principal{SubjectID: "u-1", Roles: set("admin", "viewer")}}
	st := NewWorkerTermState()
	st.Browsers = map[BrowserConn]string{ws: "viewer"}
	f.registry.Put("w", st)
	ctx, _ := f.store.PreparePolicyContext(context.Background(), ws, "w", nil)
	mustEqual(t, roleP(ctx.Role), "admin", "admin overrides browser viewer")
	mustEqual(t, ctx.ClientID, "u-1", "subject id")
	mustDeepEqual(t, ctx.Metadata, map[string]any{
		"principal": map[string]any{"subject_id": "u-1", "roles": []string{"admin", "viewer"}},
	}, "projected metadata")
}

func TestPrepareContextPrincipalOperatorAndViewer(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	op := &wsWithPrincipal{principal: &Principal{SubjectID: "u", Roles: set("operator")}}
	ctx, _ := f.store.PreparePolicyContext(context.Background(), op, "w", nil)
	mustEqual(t, roleP(ctx.Role), "operator", "operator")

	vw := &wsWithPrincipal{principal: &Principal{SubjectID: "u", Roles: set("viewer")}}
	ctx, _ = f.store.PreparePolicyContext(context.Background(), vw, "w", nil)
	mustEqual(t, roleP(ctx.Role), "viewer", "viewer else branch")
}

func TestPrepareContextStringPrincipal(t *testing.T) {
	f := makeStore(StateStoreConfig{DelegateRoles: true})
	ws := &wsWithPrincipal{principal: "anon-token"}
	st := NewWorkerTermState()
	st.Browsers = map[BrowserConn]string{ws: "operator"}
	f.registry.Put("w", st)
	ctx, _ := f.store.PreparePolicyContext(context.Background(), ws, "w", nil)
	mustEqual(t, roleP(ctx.Role), "operator", "string principal -> no override")
	mustEqual(t, ctx.ClientID, "anon-token", "client id is the string")
	mustDeepEqual(t, ctx.Metadata, map[string]any{"principal": "anon-token"}, "stringified metadata")
}

func TestPrepareContextUsesIdentityProvider(t *testing.T) {
	idp := &fakeIdP{principal: &Principal{SubjectID: "idp-user", Roles: set("admin")}}
	f := makeStore(StateStoreConfig{DelegateRoles: true, IdentityProvider: idp})
	ws := &wsWithPrincipal{principal: &Principal{SubjectID: "ignored", Roles: set("viewer")}}
	ctx, _ := f.store.PreparePolicyContext(context.Background(), ws, "w", nil)
	mustEqual(t, len(idp.captured), 1, "idp consulted")
	mustTrue(t, idp.captured[0] == ws, "idp got ws")
	mustEqual(t, roleP(ctx.Role), "admin", "idp role")
	mustEqual(t, ctx.ClientID, "idp-user", "idp client id")
}

func TestPrepareContextIdentityProviderError(t *testing.T) {
	f := makeStore(StateStoreConfig{IdentityProvider: errIdP{}})
	_, err := f.store.PreparePolicyContext(context.Background(), newBrowser("x"), "w", nil)
	mustTrue(t, err != nil, "idp error propagates")
}

func TestIsTruthyVariants(t *testing.T) {
	mustFalse(t, isTruthy(nil), "nil")
	mustFalse(t, isTruthy(false), "false")
	mustTrue(t, isTruthy(true), "true")
	mustFalse(t, isTruthy(""), "empty string")
	mustTrue(t, isTruthy("x"), "string")
	mustFalse(t, isTruthy(0), "zero int")
	mustTrue(t, isTruthy(1), "int")
	mustFalse(t, isTruthy(int64(0)), "zero int64")
	mustTrue(t, isTruthy(int64(2)), "int64")
	mustFalse(t, isTruthy(0.0), "zero float")
	mustTrue(t, isTruthy(1.5), "float")
	mustTrue(t, isTruthy([]int{1}), "other truthy")
}

func TestPrincipalTruthyVariants(t *testing.T) {
	mustFalse(t, principalTruthy(nil), "nil")
	mustFalse(t, principalTruthy(""), "empty string")
	mustTrue(t, principalTruthy("x"), "string")
	mustTrue(t, principalTruthy(&Principal{}), "principal")
	mustFalse(t, principalTruthy((*Principal)(nil)), "typed nil principal")
	mustTrue(t, principalTruthy(123), "other")
}

// --- small test helpers ---

type errString string

func (e errString) Error() string { return string(e) }

type errIdP struct{}

func (errIdP) ResolvePrincipal(context.Context, BrowserConn) (any, error) {
	return nil, errString("idp down")
}

func TestErrorTypeStrings(t *testing.T) {
	bre := &BrowserRoleResolutionError{WorkerID: "w1"}
	mustTrue(t, strings.Contains(bre.Error(), "w1"), "resolution error names worker")
	ws := &WebSocketRejection{Code: 1008, Reason: "denied"}
	mustTrue(t, strings.Contains(ws.Error(), "denied"), "websocket rejection message")
}

func set(items ...string) map[string]bool {
	m := map[string]bool{}
	for _, i := range items {
		m[i] = true
	}
	return m
}

func sortedKeys(m map[string]bool) []string { return sortedRoles(m) }

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func asError[T error](err error, target *T) bool {
	return errors.As(err, target)
}
