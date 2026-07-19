//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// tenantHeaders returns auth headers for a principal with role + tenant.
func tenantHeaders(role, tenant string) map[string]string {
	return map[string]string{"X-Subject": "u1", "X-Role": role, "X-Tenant": tenant}
}

// gtJSON unmarshals a JSON response body into a generic map.
func gtJSON(t *testing.T, b []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("json: %v (%s)", err, string(b))
	}
	return m
}

func createRfbBody() string {
	return `{"protocol":"rfb","endpoint":"vm.local:5900","display_name":"Console","secret":"hunter2"}`
}

func TestGraphicalAccessControl(t *testing.T) {
	ts := newTestServer(t, nil)

	// Anonymous → 403 (has read cap via viewer role, but no tenant scope).
	if rec := ts.do("GET", "/api/graphical-targets", "", nil); rec.Code != http.StatusForbidden {
		t.Fatalf("anonymous list = %d", rec.Code)
	}
	// Viewer with a tenant can read.
	if rec := ts.do("GET", "/api/graphical-targets", "", tenantHeaders("viewer", "acme")); rec.Code != http.StatusOK {
		t.Fatalf("viewer list = %d", rec.Code)
	}
	// Viewer lacks manage → 403 on create.
	if rec := ts.do("POST", "/api/graphical-targets", createRfbBody(), tenantHeaders("viewer", "acme")); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer create = %d", rec.Code)
	}
	// Operator with no tenant → 403.
	if rec := ts.do("POST", "/api/graphical-targets", createRfbBody(), map[string]string{"X-Subject": "u", "X-Role": "operator"}); rec.Code != http.StatusForbidden {
		t.Fatalf("no-tenant create = %d", rec.Code)
	}
}

func TestGraphicalListPagination(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("viewer", "acme")

	rec := ts.do("GET", "/api/graphical-targets?limit=5&offset=0", "", h)
	if rec.Code != http.StatusOK {
		t.Fatalf("list = %d", rec.Code)
	}
	body := gtJSON(t, rec.Body.Bytes())
	if body["limit"].(float64) != 5 || body["offset"].(float64) != 0 || body["total"].(float64) != 0 {
		t.Fatalf("pagination fields wrong: %+v", body)
	}
	// Out-of-range limit/offset → 422 with a flat detail string.
	for _, q := range []string{"?limit=0", "?limit=999", "?limit=abc", "?offset=-1", "?offset=x"} {
		if rec := ts.do("GET", "/api/graphical-targets"+q, "", h); rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("query %q = %d", q, rec.Code)
		}
	}
}

func TestGraphicalCreateGetLifecycle(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("operator", "acme")

	rec := ts.do("POST", "/api/graphical-targets", createRfbBody(), h)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create = %d (%s)", rec.Code, rec.Body.String())
	}
	created := gtJSON(t, rec.Body.Bytes())
	id, _ := created["target_id"].(string)
	if id == "" {
		t.Fatalf("no target_id assigned")
	}
	if created["tenant_id"] != "acme" || created["is_system"] != false {
		t.Fatalf("create fields wrong: %+v", created)
	}
	// Secret is stripped from the response.
	if _, ok := created["secret"]; ok {
		t.Fatalf("secret leaked in create response")
	}
	// Endpoint normalized.
	if created["endpoint"] != "vm.local:5900" {
		t.Fatalf("endpoint = %v", created["endpoint"])
	}

	// GET the created target.
	rec = ts.do("GET", "/api/graphical-targets/"+id, "", h)
	if rec.Code != http.StatusOK {
		t.Fatalf("get = %d", rec.Code)
	}
	if _, ok := gtJSON(t, rec.Body.Bytes())["secret"]; ok {
		t.Fatalf("secret leaked in get response")
	}

	// GET missing → 404 with object envelope.
	rec = ts.do("GET", "/api/graphical-targets/nope", "", h)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("get missing = %d", rec.Code)
	}
	detail := gtJSON(t, rec.Body.Bytes())["detail"].(map[string]any)
	if detail["code"] != graphical.ErrNotFound {
		t.Fatalf("error code = %v", detail["code"])
	}
}

func TestGraphicalCreateValidation(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("operator", "acme")

	cases := []struct {
		name string
		body string
		code int
		errc string
	}{
		{"tenant_in_body", `{"tenant_id":"acme","endpoint":"vm:5900"}`, 422, graphical.ErrTenantManaged},
		{"target_id_in_body", `{"target_id":"gt-x","endpoint":"vm:5900"}`, 422, graphical.ErrInvalidPayload},
		{"unknown_key", `{"bogus":1}`, 422, graphical.ErrInvalidPayload},
		{"bad_endpoint", `{"endpoint":"noport"}`, 422, graphical.ErrInvalidPayload},
		{"wrong_type_width", `{"endpoint":"vm:5900","width":"abc"}`, 422, graphical.ErrInvalidPayload},
		{"missing_endpoint", `{"protocol":"rfb"}`, 422, graphical.ErrInvalidPayload},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := ts.do("POST", "/api/graphical-targets", tc.body, h)
			if rec.Code != tc.code {
				t.Fatalf("code = %d want %d (%s)", rec.Code, tc.code, rec.Body.String())
			}
			detail := gtJSON(t, rec.Body.Bytes())["detail"].(map[string]any)
			if detail["code"] != tc.errc {
				t.Fatalf("errc = %v want %v", detail["code"], tc.errc)
			}
		})
	}
}

func TestGraphicalUpdateAndDelete(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("operator", "acme")

	created := gtJSON(t, ts.do("POST", "/api/graphical-targets", createRfbBody(), h).Body.Bytes())
	id := created["target_id"].(string)

	// Update display_name.
	rec := ts.do("PUT", "/api/graphical-targets/"+id, `{"endpoint":"vm.local:5900","display_name":"Renamed"}`, h)
	if rec.Code != http.StatusOK {
		t.Fatalf("update = %d (%s)", rec.Code, rec.Body.String())
	}
	if gtJSON(t, rec.Body.Bytes())["display_name"] != "Renamed" {
		t.Fatalf("display_name not updated")
	}

	// tenant_id in update body → 422 tenant_managed.
	rec = ts.do("PUT", "/api/graphical-targets/"+id, `{"tenant_id":"acme","endpoint":"vm:5900"}`, h)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("update tenant body = %d", rec.Code)
	}

	// target_id mismatch → 409.
	rec = ts.do("PUT", "/api/graphical-targets/"+id, `{"target_id":"gt-other","endpoint":"vm:5900"}`, h)
	if rec.Code != http.StatusConflict {
		t.Fatalf("update mismatch = %d", rec.Code)
	}

	// Update missing → 404.
	rec = ts.do("PUT", "/api/graphical-targets/ghost", `{"endpoint":"vm:5900"}`, h)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("update missing = %d", rec.Code)
	}

	// Update with a bad body key → 422.
	rec = ts.do("PUT", "/api/graphical-targets/"+id, `{"bogus":1}`, h)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("update bad key = %d", rec.Code)
	}

	// Delete → 204.
	if rec := ts.do("DELETE", "/api/graphical-targets/"+id, "", h); rec.Code != http.StatusNoContent {
		t.Fatalf("delete = %d", rec.Code)
	}
	// Delete missing → 404.
	if rec := ts.do("DELETE", "/api/graphical-targets/"+id, "", h); rec.Code != http.StatusNotFound {
		t.Fatalf("delete missing = %d", rec.Code)
	}
}

func TestGraphicalTenantIsolationHTTP(t *testing.T) {
	ts := newTestServer(t, nil)
	acme := tenantHeaders("operator", "acme")
	beta := tenantHeaders("operator", "beta")

	created := gtJSON(t, ts.do("POST", "/api/graphical-targets", createRfbBody(), acme).Body.Bytes())
	id := created["target_id"].(string)

	// beta cannot see acme's target — 404 (no leak).
	if rec := ts.do("GET", "/api/graphical-targets/"+id, "", beta); rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant get = %d", rec.Code)
	}
	// beta's list is empty.
	list := gtJSON(t, ts.do("GET", "/api/graphical-targets", "", beta).Body.Bytes())
	if list["total"].(float64) != 0 {
		t.Fatalf("cross-tenant list leaked: %+v", list)
	}
	// beta cannot update or delete acme's target — 404.
	if rec := ts.do("PUT", "/api/graphical-targets/"+id, `{"endpoint":"vm:5900"}`, beta); rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant update = %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/graphical-targets/"+id, "", beta); rec.Code != http.StatusNotFound {
		t.Fatalf("cross-tenant delete = %d", rec.Code)
	}
}

func TestGraphicalStaticImmutableHTTP(t *testing.T) {
	static := graphical.NewInMemoryRegistry()
	seed := graphical.NewDefinition()
	seed.TargetID = "gt-seed"
	seed.TenantID = "acme"
	seed.Endpoint = strPtrLocal("vm.local:5900")
	if err := static.AddStatic(seed); err != nil {
		t.Fatalf("addstatic: %v", err)
	}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.GraphicalTargets = static
	})
	h := tenantHeaders("operator", "acme")

	// Visible via GET/List.
	if rec := ts.do("GET", "/api/graphical-targets/gt-seed", "", h); rec.Code != http.StatusOK {
		t.Fatalf("get static = %d", rec.Code)
	}
	// Update a static target → 409 immutable.
	rec := ts.do("PUT", "/api/graphical-targets/gt-seed", `{"endpoint":"vm.local:5900"}`, h)
	if rec.Code != http.StatusConflict {
		t.Fatalf("update static = %d", rec.Code)
	}
	if gtJSON(t, rec.Body.Bytes())["detail"].(map[string]any)["code"] != graphical.ErrImmutable {
		t.Fatalf("expected immutable code")
	}
	// Delete a static target → 409 immutable.
	if rec := ts.do("DELETE", "/api/graphical-targets/gt-seed", "", h); rec.Code != http.StatusConflict {
		t.Fatalf("delete static = %d", rec.Code)
	}
}

func strPtrLocal(s string) *string { return &s }

func TestSeedGraphicalTargets(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		{TargetID: "gt-a", TenantID: "acme", Protocol: "rfb", TargetAddress: "vm.local:5900", Enabled: true, Width: 800, Height: 600},
		{TargetID: "gt-mem", TenantID: "acme", Protocol: "memory", Enabled: true},
		{TargetID: "gt-off", Protocol: "rfb", TargetAddress: "vm:5900", Enabled: false},
	}
	reg, err := SeedGraphicalTargets(cfg)
	if err != nil {
		t.Fatalf("seed: %v", err)
	}
	scope, _ := graphical.ScopeForTenant("acme")
	list, _ := reg.List(scope)
	if len(list) != 2 {
		t.Fatalf("expected 2 seeded (disabled skipped), got %d", len(list))
	}
	// rfb endpoint normalized + dimensions carried; memory has no endpoint.
	for _, d := range list {
		if !d.IsStatic || !d.IsSystem {
			t.Fatalf("seeded target not static/system: %+v", d)
		}
	}
}

func TestSeedGraphicalTargetsErrors(t *testing.T) {
	// Unsupported protocol.
	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{{TargetID: "gt-x", Protocol: "vnc", TargetAddress: "vm:5900", Enabled: true}}
	if _, err := SeedGraphicalTargets(cfg); err == nil {
		t.Fatalf("expected error for bad protocol")
	}

	// rfb missing address.
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{{TargetID: "gt-y", Protocol: "rfb", Enabled: true}}
	if _, err := SeedGraphicalTargets(cfg); err == nil {
		t.Fatalf("expected error for missing address")
	}

	// rfb bad endpoint (unparseable).
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{{TargetID: "gt-z", Protocol: "rfb", TargetAddress: "noport", Enabled: true}}
	if _, err := SeedGraphicalTargets(cfg); err == nil {
		t.Fatalf("expected error for bad endpoint")
	}
}

func TestSeedGraphicalTargetDefaults(t *testing.T) {
	// A blank target_id gets a generated one; a blank name defaults to the id;
	// dimensions clamp. Verified via the produced definition.
	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		{Protocol: "memory", TenantID: "acme", Enabled: true, Width: 0, Height: 99999},
	}
	reg, err := SeedGraphicalTargets(cfg)
	if err != nil {
		t.Fatalf("seed: %v", err)
	}
	scope, _ := graphical.ScopeForTenant("acme")
	list, _ := reg.List(scope)
	if len(list) != 1 {
		t.Fatalf("want 1, got %d", len(list))
	}
	d := list[0]
	if d.TargetID == "" || d.DisplayName != d.TargetID {
		t.Fatalf("id/name defaults wrong: %+v", d)
	}
	if d.Width != 640 || d.Height != 8192 {
		t.Fatalf("dimension clamp wrong: w=%d h=%d", d.Width, d.Height)
	}
}

func TestGraphicalRouteErrorMapping(t *testing.T) {
	cases := []struct {
		err    error
		status int
		code   string
	}{
		{&graphical.Error{Code: graphical.CodeAlreadyExists}, http.StatusConflict, graphical.ErrAlreadyExists},
		{&graphical.Error{Code: graphical.CodeImmutable}, http.StatusConflict, graphical.ErrImmutable},
		{&graphical.Error{Code: graphical.CodeConflict}, http.StatusConflict, graphical.ErrConflict},
		{&graphical.Error{Code: graphical.CodeInvalid}, http.StatusUnprocessableEntity, graphical.ErrInvalidPayload},
		{&graphical.Error{Code: graphical.CodeNotFound}, http.StatusNotFound, graphical.ErrNotFound},
		{&graphical.Error{Code: graphical.CodeForbidden}, http.StatusNotFound, graphical.ErrNotFound},
		{&graphical.Error{Code: graphical.CodeClosed}, http.StatusServiceUnavailable, graphical.ErrUnavailable},
		{&graphical.Error{Code: graphical.CodeBackend}, http.StatusServiceUnavailable, graphical.ErrBackend},
		{errors.New("plain"), http.StatusServiceUnavailable, graphical.ErrBackend},
	}
	for _, tc := range cases {
		rec := httptest.NewRecorder()
		graphicalRouteError(rec, tc.err)
		if rec.Code != tc.status {
			t.Fatalf("err %v: status %d want %d", tc.err, rec.Code, tc.status)
		}
		detail := gtJSON(t, rec.Body.Bytes())["detail"].(map[string]any)
		if detail["code"] != tc.code {
			t.Fatalf("err %v: code %v want %v", tc.err, detail["code"], tc.code)
		}
	}
}

func TestParseGraphicalBodyTypeErrors(t *testing.T) {
	stringKeys := []string{"display_name", "target_id", "protocol", "endpoint", "secret",
		"ca_secret_ref", "client_cert_secret_ref", "client_key_secret_ref"}
	for _, k := range stringKeys {
		_, _, _, err := parseGraphicalTargetBody(map[string]any{k: 123})
		if err == nil {
			t.Fatalf("key %q non-string accepted", k)
		}
	}
	// int fields with a non-numeric string or non-number.
	for _, k := range []string{"width", "height"} {
		if _, _, _, err := parseGraphicalTargetBody(map[string]any{k: "abc"}); err == nil {
			t.Fatalf("key %q bad int accepted", k)
		}
		if _, _, _, err := parseGraphicalTargetBody(map[string]any{k: true}); err == nil {
			t.Fatalf("key %q bool int accepted", k)
		}
	}
	// A numeric string is accepted for int fields.
	tgt, _, _, err := parseGraphicalTargetBody(map[string]any{"width": "800", "endpoint": "vm:5900"})
	if err != nil || tgt.Width != 800 {
		t.Fatalf("numeric string width: %v %+v", err, tgt)
	}
	// null values fall back to defaults.
	tgt, _, _, err = parseGraphicalTargetBody(map[string]any{"endpoint": nil, "width": nil})
	if err != nil || tgt.Endpoint != nil || tgt.Width != 640 {
		t.Fatalf("null handling: %v %+v", err, tgt)
	}
}

func TestGraphicalClosedRegistry(t *testing.T) {
	closed := graphical.NewInMemoryRegistry()
	closed.Close()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.GraphicalTargets = closed
	})
	h := tenantHeaders("operator", "acme")
	// Every op maps CodeClosed → 503.
	for _, req := range []struct{ m, p, b string }{
		{"GET", "/api/graphical-targets", ""},
		{"GET", "/api/graphical-targets/x", ""},
		{"POST", "/api/graphical-targets", createRfbBody()},
		{"PUT", "/api/graphical-targets/x", `{"endpoint":"vm:5900"}`},
		{"DELETE", "/api/graphical-targets/x", ""},
	} {
		if rec := ts.do(req.m, req.p, req.b, h); rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("%s %s closed = %d", req.m, req.p, rec.Code)
		}
	}
}

func TestGraphicalMalformedBody(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("operator", "acme")
	if rec := ts.do("POST", "/api/graphical-targets", "{not json", h); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("create malformed = %d", rec.Code)
	}
	if rec := ts.do("PUT", "/api/graphical-targets/x", "{not json", h); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("update malformed = %d", rec.Code)
	}
}

func TestSeedGraphicalDuplicateIDAndEmptyProtocol(t *testing.T) {
	// Duplicate ids → AddStatic conflict surfaces as an error.
	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		{TargetID: "gt-dup", Protocol: "rfb", TargetAddress: "vm:5900", Enabled: true},
		{TargetID: "gt-dup", Protocol: "rfb", TargetAddress: "vm:5901", Enabled: true},
	}
	if _, err := SeedGraphicalTargets(cfg); err == nil {
		t.Fatalf("expected duplicate-id error")
	}
	// Empty protocol defaults to rfb.
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		{TargetID: "gt-empty", TenantID: "acme", Protocol: "", TargetAddress: "vm:5900", Enabled: true},
	}
	reg, err := SeedGraphicalTargets(cfg)
	if err != nil {
		t.Fatalf("empty protocol seed: %v", err)
	}
	scope, _ := graphical.ScopeForTenant("acme")
	list, _ := reg.List(scope)
	if len(list) != 1 || list[0].Protocol != "rfb" {
		t.Fatalf("empty protocol not defaulted: %+v", list)
	}
}

func TestGraphicalListOffsetBeyondTotal(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("operator", "acme")
	ts.do("POST", "/api/graphical-targets", createRfbBody(), h)
	// offset past the end yields an empty page (start clamped to total).
	rec := ts.do("GET", "/api/graphical-targets?offset=50", "", h)
	if rec.Code != http.StatusOK {
		t.Fatalf("list = %d", rec.Code)
	}
	body := gtJSON(t, rec.Body.Bytes())
	items := body["items"].([]any)
	if len(items) != 0 || body["total"].(float64) != 1 {
		t.Fatalf("offset-beyond page wrong: %+v", body)
	}
}

func TestGetIntFieldNativeInt(t *testing.T) {
	// A native int value (not float64) is accepted — direct-call coverage of the
	// case JSON decoding cannot reach.
	got, err := getIntField(map[string]any{"width": 42}, "width", 0)
	if err != nil || got != 42 {
		t.Fatalf("native int: %v %d", err, got)
	}
	// JSON numbers decode to float64.
	got, err = getIntField(map[string]any{"width": float64(800)}, "width", 0)
	if err != nil || got != 800 {
		t.Fatalf("float64: %v %d", err, got)
	}
}

func TestGraphicalHandlerForbiddenAndBadBody(t *testing.T) {
	ts := newTestServer(t, nil)
	// Anonymous (no tenant) hits the 403 arm of GET/PUT/DELETE handlers too.
	if rec := ts.do("GET", "/api/graphical-targets/x", "", nil); rec.Code != http.StatusForbidden {
		t.Fatalf("anon get = %d", rec.Code)
	}
	if rec := ts.do("PUT", "/api/graphical-targets/x", `{"endpoint":"vm:5900"}`, nil); rec.Code != http.StatusForbidden {
		t.Fatalf("anon put = %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/graphical-targets/x", "", nil); rec.Code != http.StatusForbidden {
		t.Fatalf("anon delete = %d", rec.Code)
	}
	// A wrong-type field in a PUT body → 422 via the parse-error arm.
	h := tenantHeaders("operator", "acme")
	if rec := ts.do("PUT", "/api/graphical-targets/x", `{"width":"abc","endpoint":"vm:5900"}`, h); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("put bad width = %d", rec.Code)
	}
}

func TestGraphicalListReturnsItems(t *testing.T) {
	ts := newTestServer(t, nil)
	h := tenantHeaders("operator", "acme")
	ts.do("POST", "/api/graphical-targets", createRfbBody(), h)
	body := gtJSON(t, ts.do("GET", "/api/graphical-targets", "", h).Body.Bytes())
	items := body["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("expected 1 item, got %d", len(items))
	}
	// Public copy in the list must not carry the secret.
	if _, ok := items[0].(map[string]any)["secret"]; ok {
		t.Fatalf("secret leaked in list")
	}
}
