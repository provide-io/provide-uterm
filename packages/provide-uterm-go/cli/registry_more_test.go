//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// manyEventsConnector overrides the fake connector's Events to return more
// entries than the requested limit, exercising the tail-slicing branch.
type manyEventsConnector struct{ *fakeConnector }

func (manyEventsConnector) Events() []map[string]any {
	return []map[string]any{{"n": 1}, {"n": 2}, {"n": 3}}
}

// TestRegistryLiveConnBranches covers the "live connector present" branches of
// SetMode/AnalyzeSession and the "no connector" branch of Events, none of which
// the existing suite reaches (it queries those before starting a session).
func TestRegistryLiveConnBranches(t *testing.T) {
	r := newTestRegistry(t)
	ctx := context.Background()

	// Events before any connector is started → the e.conn == nil early return.
	if ev, err := r.Events(ctx, "provide-shell", 10); err != nil || len(ev) != 0 {
		t.Fatalf("events pre-start = %v (err %v), want empty", ev, err)
	}

	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("start: %v", err)
	}

	// SetMode with a live connector forwards to conn.SetMode.
	st, err := r.SetMode(ctx, "provide-shell", "hijack")
	if err != nil || st.InputMode != "hijack" {
		t.Fatalf("setmode live = %+v (err %v)", st, err)
	}

	// AnalyzeSession with a live connector attaches the analysis object.
	a, err := r.AnalyzeSession(ctx, "provide-shell")
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if _, ok := a["analysis"]; !ok {
		t.Errorf("live analyze missing analysis key: %v", a)
	}
}

// TestRegistryEventsLimit covers the tail-slice branch of Events when the
// connector returns more events than the requested limit.
func TestRegistryEventsLimit(t *testing.T) {
	r := newTestRegistry(t)
	r.connect = func(context.Context, serverconfig.SessionDefinition) (connectors.Connector, error) {
		return manyEventsConnector{newFakeConnector()}, nil
	}
	ctx := context.Background()
	if _, err := r.StartSession(ctx, "provide-shell"); err != nil {
		t.Fatalf("start: %v", err)
	}
	ev, err := r.Events(ctx, "provide-shell", 2)
	if err != nil || len(ev) != 2 {
		t.Fatalf("events limit = %d (err %v), want 2", len(ev), err)
	}
}
