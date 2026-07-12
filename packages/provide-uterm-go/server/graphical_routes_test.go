package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func routeTarget(id, tenant string) serverconfig.GraphicalTargetDefinition {
	x := graphicalTarget(id, &tenant)
	secret := "env:TOP_SECRET" // pragma: allowlist secret
	x.CASecretRef = &secret
	return x
}

func routeBody(target serverconfig.GraphicalTargetDefinition) string {
	raw, _ := json.Marshal(target)
	var payload map[string]any
	_ = json.Unmarshal(raw, &payload)
	delete(payload, "tenant_id")
	raw, _ = json.Marshal(payload)
	return string(raw)
}

func TestGraphicalTargetRoutesTenantIsolationAndRedaction(t *testing.T) {
	engine := memory.New(cp.DefaultConfig())
	registry, err := NewGraphicalTargetRegistry(nil, engine, false)
	if err != nil {
		t.Fatal(err)
	}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	adminA := map[string]string{"X-Subject": "admin-a", "X-Role": "admin", "X-Tenant": "tenant-a"}
	adminB := map[string]string{"X-Subject": "admin-b", "X-Role": "admin", "X-Tenant": "tenant-b"}
	resp := ts.do(http.MethodPost, "/api/graphical-targets", routeBody(routeTarget("target-a", "tenant-a")), adminA)
	if resp.Code != http.StatusCreated {
		t.Fatalf("create = %d %s", resp.Code, resp.Body.String())
	}
	if strings.Contains(resp.Body.String(), "secret_ref") || strings.Contains(resp.Body.String(), "TOP_SECRET") {
		t.Fatalf("secret reference disclosed: %s", resp.Body.String())
	}
	if got := ts.do(http.MethodGet, "/api/graphical-targets/target-a", "", adminB); got.Code != http.StatusNotFound {
		t.Fatalf("cross tenant get = %d %s", got.Code, got.Body.String())
	}
	if got := ts.do(http.MethodGet, "/api/graphical-targets?limit=25&offset=10", "", adminB); got.Code != http.StatusOK || strings.Contains(got.Body.String(), "target-a") ||
		got.Body.String() != "{\"items\":[],\"limit\":25,\"offset\":10,\"total\":0}\n" {
		t.Fatalf("cross tenant list = %d %s", got.Code, got.Body.String())
	}
}

func TestGraphicalTargetRoutesRejectManagedFields(t *testing.T) {
	engine := memory.New(cp.DefaultConfig())
	registry, _ := NewGraphicalTargetRegistry(nil, engine, false)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	headers := map[string]string{"X-Subject": "admin", "X-Role": "admin", "X-Tenant": "tenant-a"}
	body, _ := json.Marshal(routeTarget("target-a", "tenant-a"))
	if got := ts.do(http.MethodPost, "/api/graphical-targets", string(body), headers); got.Code != http.StatusUnprocessableEntity {
		t.Fatalf("supplied tenant = %d %s", got.Code, got.Body.String())
	}
	target := routeTarget("other", "tenant-a")
	if got := ts.do(http.MethodPut, "/api/graphical-targets/target-a", routeBody(target), headers); got.Code != http.StatusConflict {
		t.Fatalf("mismatched target id = %d %s", got.Code, got.Body.String())
	}
}

func TestGraphicalTargetListEnvelopePaginatesTenantResults(t *testing.T) {
	engine := memory.New(cp.DefaultConfig())
	registry, _ := NewGraphicalTargetRegistry(nil, engine, false)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	headers := map[string]string{"X-Subject": "admin", "X-Role": "admin", "X-Tenant": "tenant-a"}
	for _, id := range []string{"a", "b", "c"} {
		if got := ts.do(http.MethodPost, "/api/graphical-targets", routeBody(routeTarget(id, "tenant-a")), headers); got.Code != http.StatusCreated {
			t.Fatalf("create %s = %d %s", id, got.Code, got.Body.String())
		}
	}
	got := ts.do(http.MethodGet, "/api/graphical-targets?limit=1&offset=1", "", headers)
	if got.Code != http.StatusOK || !strings.Contains(got.Body.String(), `"target_id":"b"`) ||
		!strings.Contains(got.Body.String(), `"total":3`) || strings.Contains(got.Body.String(), `"target_id":"a"`) {
		t.Fatalf("page = %d %s", got.Code, got.Body.String())
	}
}

func TestGraphicalRouteErrorMapping(t *testing.T) {
	cases := []struct {
		err    error
		status int
		code   string
	}{
		{ErrGraphicalTargetNotFound, 404, "graphical_target_not_found"},
		{ErrGraphicalTargetForbidden, 404, "graphical_target_not_found"},
		{ErrGraphicalTargetAlreadyExists, 409, "graphical_target_exists"},
		{ErrGraphicalTargetImmutable, 409, "graphical_target_immutable"},
		{ErrGraphicalTargetTransaction, 409, "graphical_target_conflict"},
		{ErrGraphicalTargetInvalid, 422, "graphical_target_invalid"},
		{ErrGraphicalTargetClosed, 503, "graphical_target_unavailable"},
		{assertionError("backend sensitive"), 503, "graphical_target_backend_error"},
	}
	for _, tc := range cases {
		rec := httptest.NewRecorder()
		graphicalRouteError(rec, tc.err)
		if rec.Code != tc.status || !strings.Contains(rec.Body.String(), tc.code) || strings.Contains(rec.Body.String(), "sensitive") {
			t.Errorf("%v => %d %s", tc.err, rec.Code, rec.Body.String())
		}
	}
}

type assertionError string

func (e assertionError) Error() string { return string(e) }

func TestGraphicalTargetRoutesDenyTenantlessAndViewerMutation(t *testing.T) {
	engine := memory.New(cp.DefaultConfig())
	registry, _ := NewGraphicalTargetRegistry(nil, engine, false)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	body, _ := json.Marshal(routeTarget("target-a", "tenant-a"))
	for _, headers := range []map[string]string{
		{"X-Subject": "admin", "X-Role": "admin"},
		{"X-Subject": "viewer", "X-Role": "viewer", "X-Tenant": "tenant-a"},
	} {
		resp := ts.do(http.MethodPost, "/api/graphical-targets", string(body), headers)
		if resp.Code != http.StatusForbidden {
			t.Errorf("mutation = %d %s", resp.Code, resp.Body.String())
		}
	}
}
