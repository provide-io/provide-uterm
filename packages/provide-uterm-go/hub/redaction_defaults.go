//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

// Default secret-redaction rules for terminal session output. Port of
// provide.uterm.server.bridge.hub.redaction_defaults. The patterns/replacements
// are ported verbatim from the Python defaults; they target known credential
// formats with anchored prefixes / canonical lengths to keep the false-positive
// rate low.
//
// RE2 note: three of these rules (generic password / api_key / token) use a
// `(?=...)` lookahead that Go's RE2 rejects. NewStreamRedactor skips them at
// build time (Python re.error parity), so under Go the built-in defaults redact
// the cloud/token/PEM/Authorization shapes but NOT the three lookahead-based
// generic shapes. They are retained here verbatim so the rule set stays a
// faithful mirror of the Python source and so a future RE2-compatible rewrite is
// a local edit.

// Cloud-provider credentials.
const (
	// AWS access key id — 20-char identifier starting with the AKIA / ASIA /
	// AROA / AIDA / AGPA prefix family.
	patAWSAccessKeyID = `\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|ASCA)[0-9A-Z]{16}\b`

	// AWS secret access key — 40-char base64 pinned to the canonical
	// aws_secret_access_key=... form. Uses a scoped (?i:...) flag (not a global
	// (?i)) so it is valid inside an alternation.
	patAWSSecretAccessKey = `(?i:aws[_ -]?secret[_ -]?access[_ -]?key\s*[:=]\s*['"]?[A-Za-z0-9/+=]{40}['"]?)`
)

// GitHub personal access token (classic + fine-grained): a 4-char ghX_ prefix
// then fixed-length alphanumeric/underscore.
const patGitHubToken = `\bgh[opusr]_[A-Za-z0-9_]{36,251}\b`

// Slack tokens: xoxb-/xoxa-/xoxp-/xoxr-/xoxs-/xoxe- then numeric ids and a tail.
const patSlackToken = `\bxox[abeprs]-(?:[0-9]+-){2,}[A-Za-z0-9-]{20,}\b`

// Generic shapes.
const (
	// JSON Web Token — three base64url segments joined by dots, redacted only
	// when the header decodes plausibly (leading eyJ).
	patJWT = `\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`

	// SSH/PEM private key block — the BEGIN/END marker pair as one region.
	patPEMPrivateKey = `-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----` +
		`[\s\S]+?` +
		`-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----`

	// Authorization: Bearer <token> HTTP header.
	patBearerHeader = `(?i:\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._\-+/=]+))`

	// password= / passwd= / pwd= shapes (RE2-incompatible: (?=...) lookahead). // pragma: allowlist secret
	patGenericPassword = `(?i:\b(?:password|passwd|pwd)\s*[:=]\s*['"]?(\S{1,128}?)['"]?(?=\s|$|,|;|&))`

	// api[_-]key= shapes (RE2-incompatible: (?=...) lookahead).
	patGenericAPIKey = `(?i:\bapi[_-]?key\s*[:=]\s*['"]?(\S{6,128}?)['"]?(?=\s|$|,|;|&))`

	// token= shape (RE2-incompatible: (?=...) lookahead).
	patGenericToken = `(?i:\btoken\s*[:=]\s*['"]?(\S{8,256}?)['"]?(?=\s|$|,|;|&))`
)

// DefaultRules returns the canonical default set of output-redaction rules. Port
// of redaction_defaults.default_rules. High-confidence anchored formats first,
// broad generic shapes last (the combined regexp makes order irrelevant to
// correctness — it matters only for reading the source).
func DefaultRules() []RedactionRule {
	return []RedactionRule{
		// High-confidence, anchored credential formats.
		{Pattern: patAWSAccessKeyID, Replacement: "[AWS_ACCESS_KEY_REDACTED]"},
		{Pattern: patAWSSecretAccessKey, Replacement: "[AWS_SECRET_REDACTED]"},
		{Pattern: patGitHubToken, Replacement: "[GITHUB_TOKEN_REDACTED]"},
		{Pattern: patSlackToken, Replacement: "[SLACK_TOKEN_REDACTED]"},
		{Pattern: patJWT, Replacement: "[JWT_REDACTED]"},
		{Pattern: patPEMPrivateKey, Replacement: "[PRIVATE_KEY_REDACTED]"},
		// Generic shapes — broader patterns, placed after the specific ones.
		{Pattern: patBearerHeader, Replacement: "Authorization: Bearer [REDACTED]"},
		{Pattern: patGenericPassword, Replacement: "[PASSWORD_REDACTED]"},
		{Pattern: patGenericAPIKey, Replacement: "[API_KEY_REDACTED]"},
		{Pattern: patGenericToken, Replacement: "[TOKEN_REDACTED]"},
	}
}
