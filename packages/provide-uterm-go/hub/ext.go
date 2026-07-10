//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// Structured event names emitted by the connection/resume lifecycle, matching
// the provide.telemetry event() dotted strings used by the Python ext.py
// (event("terminal","session","registered") == "terminal.session.registered").
const (
	eventSessionRegistered   = "terminal.session.registered"
	eventSessionDisconnected = "terminal.session.disconnected"
	eventRateLimitTriggered  = "terminal.ratelimit.triggered"
	eventResumeFailed        = "terminal.resume.failed"
)

// PolicyDecision is the decision returned by a [PolicyGate]. Port of ext.PolicyDecision.
// Action is one of "allow", "deny", "hold".
type PolicyDecision struct {
	Action    string
	RequestID *string
	TimeoutS  int
	Reason    *string
}

// AllowDecision returns the default allow decision (TimeoutS mirrors the
// Pydantic default of 60).
func AllowDecision() PolicyDecision { return PolicyDecision{Action: "allow", TimeoutS: 60} }

// PolicyGate is the input-interception policy surface. Port of ext.PolicyGate.
type PolicyGate interface {
	InterceptInput(ctx context.Context, data string, pc PolicyContext) (PolicyDecision, error)
}

// NoOpPolicyGate allows everything. Port of ext.NoOpPolicyGate.
type NoOpPolicyGate struct{}

// InterceptInput always allows.
func (NoOpPolicyGate) InterceptInput(_ context.Context, _ string, _ PolicyContext) (PolicyDecision, error) {
	return AllowDecision(), nil
}

// RedactionRule is a regex-based redaction rule. Port of ext.RedactionRule.
type RedactionRule struct {
	Pattern     string
	Replacement string
}

// OutputPolicyGate returns the active redaction rules for a policy context.
// Port of ext.OutputPolicyGate. A nil gate on the hub means no output
// redaction (the default) and the broadcast/read paths ship raw frames.
type OutputPolicyGate interface {
	GetRedactionRules(ctx context.Context, pc PolicyContext) ([]RedactionRule, error)
}

// Redactor applies a rule set to a frame map, returning a redacted COPY; the hub
// never mutates the input. [RedactFrameFields] is the concrete implementation
// wiring the real [StreamRedactor] (redaction.go / redaction_defaults.go /
// router_redaction.go) — assign it to TermHubConfig.Redactor to activate output
// redaction. When no [Redactor] is configured the broadcast/read paths treat a
// non-empty rule set as "leave the frame unchanged".
type Redactor func(msg map[string]any, rules []RedactionRule) map[string]any

// ConnectionHeuristics carries behavioral metrics for one connection. Port of
// ext.ConnectionHeuristics.
type ConnectionHeuristics struct {
	CPS       float64
	Jitter    float64
	Timestamp float64
}

// BehavioralThresholds bounds behavioral anomaly detection. Port of
// ext.BehavioralThresholds. Nil pointers mirror the Python None (unset).
type BehavioralThresholds struct {
	MaxCPS    *float64
	MinJitter *float64
}

// BehavioralAuditGate evaluates behavioral metrics and returns a gating
// decision. Port of ext.BehavioralAuditGate.
type BehavioralAuditGate interface {
	AuditConnection(ctx context.Context, h ConnectionHeuristics, pc PolicyContext, th BehavioralThresholds) (PolicyDecision, error)
}

// NoOpBehavioralAuditGate allows everything. Port of ext.NoOpBehavioralAuditGate.
type NoOpBehavioralAuditGate struct{}

// AuditConnection always allows.
func (NoOpBehavioralAuditGate) AuditConnection(
	_ context.Context, _ ConnectionHeuristics, _ PolicyContext, _ BehavioralThresholds,
) (PolicyDecision, error) {
	return AllowDecision(), nil
}

// TelemetryEvent is a lifecycle telemetry event. Port of ext.TelemetryEvent.
type TelemetryEvent struct {
	EventType string
	WorkerID  string
	Principal *string
	Role      *string
	Metadata  map[string]any
	Timestamp float64
}

// TelemetrySink is the upward Node→Fleet-Manager telemetry channel. Port of
// ext.TelemetrySink. Emit must never raise (a returned error is swallowed by
// the hub's fail-open emitter).
type TelemetrySink interface {
	Emit(ctx context.Context, evt TelemetryEvent) error
}
