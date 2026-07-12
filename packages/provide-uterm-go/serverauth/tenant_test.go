package serverauth

import (
	"path/filepath"
	"testing"

	jwt "github.com/golang-jwt/jwt/v5"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestCanonicalTenantID(t *testing.T) {
	valid := []string{"tenant-1", "A.b_c"}
	for _, value := range valid {
		if got, err := CanonicalTenantID(value); err != nil || got != value {
			t.Fatalf("CanonicalTenantID(%q) = %q, %v", value, got, err)
		}
	}
	for _, value := range []string{"", " tenant", "../tenant", "tenant/id", "tenant\nother"} {
		if _, err := CanonicalTenantID(value); err == nil {
			t.Errorf("CanonicalTenantID(%q) accepted", value)
		}
	}
}

func TestDevIDPMintsCanonicalTenant(t *testing.T) {
	auth := serverconfig.DefaultServerConfig().Auth
	token, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: filepath.Join(t.TempDir(), "token"), TenantID: "dev-tenant"})
	if err != nil {
		t.Fatal(err)
	}
	p, err := NewLocalIdentityProvider(&auth, nil).PrincipalFromJWTToken(token)
	if err != nil || p.TenantID != "dev-tenant" {
		t.Fatalf("dev tenant = %#v, %v", p, err)
	}
}

func TestJWTPrincipalRequiresCanonicalTenant(t *testing.T) {
	secret := "secret" // pragma: allowlist secret
	auth := serverconfig.DefaultServerConfig().Auth
	auth.JWTPublicKeyPEM = &secret
	auth.JWTTenantClaim = "tenant_id"
	p := NewLocalIdentityProvider(&auth, nil)
	mint := func(tenant any) string {
		claims := jwt.MapClaims{"sub": "alice", "iss": auth.JWTIssuer, "aud": auth.JWTAudience, "exp": float64(4102444800), "tenant_id": tenant}
		tok := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
		s, _ := tok.SignedString([]byte(secret))
		return s
	}
	principal, err := p.PrincipalFromJWTToken(mint("tenant-a"))
	if err != nil || principal.TenantID != "tenant-a" {
		t.Fatalf("tenant principal = %#v, %v", principal, err)
	}
	for _, bad := range []any{"", "../other", []any{"tenant-a"}} {
		if _, err := p.PrincipalFromJWTToken(mint(bad)); err == nil {
			t.Errorf("accepted tenant claim %#v", bad)
		}
	}
}

func TestGraphicalCapabilitiesAreExplicit(t *testing.T) {
	authz := NewAuthorizationService()
	for role, wants := range map[string][]string{
		"viewer":   {"graphical.target.read", "graphical.session.attach"},
		"operator": {"graphical.target.read", "graphical.session.attach"},
		"admin":    {"graphical.target.read", "graphical.target.manage", "graphical.session.attach"},
	} {
		p := &Principal{SubjectID: "u", TenantID: "t", Roles: NewSet(role), Scopes: NewSet("*")}
		for _, want := range wants {
			if !authz.HasCapability(p, want) {
				t.Errorf("%s lacks %s", role, want)
			}
		}
	}
	if authz.HasCapability(AnonymousPrincipal(), "graphical.target.read") {
		t.Fatal("tenantless anonymous principal gained graphical access")
	}
}

func TestHeaderAuthFailsClosedWithoutCanonicalTenant(t *testing.T) {
	auth := serverconfig.DefaultServerConfig().Auth
	provider := NewLocalIdentityProvider(&auth, nil)
	for _, tenant := range []string{"", "../spoof"} {
		p := provider.PrincipalFromHeaderAuth(&Request{Headers: map[string]string{
			auth.PrincipalHeader: "admin", auth.RoleHeader: "admin", auth.TenantHeader: tenant,
		}})
		if p.SubjectID != "anonymous" || p.TenantID != "" || p.Roles.Has("admin") {
			t.Errorf("tenant %q retained authority: %#v", tenant, p)
		}
	}
}
