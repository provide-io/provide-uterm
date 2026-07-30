//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
)

// Delivery tuning. Ports the module constants in webhooks.py.

// webhookDeliverTimeout bounds one POST attempt (_DELIVER_TIMEOUT_S).
//
// Computed by a function rather than `const webhookDeliverTimeout = 5 *
// time.Second`: a package-level const/var initializer executes before any
// test runs and carries no coverage counter, so a mutation of the `*` there is
// permanently NOT_COVERED no matter how good the tests are. Wrapping the
// multiplication in a function gives it a coverage counter (it still runs
// exactly once, at package init) that TestWebhookDeliverTimingConstants can
// assert against.
var webhookDeliverTimeout = computeWebhookDeliverTimeout()

func computeWebhookDeliverTimeout() time.Duration { return 5 * time.Second }

// webhookMaxBlockedDeliveries is how many *consecutive* egress-guard blocks a
// webhook survives before it is unregistered (_MAX_BLOCKED_DELIVERIES).
// Re-resolution of a previously-safe name can legitimately start failing (that
// is what DNS rebinding looks like), but a destination that is permanently
// unsafe will never succeed, and re-evaluating it forever just burns CPU on an
// attacker's schedule.
const webhookMaxBlockedDeliveries = 3

// webhookRetryDelays ports _RETRY_DELAYS: three retries after the first
// attempt, then give up.
//
// Computed by a function for the same NOT_COVERED reason as
// webhookDeliverTimeout above: the two multiplications inside the literal
// slice need a coverage counter to mutation-test.
var webhookRetryDelays = computeWebhookRetryDelays()

func computeWebhookRetryDelays() []time.Duration {
	return []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second}
}

// WebhookOptions configures a [WebhookRegistry]. Every field is optional; the
// zero value yields the closed posture described on each field.
type WebhookOptions struct {
	// Guard resolves and classifies destinations. nil builds a default guard
	// (real resolver, wall clock).
	Guard *EgressGuard
	// EventBus is the bus deliveries are driven from. nil registers webhooks
	// that never fire — a graceful no-op matching the reference's
	// event_bus=None branch.
	EventBus *hub.EventBus
	// AllowLoopbackDestinations is the §3 *effective* permission, not the raw
	// config key: the caller ORs the key with is-loopback-bind before passing
	// it. false (the zero value) is the closed posture an embedder who has not
	// thought about egress should get, matching the reference's
	// allow_loopback_destinations: bool = False.
	AllowLoopbackDestinations bool
	// HasLiveTunnelShare answers §4: does this session hold an *unexpired*
	// tunnel share right now? nil reports "no share" — an embedder with no
	// tunnel feature has no shares, and refusing loopback delivery outright
	// would be a guard that fires on a condition that cannot occur.
	HasLiveTunnelShare func(sessionID string) bool
	// OnMetric receives counter increments. nil discards them.
	OnMetric func(name string, value int)
	// HTTPClient performs deliveries. nil uses a client bounded by
	// webhookDeliverTimeout.
	HTTPClient *http.Client
	// Now is the wall clock (seconds) stamped onto payloads and signatures.
	// nil uses the real clock.
	Now func() float64
	// Logger receives delivery diagnostics. nil uses the telemetry logger.
	Logger *slog.Logger
	// RetryDelays overrides webhookRetryDelays. A non-nil empty slice means
	// "one attempt, no retries" — which is how a test asserts the give-up path
	// without waiting 3.5 seconds for it.
	RetryDelays []time.Duration
}

// webhookEntry is one registered webhook plus its delivery-loop handle.
type webhookEntry struct {
	id         string
	sessionID  string
	url        string
	eventTypes []string // nil = every event type
	pattern    string   // "" = no text filter
	secret     string   // "" = unsigned

	// stop tears down the bus subscription and releases the delivery loop.
	stop func()
	// blocked counts *consecutive* egress-guard blocks (see
	// webhookMaxBlockedDeliveries). Guarded by WebhookRegistry.mu.
	blocked int
}

// WebhookRegistry is the in-memory session-webhook registry with one background
// delivery loop per webhook. Port of webhooks.WebhookManager, and the
// implementation of [WebhookManager] the production factory wires into
// [Deps.Webhooks].
//
// It is the enforcement point for conformance/EGRESS_GUARD.md: §1–§3 and §5 at
// registration (so a destination that can never be delivered to is refused with
// a 422 the caller can see), and §1–§5 again at *delivery*, because a name that
// was safe when it was registered is not necessarily safe when an event fires —
// that is exactly the DNS-rebinding case — and because §4's tunnel-share
// condition is not knowable until then.
//
// Safe for concurrent use.
type WebhookRegistry struct {
	guard       *EgressGuard
	bus         *hub.EventBus
	allowLB     bool
	liveShare   func(string) bool
	onMetric    func(string, int)
	client      *http.Client
	now         func() float64
	logger      *slog.Logger
	retryDelays []time.Duration

	mu      sync.Mutex
	entries map[string]*webhookEntry
	wg      sync.WaitGroup
}

// compile-time assertion that the concrete registry satisfies the HTTP layer's
// interface — the whole point of this type is that Deps.Webhooks can hold it.
var _ WebhookManager = (*WebhookRegistry)(nil)

// NewWebhookRegistry builds a registry from opts.
func NewWebhookRegistry(opts WebhookOptions) *WebhookRegistry {
	m := &WebhookRegistry{
		guard:       opts.Guard,
		bus:         opts.EventBus,
		allowLB:     opts.AllowLoopbackDestinations,
		liveShare:   opts.HasLiveTunnelShare,
		onMetric:    opts.OnMetric,
		client:      opts.HTTPClient,
		now:         opts.Now,
		logger:      opts.Logger,
		retryDelays: opts.RetryDelays,
		entries:     map[string]*webhookEntry{},
	}
	if m.guard == nil {
		m.guard = NewEgressGuard(nil, nil)
	}
	if m.client == nil {
		m.client = &http.Client{Timeout: webhookDeliverTimeout}
	}
	if m.now == nil {
		m.now = func() float64 { return float64(time.Now().UnixNano()) / 1e9 }
	}
	if m.logger == nil {
		m.logger = ptel.GetLogger(context.Background(), "provide.uterm.server.webhooks")
	}
	if m.retryDelays == nil {
		m.retryDelays = webhookRetryDelays
	}
	return m
}

// ValidateURL implements [WebhookManager]: refuse a destination the guard will
// not deliver to. Registration-time half of the egress contract.
func (m *WebhookRegistry) ValidateURL(url string) error {
	ctx, cancel := context.WithTimeout(context.Background(), webhookDeliverTimeout)
	defer cancel()
	_, err := m.guard.CheckWebhookDestination(ctx, url, m.allowLB)
	return err
}

// ValidatePattern implements [WebhookManager]: the pattern must be a regex the
// event bus will accept, so a webhook cannot smuggle in a filter that would be
// rejected only later, inside a delivery goroutine where nobody sees the error.
func (m *WebhookRegistry) ValidatePattern(pattern string) error {
	return hub.ValidateWatchPattern(pattern)
}

// Register implements [WebhookManager]. It re-validates rather than trusting the
// route to have done it: this is the only entry point, so it is the only place
// the invariant can be guaranteed.
func (m *WebhookRegistry) Register(
	sessionID, url string, eventTypes []string, pattern, secret string,
) (map[string]any, error) {
	if err := m.ValidateURL(url); err != nil {
		return nil, err
	}
	if pattern != "" {
		if err := m.ValidatePattern(pattern); err != nil {
			return nil, err
		}
	}
	e := &webhookEntry{
		id:         newWebhookID(),
		sessionID:  sessionID,
		url:        url,
		eventTypes: eventTypes,
		pattern:    pattern,
		secret:     secret,
	}
	if err := m.subscribe(e); err != nil {
		return nil, err
	}
	m.mu.Lock()
	m.entries[e.id] = e
	m.mu.Unlock()
	return webhookView(e), nil
}

// subscribe opens the bus subscription and starts the delivery loop.
//
// Deviation from the reference: the subscription is opened *synchronously*,
// before Register returns, where the Python version opens it inside the
// background task. An event published immediately after a successful
// registration is therefore never missed here — the caller has been told the
// webhook exists, so it has to be watching.
func (m *WebhookRegistry) subscribe(e *webhookEntry) error {
	if m.bus == nil {
		// No bus: the webhook is registered and inert. Matches the reference's
		// event_bus=None branch, where the delivery task returns immediately.
		e.stop = func() {}
		return nil
	}
	var patternPtr *string
	if e.pattern != "" {
		patternPtr = &e.pattern
	}
	sub, remove, err := m.bus.Watch(e.sessionID, e.eventTypes, patternPtr)
	if err != nil {
		return err
	}
	done := make(chan struct{})
	var once sync.Once
	e.stop = func() {
		once.Do(func() {
			close(done)
			remove()
		})
	}
	m.wg.Add(1)
	go m.deliveryLoop(e, sub, done)
	return nil
}

// deliveryLoop is the per-webhook background delivery task. Port of
// _delivery_loop: drain the subscription until the worker-disconnect sentinel
// (a nil event) or teardown.
func (m *WebhookRegistry) deliveryLoop(e *webhookEntry, sub *hub.Subscription, done <-chan struct{}) {
	defer m.wg.Done()
	for {
		select {
		case <-done:
			return
		case event := <-sub.Queue:
			if event == nil {
				return // worker-disconnected sentinel
			}
			m.deliver(e, event)
		}
	}
}

// deliver runs the delivery-time guard then POSTs. This is where §4 lives: the
// tunnel-share condition is a runtime fact (shares are issued by
// POST /api/tunnels while the server runs), so it cannot be folded into the
// load-time permission — at config load it is neither true nor false yet.
func (m *WebhookRegistry) deliver(e *webhookEntry, event map[string]any) {
	ctx, cancel := context.WithTimeout(context.Background(), webhookDeliverTimeout)
	loopback, err := m.guard.CheckWebhookDestination(ctx, e.url, m.allowLB)
	cancel()
	if err != nil {
		m.recordGuardBlock(e, err)
		return
	}
	// §4: a loopback destination is refused for a session that currently holds
	// an active tunnel share, even when §3 permits loopback. Tunnel sharing
	// exposes a loopback-bound server through a relay, so "bound to loopback"
	// has stopped implying "only local callers exist", and the assumption §3's
	// bind term rests on no longer holds for this session.
	if loopback && m.hasLiveTunnelShare(e.sessionID) {
		// A dedicated counter, not the generic block counter: this refusal is
		// not an SSRF-guard verdict on the destination and must not accumulate
		// toward the auto-unregister threshold. The share can be revoked, at
		// which point the webhook is expected to resume working — killing it
		// after three shared events would be a surprise the operator never
		// asked for.
		m.metric("webhook_delivery_blocked_tunnel_total", 1)
		m.logger.Warn("webhook_delivery_blocked",
			"webhook_id", e.id, "url", e.url, "session_id", e.sessionID,
			"reason", "loopback_destination_with_live_tunnel_share")
		return
	}
	// A guard pass clears the consecutive-block count, so a webhook that has
	// been intermittently safe is not killed by a stale tally.
	m.mu.Lock()
	e.blocked = 0
	m.mu.Unlock()
	m.post(e, event)
}

// recordGuardBlock counts an egress-guard refusal and auto-unregisters the
// webhook once the refusals stop looking transient.
func (m *WebhookRegistry) recordGuardBlock(e *webhookEntry, cause error) {
	m.mu.Lock()
	e.blocked++
	count := e.blocked
	m.mu.Unlock()
	m.metric("webhook_delivery_blocked_total", 1)
	m.logger.Warn("webhook_delivery_blocked",
		"webhook_id", e.id, "url", e.url, "reason", "unsafe_destination",
		"count", count, "error", cause)
	if count >= webhookMaxBlockedDeliveries {
		m.metric("webhook_auto_unregistered_total", 1)
		m.logger.Error("webhook_auto_unregistered",
			"webhook_id", e.id, "url", e.url, "reason", "ssrf_guard_threshold", "count", count)
		// Unregister never waits for the delivery loop, so calling it from
		// inside that loop is safe (the Python version has to schedule a task
		// to avoid awaiting itself).
		m.Unregister(e.id)
	}
}

// hasLiveTunnelShare answers §4 through the injected hook.
func (m *WebhookRegistry) hasLiveTunnelShare(sessionID string) bool {
	if m.liveShare == nil {
		return false
	}
	return m.liveShare(sessionID)
}

// post delivers one event with the ported retry schedule. Port of _deliver's
// send loop: attempt, then each retry delay, then give up loudly.
func (m *WebhookRegistry) post(e *webhookEntry, event map[string]any) {
	payload := map[string]any{
		"webhook_id": e.id,
		"session_id": e.sessionID,
		"event":      event,
		"timestamp":  m.now(),
	}
	body, err := json.Marshal(payload)
	if err != nil {
		// Only reachable if an event carries a value encoding/json refuses.
		m.logger.Warn("webhook_delivery_error", "webhook_id", e.id, "url", e.url, "error", err)
		return
	}
	header := http.Header{"Content-Type": []string{"application/json"}}
	if e.secret != "" {
		ts := strconv.FormatFloat(m.now(), 'f', -1, 64)
		header.Set("X-Uterm-Timestamp", ts)
		header.Set("X-Uterm-Signature", serverauth.BuildWebhookSignature(e.secret, body, ts))
	}
	for attempt := 0; ; attempt++ {
		if m.attempt(e, body, header, attempt) {
			return
		}
		if attempt >= len(m.retryDelays) {
			break
		}
		time.Sleep(m.retryDelays[attempt])
	}
	m.metric("webhook_delivery_giving_up_total", 1)
	m.logger.Error("webhook_delivery_giving_up", "webhook_id", e.id, "url", e.url)
}

// attempt performs one POST and reports whether it succeeded (2xx).
func (m *WebhookRegistry) attempt(e *webhookEntry, body []byte, header http.Header, attempt int) bool {
	ctx, cancel := context.WithTimeout(context.Background(), webhookDeliverTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.url, bytes.NewReader(body))
	if err != nil {
		m.logger.Warn("webhook_delivery_error",
			"webhook_id", e.id, "url", e.url, "error", err, "attempt", attempt+1)
		return false
	}
	req.Header = header.Clone()
	resp, err := m.client.Do(req)
	if err != nil {
		m.logger.Warn("webhook_delivery_error",
			"webhook_id", e.id, "url", e.url, "error", err, "attempt", attempt+1)
		return false
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true
	}
	m.metric("webhook_delivery_failed_total", 1)
	m.logger.Warn("webhook_delivery_failed",
		"webhook_id", e.id, "url", e.url, "status", resp.StatusCode, "attempt", attempt+1)
	return false
}

// ListWebhooks implements [WebhookManager].
func (m *WebhookRegistry) ListWebhooks(sessionID string) []map[string]any {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []map[string]any{}
	for _, e := range m.entries {
		if e.sessionID == sessionID {
			out = append(out, webhookView(e))
		}
	}
	return out
}

// GetWebhook implements [WebhookManager].
func (m *WebhookRegistry) GetWebhook(webhookID string) (map[string]any, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	e, ok := m.entries[webhookID]
	if !ok {
		return nil, false
	}
	return webhookView(e), true
}

// Unregister implements [WebhookManager]: drop the webhook and release its
// delivery loop. It does not wait for the loop to finish, so it is callable
// from inside that loop (see recordGuardBlock).
func (m *WebhookRegistry) Unregister(webhookID string) bool {
	m.mu.Lock()
	e, ok := m.entries[webhookID]
	if ok {
		delete(m.entries, webhookID)
	}
	m.mu.Unlock()
	if !ok {
		return false
	}
	e.stop()
	return true
}

// Shutdown releases every delivery loop and clears the registry, then waits for
// the loops to exit so a caller can be sure nothing will POST after it returns.
// Never call it from a delivery loop.
func (m *WebhookRegistry) Shutdown() {
	m.mu.Lock()
	entries := make([]*webhookEntry, 0, len(m.entries))
	for id, e := range m.entries {
		entries = append(entries, e)
		delete(m.entries, id)
	}
	m.mu.Unlock()
	for _, e := range entries {
		e.stop()
	}
	m.wg.Wait()
}

// metric increments a counter when a sink is wired.
func (m *WebhookRegistry) metric(name string, value int) {
	if m.onMetric != nil {
		m.onMetric(name, value)
	}
}

// webhookView renders the wire shape the routes return, matching the Python
// route's dict exactly: event_types is null (not []) when unfiltered.
func webhookView(e *webhookEntry) map[string]any {
	var eventTypes any
	if e.eventTypes != nil {
		eventTypes = e.eventTypes
	}
	var pattern any
	if e.pattern != "" {
		pattern = e.pattern
	}
	return map[string]any{
		"webhook_id":  e.id,
		"session_id":  e.sessionID,
		"url":         e.url,
		"event_types": eventTypes,
		"pattern":     pattern,
	}
}

// newWebhookID mints an opaque webhook id. Matches uuid4().hex in shape (32 hex
// chars) and in unguessability, which matters because the id is the only thing
// standing between a caller and someone else's webhook record.
func newWebhookID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil { // pragma: no cover — crypto/rand does not fail on supported platforms
		panic(errors.New("server: webhook id entropy unavailable"))
	}
	return hex.EncodeToString(b[:])
}

// LiveTunnelShare reports whether sessionID currently holds an unexpired tunnel
// share, and is the production answer to §4's "currently holds an active tunnel
// share".
//
// A share *is* the token record: POST /api/tunnels mints one keyed by the tunnel
// session's own id, DELETE .../tokens removes it, and POST .../tokens/rotate
// replaces it with a fresh expiry. So presence of the record is the share, and
// the expiry is read from the record rather than assumed — an expired share must
// not keep the guard closed (a token record outlives its expiry until the next
// sweep, so "the key exists" is not the same question as "a share is live now").
//
// now is passed in rather than read from a package-level clock so the caller can
// hand it the same clock the tunnel routes stamped ExpiresAt with; the two
// disagreeing is exactly how an expiry check goes quietly wrong.
func LiveTunnelShare(store tunnel.Store, sessionID string, now float64) bool {
	if store == nil {
		return false
	}
	rec, ok := store.GetToken(sessionID)
	return ok && rec.ExpiresAt > now
}
