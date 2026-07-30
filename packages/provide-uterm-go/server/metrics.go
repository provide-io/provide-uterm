//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"sort"
	"strconv"
	"strings"
	"sync"
)

// metricKeys are the counter names pre-seeded to zero, matching the Python
// initial_metrics() dict so /api/metrics reports the same key set from boot.
var metricKeys = []string{
	"http_requests_total",
	"http_requests_4xx_total",
	"http_requests_5xx_total",
	"http_requests_error_total",
	"auth_failures_http_total",
	"auth_failures_ws_total",
	"ws_disconnect_total",
	"ws_disconnect_worker_total",
	"ws_disconnect_browser_total",
	"hijack_conflicts_total",
	"hijack_lease_expiries_total",
	"hijack_acquires_total",
	"hijack_releases_total",
	"hijack_steps_total",
	"ws_browser_rate_limited_total",
	"ws_browser_control_rate_limited_total",
	"ws_worker_frame_invalid_total",
	"rest_acquire_rate_limited_total",
	"rest_send_rate_limited_total",
	"rest_step_rate_limited_total",
	"webhook_delivery_blocked_total",
	// Go-only until the other ports land conformance/EGRESS_GUARD.md §4: the
	// count of loopback deliveries refused because the session held a live
	// tunnel share. Seeded to zero rather than created on first use so an
	// operator can see the guard exists before it has ever fired.
	"webhook_delivery_blocked_tunnel_total",
	"webhook_auto_unregistered_total",
	"webhook_delivery_failed_total",
	"webhook_delivery_giving_up_total",
	"event_bus_subscriber_drop_total",
}

// Metrics is the server-wide counter map. It is the sink passed to the hub's
// OnMetric callback (so hub.Metric increments land here) and the source for
// /api/metrics. Safe for concurrent use.
type Metrics struct {
	mu     sync.Mutex
	values map[string]int
}

// NewMetrics returns a Metrics with every known counter seeded to zero.
func NewMetrics() *Metrics {
	m := &Metrics{values: make(map[string]int, len(metricKeys))}
	for _, k := range metricKeys {
		m.values[k] = 0
	}
	return m
}

// Inc adds value to a counter, creating it if unseen (matching the Python
// _inc_metric closure, which setdefaults unknown names to 0 then adds).
func (m *Metrics) Inc(name string, value int) {
	m.mu.Lock()
	m.values[name] += value
	m.mu.Unlock()
}

// Snapshot returns a copy of the counter map for JSON serialization.
func (m *Metrics) Snapshot() map[string]int {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]int, len(m.values))
	for k, v := range m.values {
		out[k] = v
	}
	return out
}

// Prometheus renders the counter map in Prometheus text-exposition format,
// matching /api/metrics/prometheus: for each sorted name a "# TYPE" line then a
// value line, trailing newline when non-empty.
func (m *Metrics) Prometheus() string {
	snap := m.Snapshot()
	names := make([]string, 0, len(snap))
	for k := range snap {
		names = append(names, k)
	}
	sort.Strings(names)
	var b strings.Builder
	for _, name := range names {
		b.WriteString("# TYPE ")
		b.WriteString(name)
		b.WriteString(" counter\n")
		b.WriteString(name)
		b.WriteByte(' ')
		b.WriteString(strconv.Itoa(snap[name]))
		b.WriteByte('\n')
	}
	return b.String()
}
