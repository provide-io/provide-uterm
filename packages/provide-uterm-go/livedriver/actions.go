//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"context"
	"fmt"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// call is one client-library method, bound to a step's arguments and waiting
// for a context. Every action a scenario can name resolves to one of these, so
// what is under test is the library a consumer would actually use rather than a
// hand-rolled request that happens to agree with it.
type call func(ctx context.Context) (any, error)

// callFor binds the library method a step names. The error is a driver failure
// — an action nobody implements, or an argument the call cannot be made
// without — and ends the run rather than being reported as something a server
// did.
func callFor(hc *client.HijackClient, step Step) (call, error) {
	switch step.Action {
	case ActionHealth:
		return func(ctx context.Context) (any, error) { return hc.Health(ctx) }, nil
	case ActionListSessions:
		return func(ctx context.Context) (any, error) { return hc.ListSessions(ctx) }, nil
	case ActionGetSession, ActionSessionSnapshot, ActionSessionEvents, ActionSetInputMode:
		return sessionCall(hc, step)
	case ActionHijackAcquire, ActionHijackHeartbeat, ActionHijackSend,
		ActionHijackStep, ActionHijackSnapshot, ActionHijackRelease:
		return hijackCall(hc, step)
	default:
		return nil, fmt.Errorf("unknown action %q", step.Action)
	}
}

// sessionCall binds the actions that address a session by id.
func sessionCall(hc *client.HijackClient, step Step) (call, error) {
	if step.SessionID == "" {
		return nil, fmt.Errorf("action %s requires session_id", step.Action)
	}
	switch step.Action {
	case ActionGetSession:
		return func(ctx context.Context) (any, error) { return hc.GetSession(ctx, step.SessionID) }, nil
	case ActionSessionSnapshot:
		return func(ctx context.Context) (any, error) { return hc.SessionSnapshot(ctx, step.SessionID) }, nil
	case ActionSessionEvents:
		return func(ctx context.Context) (any, error) {
			return hc.SessionEvents(ctx, step.SessionID, step.limit())
		}, nil
	default: // ActionSetInputMode
		if step.InputMode == "" {
			return nil, fmt.Errorf("action %s requires input_mode", step.Action)
		}
		return func(ctx context.Context) (any, error) {
			return hc.SetSessionMode(ctx, step.SessionID, step.InputMode)
		}, nil
	}
}

// hijackCall binds the lease lifecycle. Every one of these needs a worker, and
// every one but the acquire needs the lease the acquire answered with — which
// is what the reference grammar exists to carry.
func hijackCall(hc *client.HijackClient, step Step) (call, error) {
	if step.WorkerID == "" {
		return nil, fmt.Errorf("action %s requires worker_id", step.Action)
	}
	if step.Action == ActionHijackAcquire {
		opts := client.AcquireOptions{Owner: step.owner(), LeaseS: step.leaseS()}
		return func(ctx context.Context) (any, error) { return hc.Acquire(ctx, step.WorkerID, opts) }, nil
	}
	if step.HijackID == "" {
		return nil, fmt.Errorf("action %s requires hijack_id", step.Action)
	}
	return leaseCall(hc, step), nil
}

// leaseCall binds the actions performed through an already-held lease.
func leaseCall(hc *client.HijackClient, step Step) call {
	worker, lease := step.WorkerID, step.HijackID
	switch step.Action {
	case ActionHijackHeartbeat:
		return func(ctx context.Context) (any, error) { return hc.Heartbeat(ctx, worker, lease, step.leaseS()) }
	case ActionHijackSend:
		return func(ctx context.Context) (any, error) {
			return hc.Send(ctx, worker, lease, client.SendOptions{Keys: step.Keys})
		}
	case ActionHijackStep:
		return func(ctx context.Context) (any, error) { return hc.Step(ctx, worker, lease) }
	case ActionHijackSnapshot:
		// A zero wait selects the library's own default, which is the wait the
		// reference driver's snapshot() takes when a step does not ask for one.
		return func(ctx context.Context) (any, error) { return hc.Snapshot(ctx, worker, lease, 0) }
	default: // ActionHijackRelease
		return func(ctx context.Context) (any, error) { return hc.Release(ctx, worker, lease) }
	}
}
