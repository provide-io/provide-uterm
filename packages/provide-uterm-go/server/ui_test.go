//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// writeViteManifest writes a .vite/manifest.json describing the React entry into
// dir so the renderer resolves the Vite (React) surface.
func writeViteManifest(t *testing.T, dir string) {
	t.Helper()
	viteDir := filepath.Join(dir, ".vite")
	if err := os.MkdirAll(viteDir, 0o750); err != nil {
		t.Fatalf("mkdir .vite: %v", err)
	}
	manifest := `{"src/main.tsx":{"file":"assets/main-ABC123.js","name":"main","isEntry":true,"css":["assets/main-DEF456.css"]}}`
	if err := os.WriteFile(filepath.Join(viteDir, "manifest.json"), []byte(manifest), 0o600); err != nil {
		t.Fatalf("write vite manifest: %v", err)
	}
}

// writeVanillaManifest writes a root vanilla-manifest.json mapping src/hijack.ts
// to a hashed output file.
func writeVanillaManifest(t *testing.T, dir string) {
	t.Helper()
	manifest := `{"src/hijack.ts":{"file":"assets/hijack_script-XYZ.js","name":"hijack_script"}}`
	if err := os.WriteFile(filepath.Join(dir, "vanilla-manifest.json"), []byte(manifest), 0o600); err != nil {
		t.Fatalf("write vanilla manifest: %v", err)
	}
}

// extractBootstrap pulls the #app-bootstrap JSON payload out of a rendered page
// and unmarshals it. The blob never contains a literal "</" (they are escaped to
// "<\/"), so splitting on "</script>" isolates it cleanly.
func extractBootstrap(t *testing.T, htmlDoc string) map[string]any {
	t.Helper()
	const marker = "<script type='application/json' id='app-bootstrap'>"
	idx := strings.Index(htmlDoc, marker)
	if idx == -1 {
		t.Fatalf("no app-bootstrap tag in:\n%s", htmlDoc)
	}
	rest := htmlDoc[idx+len(marker):]
	end := strings.Index(rest, "</script>")
	if end == -1 {
		t.Fatalf("unterminated app-bootstrap tag")
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(rest[:end]), &out); err != nil {
		t.Fatalf("bootstrap json: %v\nblob: %s", err, rest[:end])
	}
	return out
}

// TestUIPageBootstrap asserts every page kind carries the right bootstrap JSON.
func TestUIPageBootstrap(t *testing.T) {
	dir := t.TempDir()
	writeViteManifest(t, dir)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.FrontendDir = dir })
	ts.reg.add("s1", "admin1", "public")

	cases := []struct {
		path      string
		pageKind  string
		surface   string
		hasScope  bool
		sessionID string
	}{
		{"/app/", "dashboard", "", false, ""},
		{"/app/connect", "connect", "", false, ""},
		{"/app/session/s1", "session", "user", true, "s1"},
		{"/app/operator/s1", "operator", "operator", true, "s1"},
		{"/app/replay/s1", "replay", "operator", true, "s1"},
		{"/app/inspect/s1", "inspect", "operator", true, "s1"},
	}
	for _, c := range cases {
		rec := ts.do("GET", c.path, "", adminHeaders())
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status %d", c.path, rec.Code)
		}
		body := rec.Body.String()
		boot := extractBootstrap(t, body)
		if boot["page_kind"] != c.pageKind {
			t.Fatalf("%s: page_kind=%v want %s", c.path, boot["page_kind"], c.pageKind)
		}
		if _, ok := boot["app_path"]; !ok {
			t.Fatalf("%s: missing app_path", c.path)
		}
		if _, ok := boot["assets_path"]; !ok {
			t.Fatalf("%s: missing assets_path", c.path)
		}
		if c.hasScope {
			if boot["surface"] != c.surface {
				t.Fatalf("%s: surface=%v want %s", c.path, boot["surface"], c.surface)
			}
			if boot["session_id"] != c.sessionID {
				t.Fatalf("%s: session_id=%v want %s", c.path, boot["session_id"], c.sessionID)
			}
			// share_role is always present; null for a non-share principal (this
			// request carries an IdP header, not a tunnel-share cookie). The
			// share-cookie path is covered by TestSharePageEmitsShareRole.
			if v, ok := boot["share_role"]; !ok || v != nil {
				t.Fatalf("%s: share_role=%v present=%v want null", c.path, v, ok)
			}
		}
		// Vite manifest present → the React script + CSS tags render, no CDN
		// omission of xterm assets.
		if !strings.Contains(body, "assets/main-ABC123.js") {
			t.Fatalf("%s: missing vite entry script", c.path)
		}
		if !strings.Contains(body, "assets/main-DEF456.css") {
			t.Fatalf("%s: missing vite css link", c.path)
		}
		// xterm CDN tags always present (default config).
		if !strings.Contains(body, "/lib/xterm.js") {
			t.Fatalf("%s: missing xterm cdn script", c.path)
		}
	}
}

// TestUIReplayTitleSuffix asserts the replay page title is suffixed " Replay".
func TestUIReplayTitleSuffix(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.mu.Lock()
	ts.reg.defs["s1"] = &serverconfig.SessionDefinition{SessionID: "s1", DisplayName: "My Box", Visibility: "public"}
	ts.reg.mu.Unlock()

	rec := ts.do("GET", "/app/replay/s1", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "<title>My Box Replay</title>") {
		t.Fatalf("replay title not suffixed:\n%s", rec.Body.String())
	}
}

// TestUIVanillaFallback asserts that without a Vite manifest the session page
// falls back to the vanilla <uterm-session> element + hijack pre-vite script.
func TestUIVanillaFallback(t *testing.T) {
	dir := t.TempDir()
	writeVanillaManifest(t, dir) // vanilla manifest present, but NO .vite/manifest.json
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.FrontendDir = dir })
	ts.reg.add("s1", "admin1", "public")

	rec := ts.do("GET", "/app/session/s1", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	body := rec.Body.String()
	if strings.Contains(body, "<div id='app-root'>") {
		t.Fatalf("vanilla surface should use <uterm-session>, got div:\n%s", body)
	}
	if !strings.Contains(body, "<uterm-session id='app-root'></uterm-session>") {
		t.Fatalf("missing uterm-session element:\n%s", body)
	}
	// The hijack pre-vite module resolves via the vanilla manifest.
	if !strings.Contains(body, "assets/hijack_script-XYZ.js") {
		t.Fatalf("missing resolved hijack pre-vite script:\n%s", body)
	}
}

// TestUIVanillaFallbackNoManifest asserts the basename fallback when neither
// manifest exists (empty frontend dir): hijack.ts → hijack.js.
func TestUIVanillaFallbackNoManifest(t *testing.T) {
	ts := newTestServer(t, nil) // no FrontendDir at all
	ts.reg.add("s1", "admin1", "public")

	rec := ts.do("GET", "/app/session/s1", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "<uterm-session id='app-root'></uterm-session>") {
		t.Fatalf("missing uterm-session element:\n%s", body)
	}
	// hijack.ts basename fallback → hijack.js.
	if !strings.Contains(body, "/_terminal/hijack.js'></script>") {
		t.Fatalf("missing hijack.js basename fallback:\n%s", body)
	}
}

// TestUIPageNotFound asserts a 404 for an unknown session on every scoped page.
func TestUIPageNotFound(t *testing.T) {
	ts := newTestServer(t, nil)
	for _, path := range []string{"/app/session/ghost", "/app/operator/ghost", "/app/replay/ghost", "/app/inspect/ghost"} {
		rec := ts.do("GET", path, "", adminHeaders())
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s: status %d want 404", path, rec.Code)
		}
	}
}

// TestUIPageForbidden asserts a 403 when the caller cannot read the session.
func TestUIPageForbidden(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("priv", "admin1", "private")
	for _, path := range []string{"/app/session/priv", "/app/operator/priv", "/app/replay/priv", "/app/inspect/priv"} {
		rec := ts.do("GET", path, "", viewerHeaders())
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s: status %d want 403", path, rec.Code)
		}
	}
}

// TestUIPageInvalidID asserts a 422 for a malformed session id path param.
func TestUIPageInvalidID(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("GET", "/app/session/bad%20id", "", adminHeaders())
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status %d want 422", rec.Code)
	}
}

// TestUIPageCookies asserts principal + surface cookies always set, and the
// token cookie only in jwt mode for a non-anonymous principal with a bearer
// token.
func TestUIPageCookies(t *testing.T) {
	// dev_token mode (default): principal + surface set, NO token cookie even
	// with a bearer header.
	ts := newTestServer(t, nil)
	headers := map[string]string{"X-Subject": "admin1", "X-Role": "admin", "Authorization": "Bearer tok123"}
	rec := ts.do("GET", "/app/", "", headers)
	cookies := rec.Result().Cookies()
	if c := cookieByName(cookies, "uterm_principal"); c == nil || c.Value != "admin1" {
		t.Fatalf("principal cookie=%v", c)
	}
	if c := cookieByName(cookies, "uterm_surface"); c == nil || c.Value != "operator" {
		t.Fatalf("surface cookie=%v", c)
	}
	if !cookieByName(cookies, "uterm_principal").HttpOnly {
		t.Fatalf("principal cookie not HttpOnly")
	}
	if c := cookieByName(cookies, "uterm_token"); c != nil {
		t.Fatalf("token cookie set in dev_token mode: %v", c)
	}
	// The session page uses surface=user.
	ts.reg.add("s1", "admin1", "public")
	rec = ts.do("GET", "/app/session/s1", "", headers)
	if c := cookieByName(rec.Result().Cookies(), "uterm_surface"); c == nil || c.Value != "user" {
		t.Fatalf("session surface cookie=%v", c)
	}

	// jwt mode + bearer token → token cookie set.
	jwtTS := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) { cfg.Auth.Mode = "jwt" })
	rec = jwtTS.do("GET", "/app/", "", headers)
	if c := cookieByName(rec.Result().Cookies(), "uterm_token"); c == nil || c.Value != "tok123" {
		t.Fatalf("token cookie=%v want tok123", c)
	}

	// jwt mode but no bearer token → no token cookie.
	rec = jwtTS.do("GET", "/app/", "", map[string]string{"X-Subject": "admin1", "X-Role": "admin"})
	if c := cookieByName(rec.Result().Cookies(), "uterm_token"); c != nil {
		t.Fatalf("token cookie set without bearer: %v", c)
	}
}

// TestUIPageCookieSecure asserts the Secure flag follows X-Forwarded-Proto.
func TestUIPageCookieSecure(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("GET", "/app/", "", map[string]string{"X-Subject": "admin1", "X-Role": "admin", "X-Forwarded-Proto": "https"})
	if c := cookieByName(rec.Result().Cookies(), "uterm_principal"); c == nil || !c.Secure {
		t.Fatalf("secure flag not honoured: %v", c)
	}
	// Plain http → not secure.
	rec = ts.do("GET", "/app/", "", adminHeaders())
	if c := cookieByName(rec.Result().Cookies(), "uterm_principal"); c == nil || c.Secure {
		t.Fatalf("secure flag set without https: %v", c)
	}
}

// TestUIHTMLEscaping asserts a session display_name containing markup cannot
// break out of the page: the <title> is escaped and no unescaped <script>
// injection appears, while the bootstrap JSON still round-trips the raw value.
func TestUIHTMLEscaping(t *testing.T) {
	ts := newTestServer(t, nil)
	evil := "<script>alert(1)</script>"
	ts.reg.mu.Lock()
	ts.reg.defs["s1"] = &serverconfig.SessionDefinition{SessionID: "s1", DisplayName: evil, Visibility: "public"}
	ts.reg.mu.Unlock()

	rec := ts.do("GET", "/app/session/s1", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
	body := rec.Body.String()
	// The raw injection must never appear unescaped.
	if strings.Contains(body, "<script>alert(1)</script>") {
		t.Fatalf("unescaped injection present:\n%s", body)
	}
	// The escaped title must be present.
	if !strings.Contains(body, "&lt;script&gt;alert(1)&lt;/script&gt;") {
		t.Fatalf("title not escaped:\n%s", body)
	}
	// The bootstrap JSON round-trips the raw display name.
	boot := extractBootstrap(t, body)
	if boot["title"] != evil {
		t.Fatalf("bootstrap title=%q want %q", boot["title"], evil)
	}
}

// TestBootstrapTagScriptEscaping directly asserts a payload value containing
// "</script>" does not terminate the tag early, and the JSON still parses.
func TestBootstrapTagScriptEscaping(t *testing.T) {
	tag := bootstrapTag(map[string]any{"title": "pwn</script><img src=x>", "page_kind": "dashboard"})
	// Exactly one real closing </script> (the tag's own).
	if n := strings.Count(tag, "</script>"); n != 1 {
		t.Fatalf("expected 1 closing </script>, got %d: %s", n, tag)
	}
	if !strings.Contains(tag, `<\/script>`) {
		t.Fatalf("payload </ not escaped to <\\/: %s", tag)
	}
	// The blob still parses back to the original value.
	const marker = "<script type='application/json' id='app-bootstrap'>"
	blob := strings.TrimSuffix(strings.TrimPrefix(tag, marker), "</script>")
	var out map[string]any
	if err := json.Unmarshal([]byte(blob), &out); err != nil {
		t.Fatalf("blob parse: %v", err)
	}
	if out["title"] != "pwn</script><img src=x>" {
		t.Fatalf("round-trip title=%q", out["title"])
	}
}

// TestUISRIIntegrity asserts integrity + crossorigin attributes render when SRI
// hashes are configured, and are absent otherwise.
func TestUISRIIntegrity(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.UI.XtermCDNIntegrity = "sha384-xterm"
		cfg.UI.FitAddonCDNIntegrity = "sha384-fit"
	})
	rec := ts.do("GET", "/app/", "", adminHeaders())
	body := rec.Body.String()
	if !strings.Contains(body, "integrity='sha384-xterm' crossorigin='anonymous'") {
		t.Fatalf("xterm SRI missing:\n%s", body)
	}
	if !strings.Contains(body, "integrity='sha384-fit' crossorigin='anonymous'") {
		t.Fatalf("fit-addon SRI missing:\n%s", body)
	}

	// No SRI configured → no integrity attribute.
	plain := newTestServer(t, nil)
	rec = plain.do("GET", "/app/", "", adminHeaders())
	if strings.Contains(rec.Body.String(), "integrity=") {
		t.Fatalf("unexpected integrity attribute without SRI config")
	}
}

// TestUIManifestCached asserts the manifest is read once and cached: deleting
// the file after the first render does not change the second render.
func TestUIManifestCached(t *testing.T) {
	dir := t.TempDir()
	writeViteManifest(t, dir)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.FrontendDir = dir })

	first := ts.do("GET", "/app/", "", adminHeaders()).Body.String()
	if !strings.Contains(first, "assets/main-ABC123.js") {
		t.Fatalf("first render missing vite entry")
	}
	// Remove the manifest — the cached result must survive.
	if err := os.RemoveAll(filepath.Join(dir, ".vite")); err != nil {
		t.Fatalf("rm manifest: %v", err)
	}
	second := ts.do("GET", "/app/", "", adminHeaders()).Body.String()
	if !strings.Contains(second, "assets/main-ABC123.js") {
		t.Fatalf("cached manifest not reused after deletion:\n%s", second)
	}
}

// TestUIMalformedManifest asserts a malformed manifest is treated as absent
// (vanilla fallback), covering the JSON-parse error branch.
func TestUIMalformedManifest(t *testing.T) {
	dir := t.TempDir()
	viteDir := filepath.Join(dir, ".vite")
	if err := os.MkdirAll(viteDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(viteDir, "manifest.json"), []byte("{not json"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.FrontendDir = dir })
	ts.reg.add("s1", "admin1", "public")
	body := ts.do("GET", "/app/session/s1", "", adminHeaders()).Body.String()
	// No vite tags → vanilla surface.
	if !strings.Contains(body, "<uterm-session id='app-root'></uterm-session>") {
		t.Fatalf("malformed manifest should fall back to vanilla:\n%s", body)
	}
}

// TestViteEntryTagsMalformedEntry asserts viteEntryTags returns "" when the
// src/main.tsx entry is not an object.
func TestViteEntryTagsMalformedEntry(t *testing.T) {
	dir := t.TempDir()
	viteDir := filepath.Join(dir, ".vite")
	if err := os.MkdirAll(viteDir, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(viteDir, "manifest.json"), []byte(`{"src/main.tsx":"nope"}`), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	m := newUIManifests(dir)
	if got := m.viteEntryTags("/_terminal"); got != "" {
		t.Fatalf("viteEntryTags=%q want empty", got)
	}
}

// TestShellScriptsAndCSS exercises the shell legacy scripts + extra-css tags
// (only rendered in vanilla-only mode, which the page builders don't use, so
// this drives shell directly).
func TestShellScriptsAndCSS(t *testing.T) {
	m := newUIManifests("") // no manifests → vanilla-only, scripts not suppressed
	out := m.shell(shellOpts{
		title:      "t",
		assetsPath: "/_terminal",
		body:       "<body></body>",
		extraCSS:   []string{"style.css"},
		scripts:    []string{"legacy.js"},
	})
	if !strings.Contains(out, "<link rel='stylesheet' href='/_terminal/style.css'>") {
		t.Fatalf("missing extra css:\n%s", out)
	}
	if !strings.Contains(out, "<script type='module' src='/_terminal/legacy.js'></script>") {
		t.Fatalf("missing legacy script:\n%s", out)
	}
}

// TestPageRoutesPathFallbacks covers the empty AppPath / AssetsPath defaults in
// registerPageRoutes.
func TestPageRoutesPathFallbacks(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "x.js"), []byte("//x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.UI.AppPath = ""
		cfg.UI.AssetsPath = ""
		deps.FrontendDir = dir
	})
	if rec := ts.do("GET", "/app/", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("default app path: %d", rec.Code)
	}
	if rec := ts.do("GET", "/_terminal/x.js", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("default assets path: %d", rec.Code)
	}
}

// TestUINoCDN asserts the xterm/fonts CDN tags are omitted when unset.
func TestUINoCDN(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.UI.XtermCDN = ""
		cfg.UI.FitAddonCDN = ""
		cfg.UI.FontsCDN = ""
	})
	body := ts.do("GET", "/app/", "", adminHeaders()).Body.String()
	if strings.Contains(body, "xterm.js") || strings.Contains(body, "addon-fit.js") || strings.Contains(body, "fonts.googleapis") {
		t.Fatalf("CDN tags rendered despite empty config:\n%s", body)
	}
}
