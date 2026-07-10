//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bootstrap_test

import (
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/bootstrap"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

func TestNewSelectsBackend(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		cfg  cp.Config
		want string
	}{
		{"memory explicit", cp.Config{Backend: cp.BackendMemory}, "*memory.Engine"},
		{"sqlite explicit", cp.Config{Backend: cp.BackendSQLite, DatabaseURL: ":memory:"}, "*sqlite.Engine"},
		{"empty defaults to memory", cp.Config{}, "*memory.Engine"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			eng, err := bootstrap.New(tc.cfg)
			if err != nil {
				t.Fatalf("New: %v", err)
			}
			switch tc.want {
			case "*memory.Engine":
				if _, ok := eng.(*memory.Engine); !ok {
					t.Fatalf("expected *memory.Engine, got %T", eng)
				}
			case "*sqlite.Engine":
				if _, ok := eng.(*sqlite.Engine); !ok {
					t.Fatalf("expected *sqlite.Engine, got %T", eng)
				}
			}
		})
	}
}

func TestNewRejectsUnknownBackend(t *testing.T) {
	t.Parallel()
	_, err := bootstrap.New(cp.Config{Backend: "redis", DatabaseURL: "x"})
	if err == nil {
		t.Fatal("expected an error for an unknown backend")
	}
	var cpErr *cp.Error
	if !asError(err, &cpErr) || cpErr.Kind != "configuration" {
		t.Fatalf("expected a configuration Error, got %v", err)
	}
	if cpErr.Error() != "unsupported control-plane backend: redis" {
		t.Fatalf("unexpected message: %q", cpErr.Error())
	}
}

func asError(err error, target **cp.Error) bool {
	e, ok := err.(*cp.Error)
	if ok {
		*target = e
	}
	return ok
}
