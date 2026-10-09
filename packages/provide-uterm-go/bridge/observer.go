//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

// Observer sees what each worker connection carries, at the points the
// reference's HostedSessionRuntime logs a recording from (runtime.py:
// _start_recording/_stop_recording around each connection attempt,
// _send_outbound_frame, _process_inbound and _process_control_msg).
//
// The reference runs its own socket loop, so those points are lines in one
// coroutine; here the socket belongs to TermBridge, and this interface is how
// the session that owns the bridge reaches them. Calls come from the bridge's
// run, send and receive goroutines, so an implementation must be safe for
// concurrent use; it must also not block, since it runs inline on the socket
// path.
type Observer interface {
	// AttemptStarted is called before each dial.
	AttemptStarted()
	// Connected is called once the socket is open, before any frame flows.
	Connected()
	// FrameSent is called after a frame has been written. payload is the
	// encoded wire text; frame is the message: a control frame as sent, or
	// {"type": "term", "data": ...} for terminal output.
	FrameSent(payload string, frame map[string]any)
	// WireReceived is called with each inbound message, as the text fed to
	// the control-channel decoder, before it is decoded.
	WireReceived(text string)
	// ControlReceived is called with each decoded inbound control message,
	// before it is dispatched.
	ControlReceived(msg map[string]any)
	// InputReceived is called with each decoded inbound data chunk (input for
	// the terminal), before it is delivered.
	InputReceived(data string)
	// AttemptEnded is called when an attempt is over: err is the dial error,
	// or the error that ended a served connection (a failed read or write, or
	// a stream the decoder rejected); nil when the bridge is stopping.
	AttemptEnded(err error)
}

// nopObserver is the Observer of a bridge configured without one.
type nopObserver struct{}

func (nopObserver) AttemptStarted()                  {}
func (nopObserver) Connected()                       {}
func (nopObserver) FrameSent(string, map[string]any) {}
func (nopObserver) WireReceived(string)              {}
func (nopObserver) ControlReceived(map[string]any)   {}
func (nopObserver) InputReceived(string)             {}
func (nopObserver) AttemptEnded(error)               {}

// observedFrame is the message form of a queued frame, as Observer.FrameSent
// reports it.
func observedFrame(f queuedFrame) map[string]any {
	if f.isTerm {
		return map[string]any{"type": "term", "data": f.data}
	}
	return f.control
}
