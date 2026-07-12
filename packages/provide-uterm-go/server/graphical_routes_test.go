package server

import (
	"encoding/json"
	"net/http"
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

func TestGraphicalTargetRoutesTenantIsolationAndRedaction(t *testing.T) {
	engine := memory.New(cp.DefaultConfig())
	registry, err := NewGraphicalTargetRegistry(nil, engine, false)
	if err != nil {
		t.Fatal(err)
	}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	adminA := map[string]string{"X-Subject": "admin-a", "X-Role": "admin", "X-Tenant": "tenant-a"}
	adminB := map[string]string{"X-Subject": "admin-b", "X-Role": "admin", "X-Tenant": "tenant-b"}
	body, _ := json.Marshal(routeTarget("target-a", "tenant-a"))
	resp := ts.do(http.MethodPost, "/api/graphical-targets", string(body), adminA)
	if resp.Code != http.StatusCreated {
		t.Fatalf("create = %d %s", resp.Code, resp.Body.String())
	}
	if strings.Contains(resp.Body.String(), "secret_ref") || strings.Contains(resp.Body.String(), "TOP_SECRET") {
		t.Fatalf("secret reference disclosed: %s", resp.Body.String())
	}
	if got := ts.do(http.MethodGet, "/api/graphical-targets/target-a", "", adminB); got.Code != http.StatusNotFound {
		t.Fatalf("cross tenant get = %d %s", got.Code, got.Body.String())
	}
	if got := ts.do(http.MethodGet, "/api/graphical-targets", "", adminB); got.Code != http.StatusOK || strings.Contains(got.Body.String(), "target-a") {
		t.Fatalf("cross tenant list = %d %s", got.Code, got.Body.String())
	}
}

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
