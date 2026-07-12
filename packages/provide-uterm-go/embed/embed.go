//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package embed provides an in-process multi-client proxy session API.
// Hosts (protocol-aware proxies) attach interceptors and clients without CLI/HTTP.
package embed

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
)

// SessionLifecycle phases for an embedded session.
type SessionLifecycle int

const (
	LifecycleCreated SessionLifecycle = iota
	LifecycleConnecting
	LifecycleNegotiated
	LifecycleConnected
	LifecycleUpstreamLost
	LifecycleReconnecting
	LifecycleClientAttached
	LifecycleShutdown
)

// InterceptAction outcomes for the byte pipeline.
type InterceptAction int

const (
	ActionPass InterceptAction = iota
	ActionReplace
	ActionConsume
	ActionDefer
	ActionInject
)

// BackpressurePolicy for per-client queues (upstream is never blocked).
type BackpressurePolicy int

const (
	BackpressureDropOldest BackpressurePolicy = iota
	BackpressureDropNewest
	BackpressureDisconnect
)

// ByteDirection of a pipeline unit.
type ByteDirection int

const (
	DirUpstreamToApp ByteDirection = iota
	DirClientToUpstream
)

// WireEventKind for diagnostic wire events.
type WireEventKind int

const (
	WireIac WireEventKind = iota
	WireNegotiation
	WireDiagnostic
)

// InterceptResult from an interceptor.
type InterceptResult struct {
	Action  InterceptAction
	Payload []byte
}

func Pass() InterceptResult    { return InterceptResult{Action: ActionPass} }
func Consume() InterceptResult { return InterceptResult{Action: ActionConsume} }
func Defer() InterceptResult   { return InterceptResult{Action: ActionDefer} }
func Replace(p []byte) InterceptResult {
	return InterceptResult{Action: ActionReplace, Payload: append([]byte(nil), p...)}
}
func Inject(p []byte) InterceptResult {
	return InterceptResult{Action: ActionInject, Payload: append([]byte(nil), p...)}
}

// ClientMetadata tags for selective fan-out.
type ClientMetadata struct {
	ClientID       string
	Tags           map[string]struct{}
	Attributes     map[string]string
	Backpressure   BackpressurePolicy
	QueueCapacity  int
}

// ClientFilter selects clients for SendToClients.
type ClientFilter struct {
	RequireAnyTag []string
	ExcludeTags   []string
	Predicate     func(ClientMetadata) bool
}

// Matches reports whether meta satisfies the filter.
func (f ClientFilter) Matches(meta ClientMetadata) bool {
	for _, t := range f.ExcludeTags {
		if _, ok := meta.Tags[t]; ok {
			return false
		}
	}
	if len(f.RequireAnyTag) > 0 {
		any := false
		for _, t := range f.RequireAnyTag {
			if _, ok := meta.Tags[t]; ok {
				any = true
				break
			}
		}
		if !any {
			return false
		}
	}
	if f.Predicate != nil && !f.Predicate(meta) {
		return false
	}
	return true
}

// UpstreamPipe is the game/remote byte pipe (application bytes).
type UpstreamPipe interface {
	IsConnected() bool
	Connect(ctx context.Context) error
	Disconnect(ctx context.Context) error
	Send(ctx context.Context, data []byte) error
	// Receive returns application bytes; empty slice means clean EOF.
	Receive(ctx context.Context) ([]byte, error)
}

// ByteInterceptor handles both directions.
type ByteInterceptor interface {
	OnUpstream(ctx context.Context, c InterceptContext) (InterceptResult, error)
	OnClient(ctx context.Context, c InterceptContext) (InterceptResult, error)
}

// InterceptContext for interceptor callbacks.
type InterceptContext struct {
	Session   *Session
	Direction ByteDirection
	Data      []byte
	ClientID  string
}

// PassThrough is the default interceptor.
type PassThrough struct{}

func (PassThrough) OnUpstream(context.Context, InterceptContext) (InterceptResult, error) {
	return Pass(), nil
}
func (PassThrough) OnClient(context.Context, InterceptContext) (InterceptResult, error) {
	return Pass(), nil
}

// TelnetPolicy supplies host answers for IAC mechanics.
type TelnetPolicy interface {
	TerminalType() string
	WindowSize() (cols, rows int)
	OnOption(command, option byte) []byte
	OnSubnegotiation(option byte, body []byte) []byte
}

// DefaultTelnetPolicy is ANSI 80×25 with minimal option answers.
type DefaultTelnetPolicy struct {
	Term string
	Cols int
	Rows int
}

func (p DefaultTelnetPolicy) TerminalType() string {
	if p.Term == "" {
		return "ANSI"
	}
	return p.Term
}
func (p DefaultTelnetPolicy) WindowSize() (int, int) {
	c, r := p.Cols, p.Rows
	if c <= 0 {
		c = 80
	}
	if r <= 0 {
		r = 25
	}
	return c, r
}
func (p DefaultTelnetPolicy) OnOption(command, option byte) []byte {
	const iac, will, wont, doCmd, dont = 255, 251, 252, 253, 254
	switch command {
	case doCmd:
		return []byte{iac, will, option}
	case will:
		return []byte{iac, doCmd, option}
	case wont:
		return []byte{iac, dont, option}
	case dont:
		return []byte{iac, wont, option}
	default:
		return nil
	}
}
func (p DefaultTelnetPolicy) OnSubnegotiation(option byte, body []byte) []byte {
	const iac, sb, se = 255, 250, 240
	if option == 24 && len(body) > 0 && body[0] == 1 {
		term := []byte(p.TerminalType())
		out := make([]byte, 0, 4+len(term)+2)
		out = append(out, iac, sb, 24, 0)
		out = append(out, term...)
		out = append(out, iac, se)
		return out
	}
	if option == 31 {
		c, r := p.WindowSize()
		return []byte{iac, sb, 31, byte(c >> 8), byte(c), byte(r >> 8), byte(r), iac, se}
	}
	return nil
}

// Options for CreateSession.
type Options struct {
	SessionID   string
	Interceptor ByteInterceptor
	Telnet      TelnetPolicy
	Services    map[string]any
}

// Hub creates and tracks sessions.
type Hub struct {
	mu       sync.Mutex
	sessions map[string]*Session
	seq      atomic.Uint64
}

// NewHub returns an empty embed hub.
func NewHub() *Hub {
	return &Hub{sessions: map[string]*Session{}}
}

// CreateSession registers a new session.
func (h *Hub) CreateSession(opts Options) (*Session, error) {
	id := opts.SessionID
	if id == "" {
		id = fmt.Sprintf("embed-%x", h.seq.Add(1))
	}
	s := newSession(id, opts)
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.sessions[id]; ok {
		return nil, fmt.Errorf("session already exists: %s", id)
	}
	h.sessions[id] = s
	return s, nil
}

// GetSession returns a session by id.
func (h *Hub) GetSession(id string) *Session {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.sessions[id]
}

// RemoveSession drops a session from the registry.
func (h *Hub) RemoveSession(id string) {
	h.mu.Lock()
	delete(h.sessions, id)
	h.mu.Unlock()
}

// SessionIDs lists registered ids.
func (h *Hub) SessionIDs() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]string, 0, len(h.sessions))
	for k := range h.sessions {
		out = append(out, k)
	}
	return out
}

type deferredItem struct {
	dir      ByteDirection
	data     []byte
	clientID string
}

type clientSlot struct {
	meta   ClientMetadata
	ch     chan []byte
	cap    int
	closed atomic.Bool
}

// Session is the ordered multi-client proxy session.
type Session struct {
	id       string
	services map[string]any
	inter    ByteInterceptor
	telnet   TelnetPolicy

	mu        sync.Mutex
	lifecycle SessionLifecycle
	upstream  UpstreamPipe
	clients   map[string]*clientSlot
	deferred  []deferredItem
	cancel    context.CancelFunc
	readerDone chan struct{}
	depth     int // re-entrancy depth while holding mu during pipeline work

	onApp      func(dir ByteDirection, data []byte, clientID string)
	onClient   func(data []byte, clientID string)
	onWire     func(kind WireEventKind, data []byte, detail string)
	onLife     func(SessionLifecycle, string)
}

func newSession(id string, opts Options) *Session {
	inter := opts.Interceptor
	if inter == nil {
		inter = PassThrough{}
	}
	tel := opts.Telnet
	if tel == nil {
		tel = DefaultTelnetPolicy{}
	}
	svc := map[string]any{}
	for k, v := range opts.Services {
		svc[k] = v
	}
	svc["telnet_policy"] = tel
	return &Session{
		id:        id,
		services:  svc,
		inter:     inter,
		telnet:    tel,
		lifecycle: LifecycleCreated,
		clients:   map[string]*clientSlot{},
	}
}

// SessionID returns the id.
func (s *Session) SessionID() string { return s.id }

// Lifecycle returns the current phase.
func (s *Session) Lifecycle() SessionLifecycle {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lifecycle
}

// Services returns the session-scoped bag.
func (s *Session) Services() map[string]any { return s.services }

// OnApplicationData sets the app-byte observer.
func (s *Session) OnApplicationData(fn func(ByteDirection, []byte, string)) { s.onApp = fn }

// OnClientData sets the client-byte observer.
func (s *Session) OnClientData(fn func([]byte, string)) { s.onClient = fn }

// OnWire sets the wire-event observer.
func (s *Session) OnWire(fn func(WireEventKind, []byte, string)) { s.onWire = fn }

// OnLifecycle sets the lifecycle observer.
func (s *Session) OnLifecycle(fn func(SessionLifecycle, string)) { s.onLife = fn }

func (s *Session) setLife(phase SessionLifecycle, detail string) {
	s.lifecycle = phase
	if s.onLife != nil {
		s.onLife(phase, detail)
	}
}

// ConnectUpstream attaches and starts reading the upstream pipe.
func (s *Session) ConnectUpstream(ctx context.Context, up UpstreamPipe) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.setLife(LifecycleConnecting, "")
	if err := up.Connect(ctx); err != nil {
		return err
	}
	s.upstream = up
	s.startReaderLocked()
	s.setLife(LifecycleConnected, "")
	return nil
}

// ReplaceUpstream swaps the upstream while keeping clients.
func (s *Session) ReplaceUpstream(ctx context.Context, up UpstreamPipe) error {
	s.mu.Lock()
	s.setLife(LifecycleReconnecting, "")
	cancel, done := s.detachReaderLocked()
	old := s.upstream
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}
	if old != nil {
		_ = old.Disconnect(ctx)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.setLife(LifecycleConnecting, "")
	if err := up.Connect(ctx); err != nil {
		return err
	}
	s.upstream = up
	s.startReaderLocked()
	s.setLife(LifecycleConnected, "")
	return nil
}

// MarkNegotiated records telnet negotiation complete.
func (s *Session) MarkNegotiated() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.setLife(LifecycleNegotiated, "")
	if s.upstream != nil && s.upstream.IsConnected() {
		s.setLife(LifecycleConnected, "")
	}
}

// AttachClient registers an in-process client handle.
func (s *Session) AttachClient(meta ClientMetadata) (*ClientHandle, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if meta.ClientID == "" {
		return nil, errors.New("ClientID required")
	}
	if _, ok := s.clients[meta.ClientID]; ok {
		return nil, fmt.Errorf("client already attached: %s", meta.ClientID)
	}
	if meta.Tags == nil {
		meta.Tags = map[string]struct{}{}
	}
	capN := meta.QueueCapacity
	if capN <= 0 {
		capN = 64
	}
	slot := &clientSlot{meta: meta, ch: make(chan []byte, capN), cap: capN}
	s.clients[meta.ClientID] = slot
	s.setLife(LifecycleClientAttached, meta.ClientID)
	return &ClientHandle{slot: slot, id: meta.ClientID}, nil
}

// SendToUpstream runs client→upstream pipeline (host/script origin).
func (s *Session) SendToUpstream(ctx context.Context, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.processClientLocked(ctx, append([]byte(nil), data...), "")
}

// SendFromInterceptor runs client→upstream while already inside a pipeline
// callback (session mutex held). Must only be called from ByteInterceptor methods.
func (s *Session) SendFromInterceptor(ctx context.Context, data []byte) error {
	if s.depth == 0 {
		return errors.New("SendFromInterceptor outside interceptor")
	}
	return s.processClientLocked(ctx, append([]byte(nil), data...), "")
}

// SendToClients fans out application bytes with optional filter.
func (s *Session) SendToClients(data []byte, filter *ClientFilter) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := ClientFilter{}
	if filter != nil {
		f = *filter
	}
	cp := append([]byte(nil), data...)
	s.deliverLocked(cp, f)
	if s.onApp != nil {
		s.onApp(DirUpstreamToApp, cp, "")
	}
}

// FlushDeferred processes deferred units in order.
func (s *Session) FlushDeferred(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for len(s.deferred) > 0 {
		item := s.deferred[0]
		s.deferred = s.deferred[1:]
		var err error
		if item.dir == DirUpstreamToApp {
			err = s.processUpstreamLocked(ctx, item.data, true)
		} else {
			err = s.processClientLocked(ctx, item.data, item.clientID)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// RaiseWire emits a diagnostic wire event.
func (s *Session) RaiseWire(kind WireEventKind, data []byte, detail string) {
	if s.onWire != nil {
		s.onWire(kind, append([]byte(nil), data...), detail)
	}
}

// Close shuts down the session.
func (s *Session) Close(ctx context.Context) error {
	s.mu.Lock()
	cancel, done := s.detachReaderLocked()
	up := s.upstream
	s.upstream = nil
	clients := s.clients
	s.clients = map[string]*clientSlot{}
	s.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}
	if up != nil {
		_ = up.Disconnect(ctx)
	}
	for _, c := range clients {
		c.closed.Store(true)
		close(c.ch)
	}
	s.mu.Lock()
	s.setLife(LifecycleShutdown, "")
	s.mu.Unlock()
	return nil
}

func (s *Session) startReaderLocked() {
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.readerDone = make(chan struct{})
	up := s.upstream
	done := s.readerDone
	go func() {
		defer close(done)
		for {
			chunk, err := up.Receive(ctx)
			if err != nil || len(chunk) == 0 {
				s.mu.Lock()
				if s.lifecycle != LifecycleShutdown && s.lifecycle != LifecycleReconnecting {
					s.setLife(LifecycleUpstreamLost, "")
				}
				s.mu.Unlock()
				return
			}
			s.mu.Lock()
			_ = s.processUpstreamLocked(ctx, chunk, false)
			s.mu.Unlock()
		}
	}()
}

// detachReaderLocked clears reader fields; caller must cancel+wait outside the lock.
func (s *Session) detachReaderLocked() (context.CancelFunc, chan struct{}) {
	cancel := s.cancel
	done := s.readerDone
	s.cancel = nil
	s.readerDone = nil
	return cancel, done
}

func (s *Session) processUpstreamLocked(ctx context.Context, data []byte, fromDefer bool) error {
	s.depth++
	defer func() { s.depth-- }()
	res, err := s.inter.OnUpstream(ctx, InterceptContext{Session: s, Direction: DirUpstreamToApp, Data: data})
	if err != nil {
		return err
	}
	return s.applyLocked(ctx, res, data, DirUpstreamToApp, "", fromDefer)
}

func (s *Session) processClientLocked(ctx context.Context, data []byte, clientID string) error {
	s.depth++
	defer func() { s.depth-- }()
	if s.onClient != nil {
		s.onClient(data, clientID)
	}
	res, err := s.inter.OnClient(ctx, InterceptContext{Session: s, Direction: DirClientToUpstream, Data: data, ClientID: clientID})
	if err != nil {
		return err
	}
	return s.applyLocked(ctx, res, data, DirClientToUpstream, clientID, false)
}

func (s *Session) applyLocked(ctx context.Context, res InterceptResult, original []byte, dir ByteDirection, clientID string, fromDefer bool) error {
	switch res.Action {
	case ActionPass:
		return s.forwardLocked(ctx, original, dir)
	case ActionReplace:
		return s.forwardLocked(ctx, res.Payload, dir)
	case ActionConsume:
		return nil
	case ActionDefer:
		if !fromDefer {
			s.deferred = append(s.deferred, deferredItem{dir: dir, data: append([]byte(nil), original...), clientID: clientID})
		}
		return nil
	case ActionInject:
		if dir == DirUpstreamToApp {
			return s.processUpstreamLocked(ctx, res.Payload, false)
		}
		return s.processClientLocked(ctx, res.Payload, clientID)
	default:
		return s.forwardLocked(ctx, original, dir)
	}
}

func (s *Session) forwardLocked(ctx context.Context, data []byte, dir ByteDirection) error {
	if len(data) == 0 {
		return nil
	}
	if dir == DirUpstreamToApp {
		s.deliverLocked(data, ClientFilter{})
		if s.onApp != nil {
			s.onApp(dir, data, "")
		}
		return nil
	}
	if s.upstream == nil || !s.upstream.IsConnected() {
		return errors.New("upstream not connected")
	}
	return s.upstream.Send(ctx, data)
}

func (s *Session) deliverLocked(data []byte, filter ClientFilter) {
	var drop []string
	for id, slot := range s.clients {
		if !filter.Matches(slot.meta) {
			continue
		}
		if !slot.tryEnqueue(data) {
			if slot.meta.Backpressure == BackpressureDisconnect {
				drop = append(drop, id)
			}
		}
	}
	for _, id := range drop {
		if slot, ok := s.clients[id]; ok {
			slot.closed.Store(true)
			close(slot.ch)
			delete(s.clients, id)
		}
	}
}

func (c *clientSlot) tryEnqueue(data []byte) bool {
	cp := append([]byte(nil), data...)
	if len(c.ch) >= c.cap {
		switch c.meta.Backpressure {
		case BackpressureDropNewest, BackpressureDisconnect:
			return false
		default: // DropOldest
			select {
			case <-c.ch:
			default:
			}
		}
	}
	select {
	case c.ch <- cp:
		return true
	default:
		return false
	}
}

// ClientHandle is an in-process client attachment.
type ClientHandle struct {
	slot *clientSlot
	id   string
}

// ClientID returns the id.
func (h *ClientHandle) ClientID() string { return h.id }

// Meta returns client metadata.
func (h *ClientHandle) Meta() ClientMetadata { return h.slot.meta }

// Receive waits for the next fan-out chunk.
func (h *ClientHandle) Receive(ctx context.Context) ([]byte, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case b, ok := <-h.slot.ch:
		if !ok {
			return nil, errors.New("client closed")
		}
		return b, nil
	}
}

// MemoryUpstream is a deterministic test upstream.
type MemoryUpstream struct {
	mu        sync.Mutex
	connected bool
	closed    bool
	sent      [][]byte
	inbound   chan []byte
}

// NewMemoryUpstream builds a test duplex.
func NewMemoryUpstream() *MemoryUpstream {
	return &MemoryUpstream{inbound: make(chan []byte, 64)}
}

// IsConnected implements UpstreamPipe.
func (m *MemoryUpstream) IsConnected() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.connected && !m.closed
}

// Connect implements UpstreamPipe.
func (m *MemoryUpstream) Connect(context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.connected = true
	return nil
}

// Disconnect implements UpstreamPipe.
func (m *MemoryUpstream) Disconnect(context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.closed {
		m.closed = true
		close(m.inbound)
	}
	m.connected = false
	return nil
}

// Send implements UpstreamPipe.
func (m *MemoryUpstream) Send(_ context.Context, data []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.connected || m.closed {
		return errors.New("not connected")
	}
	m.sent = append(m.sent, append([]byte(nil), data...))
	return nil
}

// Receive implements UpstreamPipe.
func (m *MemoryUpstream) Receive(ctx context.Context) ([]byte, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case b, ok := <-m.inbound:
		if !ok {
			return nil, nil
		}
		return b, nil
	}
}

// PushFromRemote simulates remote→host application bytes.
func (m *MemoryUpstream) PushFromRemote(data []byte) error {
	m.mu.Lock()
	closed := m.closed
	m.mu.Unlock()
	if closed {
		return errors.New("upstream closed")
	}
	m.inbound <- append([]byte(nil), data...)
	return nil
}

// Sent returns host→remote captures.
func (m *MemoryUpstream) Sent() [][]byte {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([][]byte, len(m.sent))
	copy(out, m.sent)
	return out
}

// CompleteRemote signals EOF.
func (m *MemoryUpstream) CompleteRemote() {
	_ = m.Disconnect(context.Background())
}
