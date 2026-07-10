//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import "github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"

// StrictSecurityDefaults ports security._STRICT_DEFAULTS.
var StrictSecurityDefaults = map[string]string{
	"Content-Security-Policy": "default-src 'self'; " +
		"script-src 'self' cdn.jsdelivr.net; " +
		"style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; " +
		"font-src fonts.gstatic.com; " +
		"connect-src 'self' ws: wss:; " +
		"img-src 'self' data:",
	"Strict-Transport-Security": "max-age=63072000; includeSubDomains",
	"X-Frame-Options":           "DENY",
	"X-Content-Type-Options":    "nosniff",
	"Referrer-Policy":           "strict-origin-when-cross-origin",
	"Permissions-Policy":        "camera=(), microphone=(), geolocation=()",
}

// DevSecurityDefaults ports security._DEV_DEFAULTS.
var DevSecurityDefaults = map[string]string{"X-Content-Type-Options": "nosniff"}

// securityFieldOrder mirrors _FIELD_TO_HEADER's insertion order (Python dicts
// preserve it, and _resolve_headers iterates in that order).
var securityFieldOrder = []struct {
	field  string
	header string
}{
	{"csp", "Content-Security-Policy"},
	{"hsts", "Strict-Transport-Security"},
	{"x_frame_options", "X-Frame-Options"},
	{"x_content_type_options", "X-Content-Type-Options"},
	{"referrer_policy", "Referrer-Policy"},
	{"permissions_policy", "Permissions-Policy"},
}

// HeaderPair is one (name, value) response-header entry.
type HeaderPair struct {
	Name  string
	Value string
}

func securityOverride(config *serverconfig.SecurityConfig, field string) *string {
	switch field {
	case "csp":
		return config.CSP
	case "hsts":
		return config.HSTS
	case "x_frame_options":
		return config.XFrameOptions
	case "x_content_type_options":
		return config.XContentTypeOptions
	case "referrer_policy":
		return config.ReferrerPolicy
	case "permissions_policy":
		return config.PermissionsPolicy
	}
	return nil
}

// ResolveSecurityHeaders ports security._resolve_headers: build the final
// header list, merging per-field overrides (nil = None → default; ""
// suppresses; non-empty overrides) with the mode defaults.
func ResolveSecurityHeaders(config *serverconfig.SecurityConfig) []HeaderPair {
	defaults := StrictSecurityDefaults
	if config.Mode != "strict" {
		defaults = DevSecurityDefaults
	}
	result := []HeaderPair{}
	for _, fh := range securityFieldOrder {
		override := securityOverride(config, fh.field)
		if override != nil {
			if *override != "" {
				result = append(result, HeaderPair{fh.header, *override})
			}
			continue
		}
		if v, ok := defaults[fh.header]; ok {
			result = append(result, HeaderPair{fh.header, v})
		}
	}
	return result
}
