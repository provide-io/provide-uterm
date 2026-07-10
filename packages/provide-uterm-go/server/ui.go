//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"encoding/json"
	"html"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// uiManifests is the Go port of ui.py's HTML page rendering. It reads the Vite
// (React) and legacy vanilla Vite manifests from the built frontend directory
// once (mirroring ui.py's module-level "read once, cache the result — including
// a missing/failed read" behavior) via sync.Once, then builds the page shells.
//
// When frontendDir is empty (no built frontend mounted) both manifests resolve
// to nil, matching Python's vanilla-only fallback when the manifest files are
// absent.
type uiManifests struct {
	frontendDir string

	viteOnce sync.Once
	vite     map[string]any // nil when the React app has not been built

	vanillaOnce sync.Once
	vanilla     map[string]any // nil when no vanilla-manifest is present
}

// newUIManifests builds a renderer bound to a built-frontend directory.
func newUIManifests(frontendDir string) *uiManifests {
	return &uiManifests{frontendDir: frontendDir}
}

// readManifestFile reads and JSON-decodes a manifest file, returning nil when
// it does not exist or cannot be parsed (Python swallows read/parse errors).
func readManifestFile(path string) map[string]any {
	raw, err := os.ReadFile(path) //nolint:gosec // path is server-controlled frontend build output
	if err != nil {
		return nil
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

// readVite returns the parsed Vite manifest, or nil if the React app has not
// been built (vanilla-only mode). Port of _read_vite_manifest.
func (m *uiManifests) readVite() map[string]any {
	m.viteOnce.Do(func() {
		if m.frontendDir == "" {
			return
		}
		m.vite = readManifestFile(filepath.Join(m.frontendDir, ".vite", "manifest.json"))
	})
	return m.vite
}

// readVanilla returns the parsed vanilla manifest, or nil. It mirrors
// _read_vanilla_manifest's fallback: prefer frontend/.vite/vanilla-manifest.json,
// then frontend/vanilla-manifest.json.
func (m *uiManifests) readVanilla() map[string]any {
	m.vanillaOnce.Do(func() {
		if m.frontendDir == "" {
			return
		}
		manifest := readManifestFile(filepath.Join(m.frontendDir, ".vite", "vanilla-manifest.json"))
		if manifest == nil {
			manifest = readManifestFile(filepath.Join(m.frontendDir, "vanilla-manifest.json"))
		}
		m.vanilla = manifest
	})
	return m.vanilla
}

// resolveVanillaAsset maps a source entry name (e.g. "src/hijack.ts") to its
// hashed output filename via the vanilla manifest, falling back to the basename
// with .ts→.js. Port of _resolve_vanilla_asset.
func (m *uiManifests) resolveVanillaAsset(entryName string) string {
	manifest := m.readVanilla()
	if manifest != nil {
		if entry, ok := manifest[entryName].(map[string]any); ok {
			if file, ok := entry["file"].(string); ok {
				return file
			}
		}
	}
	base := entryName
	if idx := strings.LastIndex(base, "/"); idx != -1 {
		base = base[idx+1:]
	}
	return strings.Replace(base, ".ts", ".js", 1)
}

// viteEntryTags returns the <link>/<script> tags for the Vite React entry
// point, or "" when no manifest exists (vanilla-only). Port of _vite_entry_tags.
func (m *uiManifests) viteEntryTags(assetsPath string) string {
	manifest := m.readVite()
	if manifest == nil {
		return ""
	}
	entry, ok := manifest["src/main.tsx"].(map[string]any)
	if !ok {
		return ""
	}
	safe := html.EscapeString(assetsPath)
	var b strings.Builder
	if cssFiles, ok := entry["css"].([]any); ok {
		for _, cf := range cssFiles {
			if name, ok := cf.(string); ok {
				b.WriteString("<link rel='stylesheet' href='" + safe + "/" + html.EscapeString(name) + "'>")
			}
		}
	}
	if jsFile, ok := entry["file"].(string); ok && jsFile != "" {
		b.WriteString("<script type='module' src='" + safe + "/" + html.EscapeString(jsFile) + "'></script>")
	}
	return b.String()
}

// pageCDN carries the xterm.js / fit-addon / fonts CDN URLs and their optional
// SRI integrity hashes.
type pageCDN struct {
	xtermCDN             string
	fitAddonCDN          string
	fontsCDN             string
	xtermCDNIntegrity    string
	fitAddonCDNIntegrity string
}

// cdnFromUI extracts the CDN fields from the UI config.
func cdnFromUI(u serverconfig.UiConfig) pageCDN {
	return pageCDN{
		xtermCDN:             u.XtermCDN,
		fitAddonCDN:          u.FitAddonCDN,
		fontsCDN:             u.FontsCDN,
		xtermCDNIntegrity:    u.XtermCDNIntegrity,
		fitAddonCDNIntegrity: u.FitAddonCDNIntegrity,
	}
}

// shellOpts bundles the arguments for shell.
type shellOpts struct {
	title          string
	assetsPath     string
	body           string
	extraCSS       []string
	scripts        []string
	preViteModules []string
	cdn            pageCDN
}

// shell renders the outer HTML document. Port of ui.py::_shell. When the Vite
// manifest is present the React app takes over rendering, so the legacy vanilla
// scripts and extra CSS are suppressed.
func (m *uiManifests) shell(o shellOpts) string {
	viteTags := m.viteEntryTags(o.assetsPath)
	scripts := o.scripts
	extraCSS := o.extraCSS
	if viteTags != "" {
		scripts = nil
		extraCSS = nil
	}
	safeAssets := html.EscapeString(o.assetsPath)

	var cssLinks strings.Builder
	for _, name := range extraCSS {
		cssLinks.WriteString("<link rel='stylesheet' href='" + safeAssets + "/" + html.EscapeString(name) + "'>")
	}
	var scriptTags strings.Builder
	for _, name := range scripts {
		scriptTags.WriteString("<script type='module' src='" + safeAssets + "/" + html.EscapeString(name) + "'></script>")
	}
	// pre_vite_modules must execute BEFORE the Vite bundle so their exports are
	// available when React effects run.
	var preViteTags strings.Builder
	for _, name := range o.preViteModules {
		preViteTags.WriteString("<script type='module' src='" + safeAssets + "/" + html.EscapeString(name) + "'></script>")
	}

	// SRI: emit integrity= + crossorigin=anonymous so the browser refuses to run
	// a tampered CDN asset — the primary defense against a compromised CDN.
	xtermSRI := ""
	if o.cdn.xtermCDNIntegrity != "" {
		xtermSRI = " integrity='" + html.EscapeString(o.cdn.xtermCDNIntegrity) + "' crossorigin='anonymous'"
	}
	fitAddonSRI := ""
	if o.cdn.fitAddonCDNIntegrity != "" {
		fitAddonSRI = " integrity='" + html.EscapeString(o.cdn.fitAddonCDNIntegrity) + "' crossorigin='anonymous'"
	}

	xtermCSS := ""
	xtermJS := ""
	if o.cdn.xtermCDN != "" {
		safeXterm := html.EscapeString(o.cdn.xtermCDN)
		xtermCSS = "<link rel='stylesheet' href='" + safeXterm + "/css/xterm.css'>"
		xtermJS = "<script src='" + safeXterm + "/lib/xterm.js'" + xtermSRI + "></script>"
	}
	fitAddonJS := ""
	if o.cdn.fitAddonCDN != "" {
		fitAddonJS = "<script src='" + html.EscapeString(o.cdn.fitAddonCDN) + "/lib/addon-fit.js'" + fitAddonSRI + "></script>"
	}
	fontsLink := ""
	if o.cdn.fontsCDN != "" {
		fontsLink = "<link href='" + html.EscapeString(o.cdn.fontsCDN) + "' rel='stylesheet'>"
	}

	return "<!DOCTYPE html><html><head><meta charset='UTF-8'>" +
		"<meta name='viewport' content='width=device-width, initial-scale=1.0'>" +
		"<title>" + html.EscapeString(o.title) + "</title>" +
		cssLinks.String() + xtermCSS + fontsLink +
		xtermJS + fitAddonJS +
		preViteTags.String() +
		viteTags +
		o.body + scriptTags.String() + "</html>"
}

// bootstrapTag renders the <script type="application/json" id="app-bootstrap">
// tag carrying the JSON payload the frontend reads on load. Port of
// _bootstrap_tag: it escapes every "</" as "<\/" so a payload value containing
// "</script>" cannot break out of the tag. HTML escaping in the JSON encoder is
// disabled so the "</" replacement (not <) matches Python byte-for-byte.
func bootstrapTag(payload map[string]any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	// Marshalling a map[string]any of JSON-native values never errors.
	_ = enc.Encode(payload)
	blob := strings.TrimRight(buf.String(), "\n")
	blob = strings.ReplaceAll(blob, "</", "<\\/")
	return "<script type='application/json' id='app-bootstrap'>" + blob + "</script>"
}

// standardBody renders the shared "<div id='app-root'>" body used by every page
// except the vanilla session surface.
func standardBody(bootstrap map[string]any) string {
	return "<body>" +
		"<div id='app-root'></div>" +
		"<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>" +
		bootstrapTag(bootstrap) +
		"</body>"
}

// operatorDashboardHTML renders the operator dashboard page. Port of
// operator_dashboard_html.
func (m *uiManifests) operatorDashboardHTML(title, appPath, assetsPath string, cdn pageCDN) string {
	bootstrap := map[string]any{
		"page_kind":   "dashboard",
		"title":       title,
		"app_path":    appPath,
		"assets_path": assetsPath,
	}
	return m.shell(shellOpts{title: title, assetsPath: assetsPath, body: standardBody(bootstrap), cdn: cdn})
}

// sessionPageHTML renders the user or operator session page. Port of
// session_page_html (operator selects page_kind operator/session + surface).
func (m *uiManifests) sessionPageHTML(title, assetsPath, sessionID string, operator bool, appPath string, shareRole any, cdn pageCDN) string {
	pageKind := "session"
	surface := "user"
	if operator {
		pageKind = "operator"
		surface = "operator"
	}
	bootstrap := map[string]any{
		"page_kind":   pageKind,
		"title":       title,
		"app_path":    appPath,
		"assets_path": assetsPath,
		"session_id":  sessionID,
		"surface":     surface,
		"share_role":  shareRole,
	}

	viteTags := m.viteEntryTags(assetsPath)
	appContainer := "<uterm-session id='app-root'></uterm-session>"
	var preVite []string
	if viteTags != "" {
		// React registers the custom elements itself; loading the vanilla hijack
		// entry too would re-run customElements.define() and throw.
		appContainer = "<div id='app-root'></div>"
	} else {
		preVite = []string{m.resolveVanillaAsset("src/hijack.ts")}
	}

	body := "<body>" +
		appContainer +
		"<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>" +
		bootstrapTag(bootstrap) +
		"</body>"
	return m.shell(shellOpts{title: title, assetsPath: assetsPath, body: body, preViteModules: preVite, cdn: cdn})
}

// connectPageHTML renders the quick-connect page. Port of connect_page_html.
func (m *uiManifests) connectPageHTML(title, assetsPath, appPath string, cdn pageCDN) string {
	bootstrap := map[string]any{
		"page_kind":   "connect",
		"title":       title,
		"app_path":    appPath,
		"assets_path": assetsPath,
	}
	return m.shell(shellOpts{title: title, assetsPath: assetsPath, body: standardBody(bootstrap), cdn: cdn})
}

// inspectPageHTML renders the HTTP-inspect page. Port of inspect_page_html.
func (m *uiManifests) inspectPageHTML(title, assetsPath, sessionID, appPath string, shareRole any, cdn pageCDN) string {
	bootstrap := map[string]any{
		"page_kind":   "inspect",
		"title":       title,
		"app_path":    appPath,
		"assets_path": assetsPath,
		"session_id":  sessionID,
		"surface":     "operator",
		"share_role":  shareRole,
	}
	return m.shell(shellOpts{title: title, assetsPath: assetsPath, body: standardBody(bootstrap), cdn: cdn})
}

// replayPageHTML renders the session-replay page. Port of replay_page_html —
// note the shell title is suffixed " Replay".
func (m *uiManifests) replayPageHTML(title, assetsPath, sessionID, appPath string, shareRole any, cdn pageCDN) string {
	bootstrap := map[string]any{
		"page_kind":   "replay",
		"title":       title,
		"app_path":    appPath,
		"assets_path": assetsPath,
		"session_id":  sessionID,
		"surface":     "operator",
		"share_role":  shareRole,
	}
	return m.shell(shellOpts{title: title + " Replay", assetsPath: assetsPath, body: standardBody(bootstrap), cdn: cdn})
}
