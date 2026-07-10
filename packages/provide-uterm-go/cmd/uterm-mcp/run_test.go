//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package main

import (
	"bytes"
	"testing"
)

func TestParseConfigDefaults(t *testing.T) {
	cfg, err := parseConfig([]string{"--url", "http://localhost:8780"}, &bytes.Buffer{})
	if err != nil {
		t.Fatalf("parseConfig: %v", err)
	}
	if cfg.BaseURL != "http://localhost:8780" || cfg.EntityPrefix != "/worker" || cfg.DefaultRole != "operator" {
		t.Fatalf("defaults wrong: %#v", cfg)
	}
	if cfg.Headers != nil {
		t.Fatalf("no headers expected, got %#v", cfg.Headers)
	}
}

func TestParseConfigHeadersAndRole(t *testing.T) {
	cfg, err := parseConfig([]string{
		"--url", "http://h", "--entity-prefix", "/agent", "--role", "admin",
		"--header", "Authorization: Bearer tok", "--header", "X-Extra:v",
	}, &bytes.Buffer{})
	if err != nil {
		t.Fatalf("parseConfig: %v", err)
	}
	if cfg.EntityPrefix != "/agent" || cfg.DefaultRole != "admin" {
		t.Fatalf("flags wrong: %#v", cfg)
	}
	if cfg.Headers["Authorization"] != "Bearer tok" || cfg.Headers["X-Extra"] != "v" {
		t.Fatalf("headers wrong: %#v", cfg.Headers)
	}
}

func TestParseConfigRequiresURL(t *testing.T) {
	if _, err := parseConfig([]string{"--role", "admin"}, &bytes.Buffer{}); err == nil {
		t.Fatalf("missing --url must error")
	}
}

func TestParseConfigRejectsBadRole(t *testing.T) {
	if _, err := parseConfig([]string{"--url", "http://h", "--role", "root"}, &bytes.Buffer{}); err == nil {
		t.Fatalf("bad role must error")
	}
}

func TestParseConfigRejectsUnknownFlag(t *testing.T) {
	if _, err := parseConfig([]string{"--url", "http://h", "--nope"}, &bytes.Buffer{}); err == nil {
		t.Fatalf("unknown flag must error")
	}
}

func TestParseConfigSkipsEmptyHeaderKey(t *testing.T) {
	cfg, err := parseConfig([]string{"--url", "http://h", "--header", ": novalue"}, &bytes.Buffer{})
	if err != nil {
		t.Fatalf("parseConfig: %v", err)
	}
	if cfg.Headers != nil {
		t.Fatalf("empty header key should be skipped: %#v", cfg.Headers)
	}
}
