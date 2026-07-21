//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// WebhookAuthorizationProvider ports Python WebhookAuthorizationProvider:
// every decision is delegated to an external webhook. When Secret is non-empty,
// responses must carry a valid X-Uterm-Signature (fail closed on unsigned allow).
type WebhookAuthorizationProvider struct {
	URL                   string
	Secret                string
	TimeoutS              float64
	RequireSignedResponse bool
	HTTPClient            *http.Client
	Now                   func() float64
	// Host check optional: when set, called before POST (egress).
	AssertURLAllowed func(ctx context.Context, rawURL string) error
}

// NewWebhookAuthorizationProvider builds a provider. requireSigned defaults true
// when secret is non-empty (parity with Python).
func NewWebhookAuthorizationProvider(url, secret string, timeoutS float64) *WebhookAuthorizationProvider {
	if timeoutS <= 0 {
		timeoutS = 2.0
	}
	return &WebhookAuthorizationProvider{
		URL:                   url,
		Secret:                secret,
		TimeoutS:              timeoutS,
		RequireSignedResponse: strings.TrimSpace(secret) != "",
		HTTPClient:            &http.Client{Timeout: time.Duration(timeoutS * float64(time.Second))},
		Now:                   wallClock,
	}
}

func (w *WebhookAuthorizationProvider) signedHeaders(body []byte) http.Header {
	h := make(http.Header)
	h.Set("Content-Type", "application/json")
	if strings.TrimSpace(w.Secret) != "" {
		ts := strconv.FormatFloat(w.Now(), 'f', -1, 64)
		h.Set("X-Uterm-Timestamp", ts)
		h.Set("X-Uterm-Signature", BuildWebhookSignature(w.Secret, body, ts))
	}
	return h
}

func (w *WebhookAuthorizationProvider) responseSignatureOK(body []byte, hdr http.Header) bool {
	if !w.RequireSignedResponse {
		return true
	}
	sig := hdr.Get("X-Uterm-Signature")
	if sig == "" {
		sig = hdr.Get("x-uterm-signature")
	}
	ts := hdr.Get("X-Uterm-Timestamp")
	if ts == "" {
		ts = hdr.Get("x-uterm-timestamp")
	}
	now := w.Now()
	return VerifyWebhookSignature(w.Secret, body, sig, ts, DefaultMaxAgeS, &now)
}

func (w *WebhookAuthorizationProvider) check(p *Principal, action string, extra map[string]any) bool {
	if p == nil {
		return false
	}
	roles := make([]string, 0, len(p.Roles))
	for r := range p.Roles {
		roles = append(roles, r)
	}
	scopes := make([]string, 0, len(p.Scopes))
	for s := range p.Scopes {
		scopes = append(scopes, s)
	}
	if extra == nil {
		extra = map[string]any{}
	}
	payload := map[string]any{
		"principal": map[string]any{
			"subject_id": p.SubjectID,
			"roles":      roles,
			"scopes":     scopes,
			"claims":     p.Claims,
		},
		"action":  action,
		"context": extra,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(w.TimeoutS*float64(time.Second)))
	defer cancel()
	if w.AssertURLAllowed != nil {
		if err := w.AssertURLAllowed(ctx, w.URL); err != nil {
			return false
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.URL, bytes.NewReader(body))
	if err != nil {
		return false
	}
	req.Header = w.signedHeaders(body)
	client := w.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return false
	}
	if resp.StatusCode != http.StatusOK {
		return false
	}
	if !w.responseSignatureOK(raw, resp.Header) {
		return false
	}
	var parsed struct {
		Allow bool `json:"allow"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return false
	}
	return parsed.Allow
}

// CapabilitiesFor posts action=capabilities; empty on error.
func (w *WebhookAuthorizationProvider) CapabilitiesFor(p *Principal) Set {
	if p == nil {
		return NewSet()
	}
	payload := map[string]any{
		"subject_id": p.SubjectID,
		"action":     "capabilities",
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return NewSet()
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(w.TimeoutS*float64(time.Second)))
	defer cancel()
	if w.AssertURLAllowed != nil {
		if err := w.AssertURLAllowed(ctx, w.URL); err != nil {
			return NewSet()
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.URL, bytes.NewReader(body))
	if err != nil {
		return NewSet()
	}
	req.Header = w.signedHeaders(body)
	client := w.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return NewSet()
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil || resp.StatusCode != http.StatusOK || !w.responseSignatureOK(raw, resp.Header) {
		return NewSet()
	}
	var parsed struct {
		Capabilities []string `json:"capabilities"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return NewSet()
	}
	return NewSet(parsed.Capabilities...)
}

func (w *WebhookAuthorizationProvider) HasCapability(p *Principal, capability string) bool {
	return w.check(p, capability, nil)
}

func (w *WebhookAuthorizationProvider) IsAdmin(p *Principal) bool {
	return w.check(p, "admin", nil)
}

func (w *WebhookAuthorizationProvider) IsOwner(p *Principal, session *serverconfig.SessionDefinition) bool {
	sid := ""
	if session != nil {
		sid = session.SessionID
	}
	return w.check(p, "session.owner", map[string]any{"session_id": sid})
}

func (w *WebhookAuthorizationProvider) CanReadSession(p *Principal, session *serverconfig.SessionDefinition) bool {
	sid := ""
	if session != nil {
		sid = session.SessionID
	}
	return w.check(p, "session.read", map[string]any{"session_id": sid})
}

func (w *WebhookAuthorizationProvider) CanReadRecording(p *Principal, session *serverconfig.SessionDefinition) bool {
	sid := ""
	if session != nil {
		sid = session.SessionID
	}
	return w.check(p, "session.recording.read", map[string]any{"session_id": sid})
}

func (w *WebhookAuthorizationProvider) CanCreateSession(p *Principal) bool {
	return w.check(p, "session.control.create", nil)
}

func (w *WebhookAuthorizationProvider) CanMutateSession(p *Principal, session *serverconfig.SessionDefinition, action string) bool {
	sid := ""
	if session != nil {
		sid = session.SessionID
	}
	return w.check(p, action, map[string]any{"session_id": sid})
}

func (w *WebhookAuthorizationProvider) CanReadProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool {
	pid := ""
	if profile != nil {
		pid = profile.ProfileID
	}
	return w.check(p, "profile.read", map[string]any{"profile_id": pid})
}

func (w *WebhookAuthorizationProvider) CanMutateProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool {
	pid := ""
	if profile != nil {
		pid = profile.ProfileID
	}
	return w.check(p, "profile.mutate", map[string]any{"profile_id": pid})
}

func (w *WebhookAuthorizationProvider) ResolveBrowserRole(p *Principal, session *serverconfig.SessionDefinition) string {
	if p == nil {
		return "viewer"
	}
	roles := make([]string, 0, len(p.Roles))
	for r := range p.Roles {
		roles = append(roles, r)
	}
	sid := ""
	if session != nil {
		sid = session.SessionID
	}
	payload := map[string]any{
		"principal": map[string]any{
			"subject_id": p.SubjectID,
			"roles":      roles,
		},
		"session_id": sid,
		"action":     "resolve_role",
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return "viewer"
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(w.TimeoutS*float64(time.Second)))
	defer cancel()
	if w.AssertURLAllowed != nil {
		if err := w.AssertURLAllowed(ctx, w.URL); err != nil {
			return "viewer"
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.URL, bytes.NewReader(body))
	if err != nil {
		return "viewer"
	}
	req.Header = w.signedHeaders(body)
	client := w.HTTPClient
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return "viewer"
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil || resp.StatusCode != http.StatusOK || !w.responseSignatureOK(raw, resp.Header) {
		return "viewer"
	}
	var parsed struct {
		Role string `json:"role"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return "viewer"
	}
	switch strings.ToLower(strings.TrimSpace(parsed.Role)) {
	case "admin", "operator", "viewer":
		return strings.ToLower(strings.TrimSpace(parsed.Role))
	default:
		return "viewer"
	}
}

// Ensure interface compliance.
var _ AuthorizationProvider = (*WebhookAuthorizationProvider)(nil)

// NewAuthorizationServiceFromConfig picks local RBAC or webhook authz.
func NewAuthorizationServiceFromConfig(cfg *serverconfig.UtermServerConfig) *AuthorizationService {
	if cfg == nil || cfg.Governance.AuthzWebhookURL == nil || strings.TrimSpace(*cfg.Governance.AuthzWebhookURL) == "" {
		return NewAuthorizationService()
	}
	g := cfg.Governance
	hmacKey := strPtr(g.AuthzWebhookSecret)
	timeout := g.AuthzWebhookTimeoutS
	p := NewWebhookAuthorizationProvider(*g.AuthzWebhookURL, hmacKey, timeout)
	return NewAuthorizationServiceWith(p)
}

func strPtr(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// String for debugging.
func (w *WebhookAuthorizationProvider) String() string {
	return fmt.Sprintf("WebhookAuthorizationProvider(%s)", w.URL)
}
