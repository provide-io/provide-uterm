//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"log/slog"
	"sync"
)

// TermHubConfig configures a [TermHub]. Every field is optional; a zero value
// selects the same default the Python TermHub.__init__ applies. Pointer /
// interface fields left nil select the documented default (e.g. PolicyGate nil
// → [NoOpPolicyGate], Clock nil → real clock).
type TermHubConfig struct {
	DashboardHijackLeaseS         int
	MaxWSMessageBytes             int
	MaxInputChars                 int
	MaxBufferChars                int
	MaxEventDataChars             int
	BrowserRateLimitPerSec        float64
	BrowserControlRateLimitPerSec float64
	RestAcquireRateLimitPerSec    float64
	RestSendRateLimitPerSec       float64
	WorkerToken                   *string
	WorkerFrameOnInvalid          string
	EventDequeMaxlen              int
	ResumeStore                   ResumeTokenStore
	ResumeTTLS                    float64
	EventBus                      *EventBus
	WSIdleTimeoutS                float64
	PolicyGate                    PolicyGate
	IdentityProvider              IdentityProvider
	// DelegateRoles selects principal-role passthrough (Python default True).
	// nil → true.
	DelegateRoles              *bool
	OutputPolicyGate           OutputPolicyGate
	Redactor                   Redactor
	BehavioralAuditGate        BehavioralAuditGate
	BehavioralThresholds       BehavioralThresholds
	BehavioralAuditIntervalS   float64
	TelemetrySink              TelemetrySink
	MaxConnectionsPerPrincipal int
	MaxWorkers                 int

	OnHijackChanged    func(workerID string, enabled bool, owner *string) error
	OnMetric           func(name string, value int)
	ResolveBrowserRole RoleResolver
	OnWorkerEmpty      func(ctx context.Context, workerID string) error

	Clock  Clock
	Logger *slog.Logger
	// IDGen mints presence req_id values (nil → RFC-4122 v4 UUIDs).
	IDGen func() string
}

// TermHub composes the wave-A services (registry, limiter, approval store,
// lease, state, polling) with the wave-B services (router, connection manager,
// presence manager) and the resume-token store. Port of
// provide.uterm.server.bridge.hub.core_impl.TermHub.
//
// The nine services are exposed as public fields so callers can prefer
// hub.<service>.<method>(...); a focused set of facade methods carrying
// cross-service orchestration (telemetry, per-ws cleanup, event-bus close) also
// live on TermHub. A single shared [sync.Mutex] serialises every service that
// mutates worker state, exactly matching the Python single-asyncio-Lock
// invariant.
type TermHub struct {
	Registry  *WorkerRegistry
	Limiter   *RateLimiter
	Approvals *InMemoryApprovalStore
	Lease     *HijackLeaseManager
	State     *StateStore
	Polling   *PollingCoordinator
	Router    *MessageRouter
	Conn      *ConnectionManager
	Presence  *PresenceManager

	lock     *sync.Mutex
	registry *WorkerRegistry
	clock    Clock
	logger   *slog.Logger

	// Config (clamped at construction).
	maxWSMessageBytes             int
	maxInputChars                 int
	maxBufferChars                int
	maxEventDataChars             int
	browserRateLimitPerSec        float64
	browserControlRateLimitPerSec float64
	workerFrameOnInvalid          string
	eventDequeMaxlen              int
	resumeTTLS                    float64
	wsIdleTimeoutS                float64
	maxConnectionsPerPrincipal    int
	maxWorkers                    int
	behavioralAuditIntervalS      float64
	workerToken                   *string
	delegateRoles                 bool

	// Optional collaborators.
	eventBus            *EventBus
	resumeStore         ResumeTokenStore
	policyGate          PolicyGate
	outputPolicyGate    OutputPolicyGate
	redactor            Redactor
	behavioralAuditGate BehavioralAuditGate
	behavioralThresh    BehavioralThresholds
	telemetrySink       TelemetrySink
	identityProvider    IdentityProvider

	// Callbacks.
	onHijackChanged func(string, bool, *string) error
	onMetric        func(string, int)
	onWorkerEmpty   func(context.Context, string) error
	newID           func() string

	// Per-ws hub-level state, all guarded by lock.
	wsToResumeToken        map[BrowserConn]string
	resumeTokenDetached    map[string]chan struct{}
	startupPendingBrowsers map[BrowserConn]bool
	pausedBrowsers         map[BrowserConn]bool
	holdBuffers            map[BrowserConn]string
	principalBrowserCounts map[string]int
	wsPrincipal            map[BrowserConn]string

	tasks *BackgroundTasks
}

// clampInt clamps v to at least lo.
func clampMin(v, lo int) int {
	if v < lo {
		return lo
	}
	return v
}

func clampMinF(v, lo float64) float64 {
	if v < lo {
		return lo
	}
	return v
}

// def picks v when non-zero, else d.
func defInt(v, d int) int {
	if v == 0 {
		return d
	}
	return v
}

func defF(v, d float64) float64 {
	if v == 0 {
		return d
	}
	return v
}

// NewTermHub builds a fully composed hub from cfg.
func NewTermHub(cfg TermHubConfig) *TermHub {
	lock := &sync.Mutex{}
	registry := NewWorkerRegistry()
	clock := orDefaultClock(cfg.Clock)
	logger := loggerOrDefault(cfg.Logger)

	delegate := true
	if cfg.DelegateRoles != nil {
		delegate = *cfg.DelegateRoles
	}
	policyGate := cfg.PolicyGate
	if policyGate == nil {
		policyGate = NoOpPolicyGate{}
	}
	auditGate := cfg.BehavioralAuditGate
	if auditGate == nil {
		auditGate = NoOpBehavioralAuditGate{}
	}
	idGen := cfg.IDGen
	if idGen == nil {
		idGen = newUUID4
	}

	maxInputChars := clampMin(defInt(cfg.MaxInputChars, 10000), 100)
	maxBufferChars := clampMin(defInt(cfg.MaxBufferChars, 40000), maxInputChars)

	h := &TermHub{
		Registry: registry,
		lock:     lock,
		registry: registry,
		clock:    clock,
		logger:   logger,

		maxWSMessageBytes:             clampMin(defInt(cfg.MaxWSMessageBytes, 1_048_576), 1024),
		maxInputChars:                 maxInputChars,
		maxBufferChars:                maxBufferChars,
		maxEventDataChars:             clampMin(defInt(cfg.MaxEventDataChars, 8192), 256),
		browserRateLimitPerSec:        defF(cfg.BrowserRateLimitPerSec, 30),
		browserControlRateLimitPerSec: clampMinF(defF(cfg.BrowserControlRateLimitPerSec, 10), 0.1),
		workerFrameOnInvalid:          orString(cfg.WorkerFrameOnInvalid, "drop"),
		eventDequeMaxlen:              clampMin(defInt(cfg.EventDequeMaxlen, 2000), 1),
		resumeTTLS:                    clampMinF(defF(cfg.ResumeTTLS, 300), 1),
		wsIdleTimeoutS:                clampMinF(defF(cfg.WSIdleTimeoutS, 14400), 10),
		maxConnectionsPerPrincipal:    clampMin(defInt(cfg.MaxConnectionsPerPrincipal, 25), 1),
		maxWorkers:                    clampMin(defInt(cfg.MaxWorkers, 10000), 1),
		behavioralAuditIntervalS:      clampMinF(defF(cfg.BehavioralAuditIntervalS, 30), 1),
		workerToken:                   cfg.WorkerToken,
		delegateRoles:                 delegate,

		eventBus:            cfg.EventBus,
		resumeStore:         cfg.ResumeStore,
		policyGate:          policyGate,
		outputPolicyGate:    cfg.OutputPolicyGate,
		redactor:            cfg.Redactor,
		behavioralAuditGate: auditGate,
		behavioralThresh:    cfg.BehavioralThresholds,
		telemetrySink:       cfg.TelemetrySink,
		identityProvider:    cfg.IdentityProvider,

		onHijackChanged: cfg.OnHijackChanged,
		onMetric:        cfg.OnMetric,
		onWorkerEmpty:   cfg.OnWorkerEmpty,
		newID:           idGen,

		wsToResumeToken:        map[BrowserConn]string{},
		resumeTokenDetached:    map[string]chan struct{}{},
		startupPendingBrowsers: map[BrowserConn]bool{},
		pausedBrowsers:         map[BrowserConn]bool{},
		holdBuffers:            map[BrowserConn]string{},
		principalBrowserCounts: map[string]int{},
		wsPrincipal:            map[BrowserConn]string{},
	}

	h.Limiter = NewRateLimiter(
		defF(cfg.RestAcquireRateLimitPerSec, 5),
		defF(cfg.RestSendRateLimitPerSec, 20),
		clock,
	)
	h.Approvals = NewInMemoryApprovalStore(clock)
	h.State = NewStateStore(StateStoreConfig{
		Registry:           registry,
		Lock:               lock,
		Clock:              clock,
		Logger:             logger,
		MaxBufferChars:     maxBufferChars,
		OnMetric:           cfg.OnMetric,
		OnHijackChanged:    cfg.OnHijackChanged,
		ResolveBrowserRole: cfg.ResolveBrowserRole,
		IdentityProvider:   cfg.IdentityProvider,
		DelegateRoles:      delegate,
	})
	h.tasks = h.State.Tasks()
	h.Lease = NewHijackLeaseManager(registry, lock, defInt(cfg.DashboardHijackLeaseS, 45), h, clock, logger)
	h.Router = newMessageRouter(h)
	h.Presence = newPresenceManager(h)
	h.Conn = newConnectionManager(h)
	h.Polling = NewPollingCoordinator(registry, lock, clock, h.Presence.RequestSnapshot)
	return h
}

func orString(v, d string) string {
	if v == "" {
		return d
	}
	return v
}

// EventBus returns the configured event bus (nil when unset).
func (h *TermHub) EventBus() *EventBus { return h.eventBus }

// ResumeStore returns the configured resume-token store (nil when unset).
func (h *TermHub) ResumeStore() ResumeTokenStore { return h.resumeStore }

// WorkerFrameOnInvalid returns the malformed-worker-frame policy ("drop"|"reject").
func (h *TermHub) WorkerFrameOnInvalid() string { return h.workerFrameOnInvalid }

// MaxWSMessageBytes returns the configured inbound WS frame cap.
func (h *TermHub) MaxWSMessageBytes() int { return h.maxWSMessageBytes }

// MaxInputChars returns the configured per-input-frame char cap.
func (h *TermHub) MaxInputChars() int { return h.maxInputChars }

// Shutdown cancels background tasks. Port of TermHub.shutdown.
func (h *TermHub) Shutdown() int { return h.State.Shutdown() }
