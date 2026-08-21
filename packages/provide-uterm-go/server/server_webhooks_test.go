//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
)

// counters is a metric sink that records increments for assertions.
type counters struct {
	mu     sync.Mutex
	values map[string]int
}

func newCounters() *counters { return &counters{values: map[string]int{}} }

func (c *counters) inc(name string, value int) {
	c.mu.Lock()
	c.values[name] += value
	c.mu.Unlock()
}

func (c *counters) get(name string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.values[name]
}

// fixedResolver answers every name with addrs.
func fixedResolver(addrs ...string) EgressResolver {
	return func(context.Context, string) ([]string, error) { return addrs, nil }
}

// TestWebhookRegistryDefaults pins the closed posture an embedder who passes
// nothing gets: loopback refused, no tunnel hook, a real guard and clock.
func TestWebhookRegistryDefaults(t *testing.T) {
	m := NewWebhookRegistry(WebhookOptions{})
	if m.allowLB {
		t.Error("the zero value must refuse loopback destinations")
	}
	if m.guard == nil || m.client == nil || m.now == nil || m.logger == nil {
		t.Error("nil options must be filled with production defaults")
	}
	if len(m.retryDelays) != len(webhookRetryDelays) {
		t.Errorf("retryDelays = %v, want the ported schedule %v", m.retryDelays, webhookRetryDelays)
	}
	if m.hasLiveTunnelShare("anything") {
		t.Error("a nil HasLiveTunnelShare hook must report no share, not assume one")
	}
	// A plain `> 0` check is satisfied even if the default clock's
	// UnixNano()/1e9 conversion were mutated to UnixNano()*1e9 — the result is
	// still positive, just off by 18 orders of magnitude. Bound it to a
	// plausible epoch-*seconds* range so a mutated divisor fails this
	// assertion instead of merely surviving a non-negativity check.
	now := m.now()
	const minPlausibleEpochS = 1e9  // 2001-09-09
	const maxPlausibleEpochS = 1e11 // 5138-11-16
	if now < minPlausibleEpochS || now > maxPlausibleEpochS {
		t.Errorf("default clock = %v, want a plausible epoch-seconds value in [%v, %v]",
			now, minPlausibleEpochS, maxPlausibleEpochS)
	}
}

// TestWebhookDeliveryTimingConstants pins computeWebhookDeliverTimeout and
// computeWebhookRetryDelays' values. The multiplications live in functions
// (rather than a bare package-level const/var) specifically so this assertion
// has a mutant to kill — see the doc comments on webhookDeliverTimeout and
// webhookRetryDelays.
func TestWebhookDeliveryTimingConstants(t *testing.T) {
	if got := computeWebhookDeliverTimeout(); got != 5*time.Second {
		t.Errorf("computeWebhookDeliverTimeout() = %v, want 5s", got)
	}
	want := []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second}
	got := computeWebhookRetryDelays()
	if len(got) != len(want) {
		t.Fatalf("computeWebhookRetryDelays() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("computeWebhookRetryDelays()[%d] = %v, want %v", i, got[i], want[i])
		}
	}
}

// TestWebhookRegistryRejectsBadInput pins that both validators refuse before
// anything is stored — a webhook that cannot be delivered to, or whose pattern
// the event bus would reject, must never enter the registry.
func TestWebhookRegistryRejectsBadInput(t *testing.T) {
	m := NewWebhookRegistry(WebhookOptions{Guard: NewEgressGuard(fixedResolver("93.184.216.34"), nil)})

	if _, err := m.Register("s1", "http://169.254.169.254/x", nil, "", ""); err == nil {
		t.Error("a metadata destination must be refused at registration")
	}
	// (a+)+ is the classic catastrophic-backtracking shape the bus's ReDoS
	// validator refuses; it must be refused here too, where the caller can see
	// the error, rather than inside a delivery goroutine.
	if err := m.ValidatePattern("(a+)+$"); err == nil {
		t.Error("ValidatePattern must refuse a ReDoS-shaped pattern")
	}
	if err := m.ValidatePattern(`\d+`); err != nil {
		t.Errorf("ValidatePattern(%q) = %v, want nil", `\d+`, err)
	}
	if _, err := m.Register("s1", "https://hook.example/x", nil, "(a+)+$", ""); err == nil {
		t.Error("Register must refuse a ReDoS-shaped pattern")
	}
	if len(m.ListWebhooks("s1")) != 0 {
		t.Error("a refused registration must leave nothing behind")
	}
}

// TestWebhookRegistryLifecycle covers the CRUD surface the routes drive,
// including the wire shape: event_types is null (not []) when unfiltered, so a
// consumer can tell "all types" from "no types".
func TestWebhookRegistryLifecycle(t *testing.T) {
	m := NewWebhookRegistry(WebhookOptions{Guard: NewEgressGuard(fixedResolver("93.184.216.34"), nil)})
	t.Cleanup(m.Shutdown)

	unfiltered, err := m.Register("s1", "https://hook.example/a", nil, "", "")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if unfiltered["event_types"] != nil || unfiltered["pattern"] != nil {
		t.Errorf("unfiltered webhook view = %v, want null event_types + pattern", unfiltered)
	}
	filtered, err := m.Register("s1", "https://hook.example/b", []string{"snapshot"}, `ok`, "shh")
	if err != nil {
		t.Fatalf("Register filtered: %v", err)
	}
	if filtered["pattern"] != "ok" {
		t.Errorf("pattern = %v, want %q", filtered["pattern"], "ok")
	}
	if _, err := m.Register("s2", "https://hook.example/c", nil, "", ""); err != nil {
		t.Fatalf("Register other session: %v", err)
	}

	if got := len(m.ListWebhooks("s1")); got != 2 {
		t.Errorf("ListWebhooks(s1) = %d entries, want 2 (s2's must not leak)", got)
	}
	id, _ := unfiltered["webhook_id"].(string)
	if len(id) != 32 {
		t.Errorf("webhook_id = %q, want 32 hex chars", id)
	}
	view, ok := m.GetWebhook(id)
	if !ok || view["url"] != "https://hook.example/a" {
		t.Errorf("GetWebhook(%q) = %v, %v", id, view, ok)
	}
	if _, ok := m.GetWebhook("nope"); ok {
		t.Error("GetWebhook of an unknown id must report absent")
	}
	if !m.Unregister(id) {
		t.Error("Unregister of a known webhook must report true")
	}
	if m.Unregister(id) {
		t.Error("Unregister is not idempotently true — a second call found nothing")
	}
	if got := len(m.ListWebhooks("s1")); got != 1 {
		t.Errorf("after unregister ListWebhooks(s1) = %d, want 1", got)
	}
}

// TestWebhookRegistryWithoutABusRegistersInertly pins the graceful no-op the
// reference has for event_bus=None: the webhook exists and can be listed, it
// simply never fires.
func TestWebhookRegistryWithoutABusRegistersInertly(t *testing.T) {
	m := NewWebhookRegistry(WebhookOptions{Guard: NewEgressGuard(fixedResolver("93.184.216.34"), nil)})
	view, err := m.Register("s1", "https://hook.example/a", nil, "", "")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	id, _ := view["webhook_id"].(string)
	if !m.Unregister(id) {
		t.Error("a bus-less webhook must still be unregisterable")
	}
	m.Shutdown() // must not block waiting for a loop that was never started
}

// TestWebhookRegistrySurfacesBusRefusal pins that a subscription the bus refuses
// fails the registration instead of returning a webhook that silently never
// fires. The bus caps subscribers per worker, so filling the cap is the
// reachable refusal.
func TestWebhookRegistrySurfacesBusRefusal(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{MaxSubscribersPerWorker: 1})
	m := NewWebhookRegistry(WebhookOptions{
		Guard:    NewEgressGuard(fixedResolver("93.184.216.34"), nil),
		EventBus: bus,
	})
	t.Cleanup(m.Shutdown)
	if _, err := m.Register("s1", "https://hook.example/a", nil, "", ""); err != nil {
		t.Fatalf("first Register: %v", err)
	}
	if _, err := m.Register("s1", "https://hook.example/b", nil, "", ""); err == nil {
		t.Error("a bus that refuses the subscription must fail the registration")
	}
	if got := len(m.ListWebhooks("s1")); got != 1 {
		t.Errorf("ListWebhooks(s1) = %d, want 1 (the refused one must not be stored)", got)
	}
}

// deliveryRegistry builds a registry wired to a bus, with the retry schedule
// collapsed so the give-up path can be asserted without waiting 3.5s for it.
func deliveryRegistry(t *testing.T, opts WebhookOptions) (*WebhookRegistry, *hub.EventBus, *counters) {
	t.Helper()
	c := newCounters()
	bus := hub.NewEventBus(hub.EventBusOptions{})
	opts.EventBus = bus
	opts.OnMetric = c.inc
	opts.RetryDelays = []time.Duration{}
	if opts.Guard == nil {
		opts.Guard = NewEgressGuard(fixedResolver("93.184.216.34"), nil)
	}
	m := NewWebhookRegistry(opts)
	t.Cleanup(m.Shutdown)
	return m, bus, c
}

// waitForWebhook polls until cond holds or the deadline passes.
func waitForWebhook(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// TestWebhookDeliverySignsAndPosts pins the delivered payload and the HMAC
// headers: a receiver has to be able to authenticate the delivery, and the
// signature is over exactly timestamp + "." + body.
func TestWebhookDeliverySignsAndPosts(t *testing.T) {
	type delivery struct {
		body   []byte
		header http.Header
	}
	got := make(chan delivery, 1)
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(buf)
		got <- delivery{body: buf, header: r.Header.Clone()}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(probe.Close)

	m, bus, _ := deliveryRegistry(t, WebhookOptions{
		// The probe is loopback, so the guard must be told loopback is allowed;
		// that is the §3 permission, passed in already resolved.
		AllowLoopbackDestinations: true,
		Guard:                     NewEgressGuard(nil, nil),
	})
	if _, err := m.Register("s1", probe.URL, nil, "", "topsecret"); err != nil {
		t.Fatalf("Register: %v", err)
	}
	bus.Enqueue("s1", map[string]any{"type": "snapshot", "data": map[string]any{"screen": "hello"}})

	select {
	case d := <-got:
		payload := map[string]any{}
		if err := json.Unmarshal(d.body, &payload); err != nil {
			t.Fatalf("delivered body is not JSON: %v (%q)", err, d.body)
		}
		if payload["session_id"] != "s1" {
			t.Errorf("payload session_id = %v, want s1", payload["session_id"])
		}
		if _, ok := payload["event"]; !ok {
			t.Errorf("payload carries no event: %v", payload)
		}
		ts := d.header.Get("X-Uterm-Timestamp")
		want := serverauth.BuildWebhookSignature("topsecret", d.body, ts)
		if sig := d.header.Get("X-Uterm-Signature"); sig != want {
			t.Errorf("X-Uterm-Signature = %q, want %q", sig, want)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no delivery arrived")
	}
}

// TestWebhookDeliveryPatternFiltersEvents pins that subscribe() actually wires
// a non-empty pattern through to the bus subscription (not just the empty-
// pattern branch every other delivery test exercises): a matching event is
// delivered and a non-matching one is not.
func TestWebhookDeliveryPatternFiltersEvents(t *testing.T) {
	var deliveries int
	var mu sync.Mutex
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		deliveries++
		mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(probe.Close)

	m, bus, _ := deliveryRegistry(t, WebhookOptions{
		AllowLoopbackDestinations: true,
		Guard:                     NewEgressGuard(nil, nil),
	})
	// pattern filters on event["data"]["screen"], not on event type.
	if _, err := m.Register("s1", probe.URL, nil, "hello", ""); err != nil {
		t.Fatalf("Register: %v", err)
	}
	// Screen text does not match "hello": must not be delivered.
	bus.Enqueue("s1", map[string]any{"type": "output", "data": map[string]any{"screen": "goodbye"}})
	// Screen text matches: must be delivered.
	bus.Enqueue("s1", map[string]any{"type": "output", "data": map[string]any{"screen": "say hello now"}})

	waitForWebhook(t, "the matching event to be delivered", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return deliveries == 1
	})
	// Give the (correctly-filtered) non-matching event a chance to have arrived
	// too, so a regression that drops the pattern entirely shows up as 2.
	time.Sleep(20 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if deliveries != 1 {
		t.Errorf("deliveries = %d, want 1 (the pattern must filter the non-matching event)", deliveries)
	}
}

// TestWebhookPostSignatureUsesFullTimestampPrecision pins that the signed
// timestamp uses full float precision (strconv.FormatFloat's -1 precision
// argument), not a truncated one: a `Now` that returns a value needing more
// than one significant fractional digit must appear in full in the signed
// timestamp, so the signature is computed over the exact same string a
// receiver would reconstruct.
func TestWebhookPostSignatureUsesFullTimestampPrecision(t *testing.T) {
	type delivery struct {
		ts  string
		sig string
	}
	got := make(chan delivery, 1)
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- delivery{ts: r.Header.Get("X-Uterm-Timestamp"), sig: r.Header.Get("X-Uterm-Signature")}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(probe.Close)

	const fixedNow = 1700000000.123456
	c := newCounters()
	m := NewWebhookRegistry(WebhookOptions{
		Guard:       NewEgressGuard(fixedResolver("93.184.216.34"), nil),
		OnMetric:    c.inc,
		RetryDelays: []time.Duration{},
		Now:         func() float64 { return fixedNow },
	})
	m.post(&webhookEntry{id: "w1", sessionID: "s1", url: probe.URL, secret: "shh"}, map[string]any{"type": "output"})

	select {
	case d := <-got:
		// Computed independently of the source's own FormatFloat call (this
		// literal -1 is in the test, the source's is in server_webhooks.go), so
		// a mutant that changes the source's precision produces a *different*
		// (rounded) header value than this expectation.
		wantTS := strconv.FormatFloat(fixedNow, 'f', -1, 64)
		if d.ts != wantTS {
			t.Errorf("X-Uterm-Timestamp = %q, want %q (full precision)", d.ts, wantTS)
		}
		if d.sig == "" {
			t.Error("X-Uterm-Signature must be set when the webhook has a secret")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no delivery arrived")
	}
}

// TestWebhookAttemptLogsOneIndexedAttemptNumber pins that the "attempt" field
// logged on failure is 1-indexed (attempt+1), for both the request-construction
// failure arm and the transport-error arm.
func TestWebhookAttemptLogsOneIndexedAttemptNumber(t *testing.T) {
	t.Run("unbuildable request", func(t *testing.T) {
		var buf bytes.Buffer
		m := NewWebhookRegistry(WebhookOptions{
			RetryDelays: []time.Duration{},
			Logger:      slog.New(slog.NewTextHandler(&buf, nil)),
		})
		e := &webhookEntry{id: "w1", sessionID: "s1", url: "http://host\x7f/x"}
		if m.attempt(e, []byte("{}"), http.Header{}, 0) {
			t.Fatal("an unbuildable request must not report success")
		}
		if !strings.Contains(buf.String(), "attempt=1") {
			t.Errorf("log = %q, want it to report attempt=1 (1-indexed) for attempt index 0", buf.String())
		}
	})

	t.Run("transport error", func(t *testing.T) {
		probe := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
		url := probe.URL
		probe.Close() // nothing listening now, so the dial fails

		var buf bytes.Buffer
		m := NewWebhookRegistry(WebhookOptions{
			RetryDelays: []time.Duration{},
			Logger:      slog.New(slog.NewTextHandler(&buf, nil)),
		})
		e := &webhookEntry{id: "w1", sessionID: "s1", url: url}
		if m.attempt(e, []byte("{}"), http.Header{}, 0) {
			t.Fatal("a dial failure must not report success")
		}
		if !strings.Contains(buf.String(), "attempt=1") {
			t.Errorf("log = %q, want it to report attempt=1 (1-indexed) for attempt index 0", buf.String())
		}
	})

	t.Run("non-2xx response", func(t *testing.T) {
		var buf bytes.Buffer
		m := NewWebhookRegistry(WebhookOptions{
			RetryDelays: []time.Duration{},
			Logger:      slog.New(slog.NewTextHandler(&buf, nil)),
			HTTPClient:  &http.Client{Transport: fixedStatusRoundTripper{status: 500}},
		})
		e := &webhookEntry{id: "w1", sessionID: "s1", url: "https://hook.example/x"}
		if m.attempt(e, []byte("{}"), http.Header{}, 0) {
			t.Fatal("a 500 response must not report success")
		}
		if !strings.Contains(buf.String(), "attempt=1") {
			t.Errorf("log = %q, want it to report attempt=1 (1-indexed) for attempt index 0", buf.String())
		}
	})
}

// fixedStatusRoundTripper answers every request with a fabricated response
// carrying the given status code. A real httptest.Server cannot produce a 1xx
// status through ResponseWriter.WriteHeader without sending a genuine final
// response after it (1xx are "informational" in net/http, not a final
// header), so the low boundary (199) is faked at the RoundTripper level
// instead of over the wire.
type fixedStatusRoundTripper struct{ status int }

func (rt fixedStatusRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	return &http.Response{
		StatusCode: rt.status,
		Body:       http.NoBody,
		Header:     make(http.Header),
		Request:    req,
	}, nil
}

// TestWebhookAttemptStatusCodeBoundaries pins the exact 2xx window
// (`>= 200 && < 300`) at both edges: 199 and 300 must fail, 200 and 299 must
// succeed.
func TestWebhookAttemptStatusCodeBoundaries(t *testing.T) {
	cases := []struct {
		status int
		want   bool
	}{
		{199, false},
		{200, true},
		{299, true},
		{300, false},
	}
	for _, tc := range cases {
		m := NewWebhookRegistry(WebhookOptions{
			RetryDelays: []time.Duration{},
			OnMetric:    newCounters().inc,
			HTTPClient:  &http.Client{Transport: fixedStatusRoundTripper{status: tc.status}},
		})
		e := &webhookEntry{id: "w1", sessionID: "s1", url: "https://hook.example/x"}
		if got := m.attempt(e, []byte("{}"), http.Header{}, 0); got != tc.want {
			t.Errorf("status %d: attempt() = %v, want %v", tc.status, got, tc.want)
		}
	}
}

// TestWebhookDeliveryRetriesThenGivesUp pins that a destination answering 500 is
// retried and then abandoned loudly, rather than retried forever.
func TestWebhookDeliveryRetriesThenGivesUp(t *testing.T) {
	var attempts int
	var mu sync.Mutex
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		attempts++
		mu.Unlock()
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(probe.Close)

	m, bus, c := deliveryRegistry(t, WebhookOptions{
		AllowLoopbackDestinations: true,
		Guard:                     NewEgressGuard(nil, nil),
	})
	if _, err := m.Register("s1", probe.URL, nil, "", ""); err != nil {
		t.Fatalf("Register: %v", err)
	}
	bus.Enqueue("s1", map[string]any{"type": "output"})

	waitForWebhook(t, "the delivery to be abandoned", func() bool {
		return c.get("webhook_delivery_giving_up_total") == 1
	})
	if got := c.get("webhook_delivery_failed_total"); got != 1 {
		t.Errorf("webhook_delivery_failed_total = %d, want 1 (one attempt, retries collapsed)", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if attempts != 1 {
		t.Errorf("destination saw %d attempts, want 1", attempts)
	}
}

// TestWebhookDeliveryUnreachableDestinationIsRetried pins the transport-error
// arm: a destination that refuses the connection is a failed attempt, not a
// crash, and the schedule still terminates.
func TestWebhookDeliveryUnreachableDestinationIsRetried(t *testing.T) {
	probe := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := probe.URL
	probe.Close() // nothing is listening now, so the dial fails

	m, bus, c := deliveryRegistry(t, WebhookOptions{
		AllowLoopbackDestinations: true,
		Guard:                     NewEgressGuard(nil, nil),
	})
	if _, err := m.Register("s1", url, nil, "", ""); err != nil {
		t.Fatalf("Register: %v", err)
	}
	bus.Enqueue("s1", map[string]any{"type": "output"})
	waitForWebhook(t, "the unreachable delivery to be abandoned", func() bool {
		return c.get("webhook_delivery_giving_up_total") == 1
	})
}

// TestWebhookDeliveryRetriesBeforeGivingUp pins that the schedule is actually
// walked — each configured delay is a further attempt, not decoration — using a
// millisecond schedule so the assertion costs nothing.
func TestWebhookDeliveryRetriesBeforeGivingUp(t *testing.T) {
	var attempts int
	var mu sync.Mutex
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		attempts++
		mu.Unlock()
		w.WriteHeader(http.StatusBadGateway)
	}))
	t.Cleanup(probe.Close)

	c := newCounters()
	m := NewWebhookRegistry(WebhookOptions{
		AllowLoopbackDestinations: true,
		OnMetric:                  c.inc,
		RetryDelays:               []time.Duration{time.Millisecond, time.Millisecond},
	})
	m.post(&webhookEntry{id: "w1", sessionID: "s1", url: probe.URL}, map[string]any{"type": "output"})

	mu.Lock()
	defer mu.Unlock()
	if attempts != 3 {
		t.Errorf("destination saw %d attempts, want 3 (one plus two retries)", attempts)
	}
	if got := c.get("webhook_delivery_giving_up_total"); got != 1 {
		t.Errorf("webhook_delivery_giving_up_total = %d, want 1", got)
	}
}

// TestWebhookDeliveryAutoUnregistersAfterRepeatedGuardBlocks pins the reference's
// SSRF-block threshold: a destination that has become permanently unsafe is
// dropped rather than re-evaluated forever on an attacker's schedule.
//
// A rebind is simulated by resolving the name to metadata *after* registration
// succeeded against a public answer, which is exactly the shape the delivery-time
// re-check exists to catch.
func TestWebhookDeliveryAutoUnregistersAfterRepeatedGuardBlocks(t *testing.T) {
	var rebound bool
	var mu sync.Mutex
	resolver := func(context.Context, string) ([]string, error) {
		mu.Lock()
		defer mu.Unlock()
		if rebound {
			return []string{"169.254.169.254"}, nil
		}
		return []string{"93.184.216.34"}, nil
	}
	// A zero TTL keeps the guard's per-host cache from serving the pre-rebind
	// answer back on every delivery.
	guard := NewEgressGuard(resolver, nil)
	guard.ttlS = 0

	m, bus, c := deliveryRegistry(t, WebhookOptions{Guard: guard})
	view, err := m.Register("s1", "https://hook.example/x", nil, "", "")
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	id, _ := view["webhook_id"].(string)

	mu.Lock()
	rebound = true
	mu.Unlock()
	for i := 0; i < webhookMaxBlockedDeliveries; i++ {
		bus.Enqueue("s1", map[string]any{"type": "output"})
	}
	// Waited on the removal rather than on the counter that announces it: the
	// metric is emitted before Unregister runs, so a wait on the counter can win
	// the race against the effect it is reporting and leave the entry still in
	// the registry for the assertion below.
	waitForWebhook(t, "the webhook to be auto-unregistered", func() bool {
		_, ok := m.GetWebhook(id)
		return !ok
	})
	if got := c.get("webhook_auto_unregistered_total"); got != 1 {
		t.Errorf("webhook_auto_unregistered_total = %d, want 1", got)
	}
	if got := c.get("webhook_delivery_blocked_total"); got != webhookMaxBlockedDeliveries {
		t.Errorf("webhook_delivery_blocked_total = %d, want %d", got, webhookMaxBlockedDeliveries)
	}
}

// TestWebhookDeliveryGuardPassClearsTheBlockCount pins that the threshold counts
// *consecutive* blocks: a webhook that is intermittently unsafe must not be
// killed by a stale tally.
func TestWebhookDeliveryGuardPassClearsTheBlockCount(t *testing.T) {
	e := &webhookEntry{id: "w1", sessionID: "s1", url: "https://hook.example/x", blocked: 2}
	c := newCounters()
	// A destination that resolves public and a client that never gets used
	// (delivery is allowed, and the POST target simply fails) is enough: the
	// assertion is about the counter, not the POST.
	m := NewWebhookRegistry(WebhookOptions{
		Guard:       NewEgressGuard(fixedResolver("93.184.216.34"), nil),
		OnMetric:    c.inc,
		RetryDelays: []time.Duration{},
	})
	m.deliver(e, map[string]any{"type": "output"})
	m.mu.Lock()
	blocked := e.blocked
	m.mu.Unlock()
	if blocked != 0 {
		t.Errorf("blocked = %d after a guard pass, want 0", blocked)
	}
	if c.get("webhook_auto_unregistered_total") != 0 {
		t.Error("a guard pass must not auto-unregister")
	}
}

// TestWebhookDeliveryUnencodablePayloadIsDropped pins that an event carrying
// something encoding/json refuses is logged and skipped rather than panicking a
// delivery goroutine.
func TestWebhookDeliveryUnencodablePayloadIsDropped(t *testing.T) {
	c := newCounters()
	m := NewWebhookRegistry(WebhookOptions{OnMetric: c.inc, RetryDelays: []time.Duration{}})
	m.post(&webhookEntry{id: "w1", sessionID: "s1", url: "https://hook.example/x"},
		map[string]any{"chan": make(chan int)})
	if c.get("webhook_delivery_giving_up_total") != 0 {
		t.Error("an unencodable payload must not reach the send loop at all")
	}
}

// TestWebhookDeliveryRejectsAnUnbuildableRequest pins the request-construction
// arm: a destination whose URL cannot become an http.Request is a failed
// attempt, not a panic. Reachable only by bypassing validation, which is why it
// is exercised on the entry directly.
func TestWebhookDeliveryRejectsAnUnbuildableRequest(t *testing.T) {
	c := newCounters()
	m := NewWebhookRegistry(WebhookOptions{OnMetric: c.inc, RetryDelays: []time.Duration{}})
	e := &webhookEntry{id: "w1", sessionID: "s1", url: "http://host\x7f/x"}
	if m.attempt(e, []byte("{}"), http.Header{}, 0) {
		t.Error("an unbuildable request must not report success")
	}
}

// TestWebhookDeliveryLoopStopsOnWorkerDisconnect pins that the nil-map sentinel
// the bus pushes on worker disconnect ends the delivery loop, so a disconnected
// session does not leave a goroutine parked forever.
func TestWebhookDeliveryLoopStopsOnWorkerDisconnect(t *testing.T) {
	m, bus, _ := deliveryRegistry(t, WebhookOptions{})
	if _, err := m.Register("s1", "https://hook.example/x", nil, "", ""); err != nil {
		t.Fatalf("Register: %v", err)
	}
	bus.CloseWorker("s1")
	done := make(chan struct{})
	go func() { m.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("the delivery loop did not exit on the worker-disconnect sentinel")
	}
}

// TestLiveTunnelShare pins §4's expiry rule directly: a share is live only while
// its record's expiry is in the future. A token record outlives its expiry until
// the next sweep, so "the record exists" is not the question being asked.
func TestLiveTunnelShare(t *testing.T) {
	store := tunnel.NewMemStore()
	store.PutToken("live", tunnel.TokenRecord{ExpiresAt: 1000})
	store.PutToken("stale", tunnel.TokenRecord{ExpiresAt: 500})

	cases := []struct {
		name    string
		store   tunnel.Store
		session string
		now     float64
		want    bool
	}{
		{"live share", store, "live", 900, true},
		{"expired share", store, "stale", 900, false},
		{"expiry exactly now is not live", store, "live", 1000, false},
		{"never created", store, "absent", 900, false},
		{"no store at all", nil, "live", 900, false},
	}
	for _, tc := range cases {
		if got := LiveTunnelShare(tc.store, tc.session, tc.now); got != tc.want {
			t.Errorf("%s: LiveTunnelShare = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// TestCheckWebhookDestinationRejectsAnUnparseableURL pins the parse-error arm as
// a refusal: a URL the guard cannot even read has certainly not been cleared.
func TestCheckWebhookDestinationRejectsAnUnparseableURL(t *testing.T) {
	g := NewEgressGuard(fixedResolver("93.184.216.34"), nil)
	_, err := g.CheckWebhookDestination(context.Background(), "http://[::1", false)
	var blocked *EgressBlockedError
	if !errors.As(err, &blocked) {
		t.Fatalf("CheckWebhookDestination of a malformed URL = %v, want EgressBlockedError", err)
	}
	if !strings.Contains(blocked.Msg, "invalid") {
		t.Errorf("refusal message = %q, want it to name the URL as invalid", blocked.Msg)
	}
}
