//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"net/http"
	"reflect"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// annotatingRegistry is the fake registry, keeping each annotation it is asked
// to record.
type annotatingRegistry struct {
	*fakeRegistry
	mu   sync.Mutex
	anns []Annotation
}

func (r *annotatingRegistry) AnnotateSession(_ context.Context, _ string, ann Annotation) (float64, int, error) {
	r.mu.Lock()
	r.anns = append(r.anns, ann)
	r.mu.Unlock()
	return 1.5, 99, nil
}

// An operator annotation is recorded through the registry and published on the
// hub's event ring — what /events and /events/watch serve — as the reference's
// annotate_session does, and the answer's seq is that event's.
func TestAnnotationIsPublishedAsAHubEvent(t *testing.T) {
	var reg *annotatingRegistry
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		reg = &annotatingRegistry{fakeRegistry: deps.Registry.(*fakeRegistry)}
		deps.Registry = reg
	})
	ts.reg.add("s1", "admin1", "public")
	ts.hub.Registry.Put("s1", hub.NewWorkerTermState())

	for want := 1; want <= 2; want++ {
		rec := ts.do("POST", "/api/sessions/s1/annotate", `{"label":" note ","description":"why","severity":"high"}`, adminHeaders())
		if rec.Code != http.StatusOK {
			t.Fatalf("annotate: %d %s", rec.Code, rec.Body.String())
		}
		if body := decode(t, rec.Body.Bytes()); body["seq"] != float64(want) {
			t.Fatalf("seq = %v, want the hub event's %d", body["seq"], want)
		}
	}
	events := ts.hub.GetRecentEvents(context.Background(), "s1", 10)
	want := map[string]any{"label": "note", "description": "why", "severity": "high", "source": "agent", "principal": "admin1"}
	if len(events) != 2 || events[0]["type"] != "annotation" || !reflect.DeepEqual(events[0]["data"], want) {
		t.Fatalf("hub events = %v", events)
	}
	reg.mu.Lock()
	defer reg.mu.Unlock()
	if len(reg.anns) != 2 || reg.anns[0] != (Annotation{Label: "note", Description: "why", Severity: "high", Principal: "admin1"}) {
		t.Fatalf("registry asked to record %v", reg.anns)
	}
}

// A recording that cannot be written is a server error, not "no runtime".
func TestAnnotationRecordingFailureIs500(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	ts.reg.annotateErr = errors.New("disk full")
	if rec := ts.do("POST", "/api/sessions/s1/annotate", `{"label":"x"}`, adminHeaders()); rec.Code != http.StatusInternalServerError {
		t.Fatalf("status %d, want 500", rec.Code)
	}
}
