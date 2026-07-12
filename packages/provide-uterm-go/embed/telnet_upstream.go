//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package embed

import (
	"context"
	"errors"
	"io"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// WireHook is optional diagnostic callback for IAC events.
type WireHook func(kind WireEventKind, data []byte, detail string)

// ConnectionTransportUpstream adapts transports.ConnectionTransport to UpstreamPipe.
type ConnectionTransportUpstream struct {
	tr      transports.ConnectionTransport
	host    string
	port    int
	opts    transports.ConnectOptions
	timeout time.Duration
	max     int
	mu      sync.Mutex
	ok      bool
}

// NewConnectionTransportUpstream wraps an existing connection transport.
func NewConnectionTransportUpstream(tr transports.ConnectionTransport, host string, port int, opts transports.ConnectOptions) *ConnectionTransportUpstream {
	return &ConnectionTransportUpstream{
		tr: tr, host: host, port: port, opts: opts,
		timeout: 30 * time.Second, max: 8192,
	}
}

// IsConnected implements UpstreamPipe.
func (u *ConnectionTransportUpstream) IsConnected() bool {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.ok && u.tr.IsConnected()
}

// Connect implements UpstreamPipe.
func (u *ConnectionTransportUpstream) Connect(ctx context.Context) error {
	if err := u.tr.Connect(ctx, u.host, u.port, u.opts); err != nil {
		return err
	}
	u.mu.Lock()
	u.ok = true
	u.mu.Unlock()
	return nil
}

// Disconnect implements UpstreamPipe.
func (u *ConnectionTransportUpstream) Disconnect(ctx context.Context) error {
	u.mu.Lock()
	u.ok = false
	u.mu.Unlock()
	return u.tr.Disconnect(ctx)
}

// Send implements UpstreamPipe.
func (u *ConnectionTransportUpstream) Send(ctx context.Context, data []byte) error {
	return u.tr.Send(ctx, data)
}

// Receive implements UpstreamPipe.
func (u *ConnectionTransportUpstream) Receive(ctx context.Context) ([]byte, error) {
	b, err := u.tr.Receive(ctx, u.max, u.timeout)
	if err != nil {
		if errors.Is(err, io.EOF) || errors.Is(err, transports.ErrConnectionClosed) || errors.Is(err, transports.ErrNotConnected) {
			u.mu.Lock()
			u.ok = false
			u.mu.Unlock()
			return nil, nil
		}
		return nil, err
	}
	return b, nil
}

// ScriptedTelnetUpstream feeds raw wire through IAC parse + policy (no TCP).
type ScriptedTelnetUpstream struct {
	policy TelnetPolicy
	onWire WireHook
	mu     sync.Mutex
	ok     bool
	sent   [][]byte
	in     chan []byte
	carry  []byte
}

// NewScriptedTelnetUpstream builds a deterministic telnet pipe for tests.
func NewScriptedTelnetUpstream(policy TelnetPolicy) *ScriptedTelnetUpstream {
	if policy == nil {
		policy = DefaultTelnetPolicy{}
	}
	return &ScriptedTelnetUpstream{
		policy: policy,
		in:     make(chan []byte, 32),
	}
}

// SetOnWire sets the diagnostic hook.
func (s *ScriptedTelnetUpstream) SetOnWire(h WireHook) { s.onWire = h }

// SentWire returns captured host→remote wire writes (policy replies + escaped app).
func (s *ScriptedTelnetUpstream) SentWire() [][]byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([][]byte, len(s.sent))
	copy(out, s.sent)
	return out
}

// IsConnected implements UpstreamPipe.
func (s *ScriptedTelnetUpstream) IsConnected() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ok
}

// Connect implements UpstreamPipe.
func (s *ScriptedTelnetUpstream) Connect(context.Context) error {
	s.mu.Lock()
	s.ok = true
	s.mu.Unlock()
	return nil
}

// Disconnect implements UpstreamPipe.
func (s *ScriptedTelnetUpstream) Disconnect(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ok {
		s.ok = false
		close(s.in)
	}
	return nil
}

// Send implements UpstreamPipe (IAC-escapes app payload).
func (s *ScriptedTelnetUpstream) Send(_ context.Context, data []byte) error {
	esc := transports.EscapeIAC(data)
	s.mu.Lock()
	s.sent = append(s.sent, append([]byte(nil), esc...))
	s.mu.Unlock()
	return nil
}

// PushWire injects raw remote wire bytes.
func (s *ScriptedTelnetUpstream) PushWire(data []byte) {
	s.in <- append([]byte(nil), data...)
}

// Receive implements UpstreamPipe.
func (s *ScriptedTelnetUpstream) Receive(ctx context.Context) ([]byte, error) {
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case chunk, ok := <-s.in:
			if !ok {
				return nil, nil
			}
			s.carry = append(s.carry, chunk...)
			payload, events, consumed := transports.ParseTelnetBuffer(s.carry, false)
			if consumed > 0 {
				s.carry = append([]byte(nil), s.carry[consumed:]...)
			}
			for _, ev := range events {
				s.handleEvent(ev)
			}
			if len(payload) > 0 {
				return payload, nil
			}
		}
	}
}

func (s *ScriptedTelnetUpstream) handleEvent(ev transports.TelnetEvent) {
	if s.onWire != nil {
		if ev.IsSubneg {
			s.onWire(WireNegotiation, ev.Payload, "sb")
		} else {
			s.onWire(WireIac, []byte{255, ev.Cmd, ev.Opt}, "neg")
		}
	}
	var reply []byte
	if ev.IsSubneg {
		opt := byte(0)
		body := ev.Payload
		if len(body) > 0 {
			opt = body[0]
			body = body[1:]
		}
		reply = s.policy.OnSubnegotiation(opt, body)
	} else {
		reply = s.policy.OnOption(ev.Cmd, ev.Opt)
	}
	if len(reply) > 0 {
		s.mu.Lock()
		s.sent = append(s.sent, append([]byte(nil), reply...))
		s.mu.Unlock()
	}
}
