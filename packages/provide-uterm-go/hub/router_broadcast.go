//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// errNotBrowserSender marks a browser conn that cannot receive frames (does not
// implement [BrowserSender]); such a conn is treated as a dead socket.
var errNotBrowserSender = errors.New("browser conn does not implement BrowserSender")

// httpInspectControlTypes are the message types a tunnel worker receives on the
// HTTP side channel. Port of _HTTP_INSPECT_CONTROL_TYPES.
var httpInspectControlTypes = map[string]bool{
	"http_action":           true,
	"http_intercept_toggle": true,
	"http_inspect_toggle":   true,
}

type browserRole struct {
	ws   BrowserConn
	role string
}

// sendTimeout returns a context with the per-browser broadcast deadline.
func sendTimeout(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, time.Duration(broadcastSendTimeoutS*float64(time.Second)))
}

// sendToBrowser sends payload to ws under the broadcast deadline. A conn not
// implementing [BrowserSender] is a dead socket.
func sendToBrowser(ctx context.Context, ws BrowserConn, payload string) error {
	sender, ok := ws.(BrowserSender)
	if !ok {
		return errNotBrowserSender
	}
	sctx, cancel := sendTimeout(ctx)
	defer cancel()
	return sender.SendText(sctx, payload)
}

// Broadcast sends msg to all browsers registered for workerID. Port of
// router_broadcast.broadcast. Sends fan out concurrently for 2+ browsers so one
// slow viewer only consumes its own deadline; dead sockets are pruned and, if
// that changed hijack state, a fresh hijack_state is broadcast.
func (r *MessageRouter) Broadcast(ctx context.Context, workerID string, msg map[string]any) error {
	hub := r.hub
	hub.lock.Lock()
	st := hub.registry.Get(workerID)
	if st == nil {
		hub.lock.Unlock()
		return nil
	}
	var browsers []browserRole
	for ws, role := range st.Browsers {
		if !hub.startupPendingBrowsers[ws] {
			browsers = append(browsers, browserRole{ws, role})
		}
	}
	hub.lock.Unlock()

	encodedDefault, err := encodeBrowserFrame(msg)
	if err != nil {
		return err
	}

	gateActive := hub.outputPolicyGate != nil && contentEventTypes[str(msg["type"])]
	var payloadByRole map[string]string
	if gateActive {
		payloadByRole, err = r.payloadsByRole(ctx, workerID, msg, browsers, encodedDefault)
		if err != nil {
			return err
		}
	}

	payloadFor := func(br browserRole) string {
		if gateActive {
			return payloadByRole[br.role]
		}
		return encodedDefault
	}

	results := make([]error, len(browsers))
	if len(browsers) <= 1 {
		for i, br := range browsers {
			results[i] = sendToBrowser(ctx, br.ws, payloadFor(br))
		}
	} else {
		var wg sync.WaitGroup
		for i, br := range browsers {
			wg.Add(1)
			go func(i int, br browserRole, payload string) {
				defer wg.Done()
				results[i] = sendToBrowser(ctx, br.ws, payload)
			}(i, br, payloadFor(br))
		}
		wg.Wait()
	}

	var dead []BrowserConn
	for i, br := range browsers {
		if results[i] != nil {
			hub.logger.Debug("broadcast_send_failed", "worker_id", workerID, "error", results[i])
			dead = append(dead, br.ws)
		}
	}
	if len(dead) > 0 {
		changed, rerr := hub.RemoveDeadBrowsers(ctx, workerID, dead)
		if rerr != nil {
			return rerr
		}
		if changed {
			return r.BroadcastHijackState(ctx, workerID)
		}
	}
	return nil
}

// payloadsByRole pre-resolves the encoded payload for every distinct role once.
// Port of router_broadcast.payloads_by_role. When a role yields redaction rules
// and a [Redactor] is wired, its payload is the redacted encode; otherwise the
// shared default payload.
func (r *MessageRouter) payloadsByRole(
	ctx context.Context, workerID string, msg map[string]any, browsers []browserRole, encodedDefault string,
) (map[string]string, error) {
	hub := r.hub
	byRole := map[string]string{}
	for _, br := range browsers {
		if _, ok := byRole[br.role]; ok {
			continue
		}
		pc, err := hub.preparePolicyContext(ctx, br.ws, workerID, strp("output"))
		if err != nil {
			return nil, err
		}
		rules, err := hub.outputPolicyGate.GetRedactionRules(ctx, pc)
		if err != nil {
			return nil, err
		}
		if len(rules) > 0 && hub.redactor != nil {
			enc, err := encodeBrowserFrame(hub.redactor(msg, rules))
			if err != nil {
				return nil, err
			}
			byRole[br.role] = enc
		} else {
			byRole[br.role] = encodedDefault
		}
	}
	return byRole, nil
}

// hijackStateView bundles the fields needed to render a hijack_state frame.
type hijackStateView struct {
	isHijacked     bool
	isDashboard    bool
	isRest         bool
	hijackOwner    BrowserConn
	inputMode      string
	leaseExpiresAt *float64
}

// sendHijackStateTo sends a hijack_state frame to each browser and returns the
// dead sockets. Port of send_hijack_state_to.
func (r *MessageRouter) sendHijackStateTo(
	ctx context.Context, workerID string, browsers []BrowserConn, v hijackStateView, suppressErrors bool,
) ([]BrowserConn, error) {
	hub := r.hub
	var dead []BrowserConn
	for _, ws := range browsers {
		var owner *string
		switch {
		case v.isDashboard && v.hijackOwner == ws:
			owner = strp("me")
		case v.isDashboard || v.isRest:
			owner = strp("other")
		}
		frame := frames.MakeHijackStateFrame(v.isHijacked, owner, monoToWall(hub.clock, v.leaseExpiresAt), v.inputMode)
		m, err := frameToMap(frame)
		if err != nil {
			return nil, err
		}
		payload, err := encodeBrowserFrame(m)
		if err != nil {
			return nil, err
		}
		if serr := sendToBrowser(ctx, ws, payload); serr != nil {
			if !suppressErrors {
				hub.logger.Debug("broadcast_hijack_state_send_failed", "worker_id", workerID, "error", serr)
			}
			dead = append(dead, ws)
		}
	}
	return dead, nil
}

// viewFor reads the current hijack_state view for st under lock (caller holds lock).
func (r *MessageRouter) viewFor(st *WorkerTermState) hijackStateView {
	hub := r.hub
	isRest := hub.State.HasValidRESTLease(st)
	var lease *float64
	if isRest && st.HijackSession != nil {
		lease = f64p(st.HijackSession.LeaseExpiresAt)
	} else {
		lease = st.HijackOwnerExpiresAt
	}
	return hijackStateView{
		isHijacked:     hub.State.IsHijacked(st),
		isDashboard:    hub.State.IsDashboardHijackActive(st),
		isRest:         isRest,
		hijackOwner:    st.HijackOwner,
		inputMode:      st.InputMode,
		leaseExpiresAt: lease,
	}
}

// browsersFor returns the non-startup-pending browsers for st (caller holds lock).
func (r *MessageRouter) browsersFor(st *WorkerTermState) []BrowserConn {
	hub := r.hub
	var out []BrowserConn
	for ws := range st.Browsers {
		if !hub.startupPendingBrowsers[ws] {
			out = append(out, ws)
		}
	}
	return out
}

// BroadcastHijackState sends a hijack_state frame to every browser for
// workerID, pruning dead sockets and re-broadcasting to survivors. Port of
// broadcast_hijack_state.
func (r *MessageRouter) BroadcastHijackState(ctx context.Context, workerID string) error {
	hub := r.hub
	hub.lock.Lock()
	st := hub.registry.Get(workerID)
	if st == nil {
		hub.lock.Unlock()
		return nil
	}
	browsers := r.browsersFor(st)
	v := r.viewFor(st)
	hub.lock.Unlock()

	dead, err := r.sendHijackStateTo(ctx, workerID, browsers, v, false)
	if err != nil {
		return err
	}
	if len(dead) == 0 {
		return nil
	}
	if _, err := hub.RemoveDeadBrowsers(ctx, workerID, dead); err != nil {
		return err
	}
	hub.lock.Lock()
	st2 := hub.registry.Get(workerID)
	if st2 == nil {
		hub.lock.Unlock()
		return nil
	}
	survivors := r.browsersFor(st2)
	v2 := r.viewFor(st2)
	hub.lock.Unlock()
	_, err = r.sendHijackStateTo(ctx, workerID, survivors, v2, true)
	return err
}

// SendWorker sends msg to the worker WebSocket. Port of router_broadcast.send_worker.
// Returns (false, nil) when no worker is connected. Tunnel workers route input
// as raw PTY bytes and HTTP-inspect controls on the HTTP side channel (other
// types are dropped). source non-nil + an input frame records a keystroke.
func (r *MessageRouter) SendWorker(ctx context.Context, workerID string, msg map[string]any, source BrowserConn) (bool, error) {
	hub := r.hub
	if source != nil && str(msg["type"]) == "input" {
		r.RecordKeystroke(source)
	}

	hub.lock.Lock()
	st := hub.registry.Get(workerID)
	if st == nil || st.WorkerWS == nil {
		hub.lock.Unlock()
		return false, nil
	}
	ws := st.WorkerWS
	isTunnel := st.IsTunnelWorker
	hub.lock.Unlock()

	sendErr := r.deliverWorker(ctx, ws, isTunnel, msg)
	if sendErr == nil {
		return true, nil
	}
	if errors.Is(sendErr, errOwnedInputUnsupported) {
		return false, nil
	}
	hub.logger.Debug("send_worker_failed", "worker_id", workerID, "error", sendErr)
	hub.lock.Lock()
	st2 := hub.registry.Get(workerID)
	if st2 != nil && st2.WorkerWS == ws {
		st2.WorkerWS = nil
	}
	hub.lock.Unlock()
	if ctx.Err() != nil {
		// Cancellation propagates like Python's re-raised CancelledError.
		return false, sendErr
	}
	return false, nil
}

// deliverWorker performs the actual worker send, choosing the tunnel or
// text codec. Returns the send error (nil on success or a dropped message).
func (r *MessageRouter) deliverWorker(ctx context.Context, ws WorkerWS, isTunnel bool, msg map[string]any) error {
	if isTunnel {
		ts, ok := ws.(TunnelSender)
		if !ok {
			return errOwnedInputUnsupported
		}
		msgType := str(msg["type"])
		switch {
		case httpInspectControlTypes[msgType]:
			return ts.SendHTTPControl(ctx, msg)
		case msgType != "input":
			return errOwnedInputUnsupported
		default:
			data, ok := msg["data"].(string)
			if !ok {
				return errOwnedInputUnsupported
			}
			return ts.SendInput(ctx, data)
		}
	}
	payload, err := encodeWorkerFrame(msg)
	if err != nil {
		return err
	}
	return ws.SendText(ctx, payload)
}
