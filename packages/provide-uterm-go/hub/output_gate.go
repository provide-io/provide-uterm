//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// NoOpOutputPolicyGate is the default output-policy gate: it yields no rules, so
// no redaction is applied. Port of ext.NoOpOutputPolicyGate. Wiring a hub with a
// nil OutputPolicyGate has the same effect (the broadcast/read paths short-
// circuit on a nil gate); this concrete type exists for callers that want an
// explicit, non-nil gate.
type NoOpOutputPolicyGate struct{}

// GetRedactionRules returns no rules.
func (NoOpOutputPolicyGate) GetRedactionRules(_ context.Context, _ PolicyContext) ([]RedactionRule, error) {
	return nil, nil
}

// DefaultRulesOutputPolicyGate is an output-policy gate that returns the built-in
// [DefaultRules] for every recipient (fail-closed default redaction). It mirrors
// the fallback the Python WebhookOutputPolicyGate uses when its webhook is
// unreachable — the secrets keep getting redacted. Pair it with
// TermHubConfig.Redactor = RedactFrameFields to redact real output.
type DefaultRulesOutputPolicyGate struct{}

// GetRedactionRules returns the built-in default redaction rules.
func (DefaultRulesOutputPolicyGate) GetRedactionRules(_ context.Context, _ PolicyContext) ([]RedactionRule, error) {
	return DefaultRules(), nil
}
