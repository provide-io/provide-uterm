//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"strings"
	"testing"
)

func TestDispatchRouting(t *testing.T) {
	tests := []struct {
		name    string
		line    string
		wantSub string
	}{
		{"exit", "exit", "Goodbye"},
		{"quit", "quit", "Goodbye"},
		{"ctrl_d", "\x04", "Goodbye"},
		{"help", "help", "ushell commands"},
		{"help_case_insensitive", "HELP", "ushell commands"},
		{"help_whitespace", "  help  ", "ushell commands"},
		{"clear", "clear", "\x1b[2J"},
		{"unknown", "bogus", "unknown command"},
		{"help_kv", "help kv", "kv list"},
		{"help_py", "help py", "py"},
		{"help_bogus", "help bogus", "no help for"},
		{"py_no_arg", "py", "usage: py"},
		{"py_expr_stub", "py 2+2", "unavailable"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			d := newDispatcher(nil)
			got := dispatchText(t, d, tt.line)
			if !strings.Contains(got, tt.wantSub) {
				t.Fatalf("line %q → %q, want substring %q", tt.line, got, tt.wantSub)
			}
		})
	}
}

func TestDispatchEmptyAndCtrlC(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, ""); !strings.HasSuffix(got, " ") {
		t.Fatalf("empty → %q, want prompt (trailing space)", got)
	}
	r := d.Dispatch(context.Background(), "\x03")
	if len(r.Text) != 1 {
		t.Fatalf("ctrl-c frames = %v", r.Text)
	}
}

func TestDispatchEnvNoEnv(t *testing.T) {
	d := newDispatcher(&Context{Values: map[string]any{"my_key": "val"}})
	if got := dispatchText(t, d, "env"); !strings.Contains(got, "my_key") {
		t.Fatalf("env = %q", got)
	}
}

func TestDispatchEnvEmptyCtx(t *testing.T) {
	d := newDispatcher(&Context{})
	if got := dispatchText(t, d, "env"); !strings.Contains(got, "(empty context)") {
		t.Fatalf("env = %q", got)
	}
}

func TestDispatchEnvUnderscoreFiltered(t *testing.T) {
	d := newDispatcher(&Context{Values: map[string]any{"_hidden": 1}})
	if got := dispatchText(t, d, "env"); !strings.Contains(got, "(empty context)") {
		t.Fatalf("env = %q (underscore key should be filtered → empty)", got)
	}
}

func TestDispatchEnvWithEnvObject(t *testing.T) {
	env := &fakeEnv{attrs: map[string]string{"SESSION_REGISTRY": "object"}}
	d := newDispatcher(&Context{Env: env})
	got := dispatchText(t, d, "env")
	if !strings.Contains(got, "SESSION_REGISTRY") {
		t.Fatalf("env = %q", got)
	}
}

func TestDispatchEnvWithEnvNoPublicAttrs(t *testing.T) {
	env := &fakeEnv{attrs: map[string]string{}}
	d := newDispatcher(&Context{Env: env})
	if got := dispatchText(t, d, "env"); !strings.Contains(got, "(empty context)") {
		t.Fatalf("env = %q", got)
	}
}

func TestDispatchSessions(t *testing.T) {
	t.Run("no_list_fn", func(t *testing.T) {
		d := newDispatcher(nil)
		if got := dispatchText(t, d, "sessions"); !strings.Contains(got, "not available") {
			t.Fatalf("sessions = %q", got)
		}
	})
	t.Run("empty", func(t *testing.T) {
		d := newDispatcher(&Context{ListKVSessions: func(context.Context) ([]map[string]any, error) {
			return nil, nil
		}})
		if got := dispatchText(t, d, "sessions"); !strings.Contains(got, "no sessions") {
			t.Fatalf("sessions = %q", got)
		}
	})
	t.Run("with_data", func(t *testing.T) {
		d := newDispatcher(&Context{ListKVSessions: func(context.Context) ([]map[string]any, error) {
			return []map[string]any{
				{"session_id": "s1", "lifecycle_state": "running", "connector_type": "shell", "connected": true},
				{"session_id": "s2", "lifecycle_state": "idle", "connector_type": "telnet", "connected": false},
			}, nil
		}})
		got := dispatchText(t, d, "sessions")
		for _, want := range []string{"s1", "live", "idle"} {
			if !strings.Contains(got, want) {
				t.Fatalf("sessions %q missing %q", got, want)
			}
		}
	})
	t.Run("error", func(t *testing.T) {
		d := newDispatcher(&Context{ListKVSessions: func(context.Context) ([]map[string]any, error) {
			return nil, errString("kv error")
		}})
		if got := dispatchText(t, d, "sessions"); !strings.Contains(got, "kv error") {
			t.Fatalf("sessions = %q", got)
		}
	})
	t.Run("missing_fields", func(t *testing.T) {
		d := newDispatcher(&Context{ListKVSessions: func(context.Context) ([]map[string]any, error) {
			return []map[string]any{{}}, nil
		}})
		if got := dispatchText(t, d, "sessions"); !strings.Contains(got, "?") {
			t.Fatalf("sessions = %q", got)
		}
	})
}

func TestDispatchSessionsKill(t *testing.T) {
	t.Run("no_id", func(t *testing.T) {
		d := newDispatcher(nil)
		if got := dispatchText(t, d, "sessions kill"); !strings.Contains(got, "usage: sessions kill") {
			t.Fatalf("kill = %q", got)
		}
	})
	t.Run("no_binding", func(t *testing.T) {
		d := newDispatcher(nil)
		if got := dispatchText(t, d, "sessions kill sid1"); !strings.Contains(got, "not available") {
			t.Fatalf("kill = %q", got)
		}
	})
	t.Run("success", func(t *testing.T) {
		do := &fakeDO{}
		d := newDispatcher(&Context{Env: &fakeEnv{runtime: do}})
		if got := dispatchText(t, d, "sessions kill sid1"); !strings.Contains(got, "kill signal sent") {
			t.Fatalf("kill = %q", got)
		}
		if len(do.killed) != 1 || do.killed[0] != "sid1" {
			t.Fatalf("killed = %v", do.killed)
		}
	})
	t.Run("error", func(t *testing.T) {
		do := &fakeDO{err: errString("do error")}
		d := newDispatcher(&Context{Env: &fakeEnv{runtime: do}})
		if got := dispatchText(t, d, "sessions kill sid1"); !strings.Contains(got, "do error") {
			t.Fatalf("kill = %q", got)
		}
	})
}

// errString is a trivial error type for table tests.
type errString string

func (e errString) Error() string { return string(e) }
