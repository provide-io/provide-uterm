//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"bytes"
	"context"
	"crypto/hmac"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// WebhookIDPOptions configures a WebhookIdentityProvider; zero values take the
// auth_webhook.WebhookIdentityProvider.__init__ defaults.
type WebhookIDPOptions struct {
	Secret                string
	TimeoutS              float64
	OnFailure             string // "deny" (default) | "viewer"
	RequireSignedResponse *bool  // nil = default true
	ForwardHeaders        Set
	ForwardCookies        Set
	RequireResponseNonce  bool
	// AuditHook, when set, receives the structured failure audit event
	// (action, detail) mirroring audit_event("auth.webhook_idp_failure", ...).
	AuditHook func(action string, detail map[string]any)
}

// WebhookIdentityProvider ports auth_webhook.WebhookIdentityProvider — an
// IdentityProvider that delegates resolution to an external signed webhook.
type WebhookIdentityProvider struct {
	URL                   string
	Secret                string
	TimeoutS              float64
	OnFailure             string
	RequireSignedResponse bool
	ForwardHeaders        Set
	ForwardCookies        Set
	RequireResponseNonce  bool

	replay     *boundedReplayCache
	httpClient *http.Client
	resolver   HostResolver
	now        func() float64
	nonceGen   func() string
	auditHook  func(action string, detail map[string]any)
	logger     *slog.Logger
}

// NewWebhookIdentityProvider ports the __init__, including the on_failure
// validation (must be "deny" or "viewer").
func NewWebhookIdentityProvider(url string, opts WebhookIDPOptions) (*WebhookIdentityProvider, error) {
	onFailure := opts.OnFailure
	if onFailure == "" {
		onFailure = "deny"
	}
	if onFailure != "deny" && onFailure != "viewer" {
		return nil, fmt.Errorf("on_failure must be 'deny' or 'viewer'; got %q", onFailure)
	}
	requireSigned := true
	if opts.RequireSignedResponse != nil {
		requireSigned = *opts.RequireSignedResponse
	}
	timeout := opts.TimeoutS
	if timeout == 0 {
		timeout = 2.0
	}
	forwardHeaders := opts.ForwardHeaders
	if forwardHeaders == nil {
		forwardHeaders = NewSet()
	}
	forwardCookies := opts.ForwardCookies
	if forwardCookies == nil {
		forwardCookies = NewSet()
	}
	return &WebhookIdentityProvider{
		URL:                   url,
		Secret:                opts.Secret,
		TimeoutS:              timeout,
		OnFailure:             onFailure,
		RequireSignedResponse: requireSigned,
		ForwardHeaders:        forwardHeaders,
		ForwardCookies:        forwardCookies,
		RequireResponseNonce:  opts.RequireResponseNonce,
		replay:                newBoundedReplayCache(DefaultMaxAgeS, replayCacheMaxEntries),
		httpClient:            &http.Client{Timeout: time.Duration(timeout * float64(time.Second))},
		resolver:              defaultResolver,
		now:                   wallClock,
		nonceGen:              func() string { return tokenURLSafe(16) },
		auditHook:             opts.AuditHook,
		logger:                ptel.GetLogger(context.Background(), "provide.uterm.server.auth_webhook"),
	}, nil
}

type webhookRequestPayload struct {
	Headers map[string]string `json:"headers"`
	Cookies map[string]string `json:"cookies"`
	Action  string            `json:"action"`
	Nonce   string            `json:"nonce"`
}

// Authenticate ports resolve_principal: delegate identity resolution to the
// external webhook, verifying the response signature, replay window, and nonce
// binding; fail closed (deny → nil) unless on_failure="viewer".
func (w *WebhookIdentityProvider) Authenticate(ctx context.Context, req *Request) (*Principal, error) {
	principal, err := w.resolve(ctx, req)
	if err == nil {
		return principal, nil
	}
	w.logger.Warn("webhook_auth_failed", "url", w.URL, "error", err, "on_failure", w.OnFailure)
	if w.auditHook != nil {
		w.auditHook("auth.webhook_idp_failure",
			map[string]any{"url": w.URL, "on_failure": w.OnFailure, "error": err.Error()})
	}
	if w.OnFailure == "viewer" {
		return &Principal{SubjectID: "anonymous", Roles: NewSet("viewer"), Scopes: NewSet(), Claims: map[string]any{}}, nil
	}
	return nil, nil
}

func (w *WebhookIdentityProvider) resolve(ctx context.Context, req *Request) (*Principal, error) {
	headers := filterMap(req.Headers, func(k string) bool { return w.ForwardHeaders.Has(strings.ToLower(k)) })
	cookies := filterMap(req.Cookies, func(k string) bool { return w.ForwardCookies.Has(k) })

	nonce := w.nonceGen()
	body, err := json.Marshal(webhookRequestPayload{
		Headers: headers, Cookies: cookies, Action: "resolve_principal", Nonce: nonce,
	})
	if err != nil {
		return nil, err
	}

	reqHeaders := map[string]string{"Content-Type": "application/json", "X-Uterm-Nonce": nonce}
	if w.Secret != "" {
		ts := strconv.FormatFloat(w.now(), 'f', -1, 64)
		reqHeaders["X-Uterm-Timestamp"] = ts
		reqHeaders["X-Uterm-Signature"] = BuildWebhookSignature(w.Secret, body, ts)
	}

	if err := AssertWebhookTargetAllowed(ctx, w.URL, w.resolver); err != nil {
		return nil, err
	}
	respBody, respHeaders, err := w.post(ctx, body, reqHeaders)
	if err != nil {
		return nil, err
	}

	if err := w.verifyResponse(respBody, respHeaders, nonce); err != nil {
		return nil, err
	}
	return w.principalFromResponse(respBody)
}

func (w *WebhookIdentityProvider) post(ctx context.Context, body []byte, headers map[string]string) ([]byte, http.Header, error) {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, w.URL, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	for k, v := range headers {
		httpReq.Header.Set(k, v)
	}
	resp, err := w.httpClient.Do(httpReq)
	if err != nil {
		return nil, nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	content, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, nil, fmt.Errorf("webhook IdP returned status %d", resp.StatusCode)
	}
	return content, resp.Header, nil
}

// verifyResponse ports the response-signature, replay, and nonce-binding checks.
func (w *WebhookIdentityProvider) verifyResponse(respBody []byte, respHeaders http.Header, nonce string) error {
	now := w.now()
	if w.RequireSignedResponse {
		if !VerifyWebhookSignature(w.Secret, respBody,
			respHeaders.Get("X-Uterm-Signature"), respHeaders.Get("X-Uterm-Timestamp"), DefaultMaxAgeS, &now) {
			return errors.New("webhook IdP response signature verification failed")
		}
	}
	respSig := respHeaders.Get("X-Uterm-Signature")
	if respSig != "" && w.replay.seenOrRecord(respSig, now) {
		return errors.New("webhook IdP response replay detected")
	}

	var data map[string]any
	if err := json.Unmarshal(respBody, &data); err != nil {
		return err
	}
	echoed, present := data["nonce"]
	if w.RequireResponseNonce {
		if !present || echoed == nil || !hmac.Equal([]byte(asStr(echoed)), []byte(nonce)) {
			return errors.New("webhook IdP response nonce missing or mismatched")
		}
	} else if present && echoed != nil && !hmac.Equal([]byte(asStr(echoed)), []byte(nonce)) {
		return errors.New("webhook IdP response nonce mismatched")
	}
	return nil
}

func (w *WebhookIdentityProvider) principalFromResponse(respBody []byte) (*Principal, error) {
	var data map[string]any
	if err := json.Unmarshal(respBody, &data); err != nil {
		return nil, err
	}
	subject, ok := data["subject_id"].(string)
	if !ok {
		return nil, errors.New("webhook IdP response missing subject_id")
	}
	roles := stringList(data["roles"])
	if roles == nil {
		roles = []string{defaultRole}
	}
	scopes := NewSet()
	for _, s := range stringList(data["scopes"]) {
		scopes[s] = struct{}{}
	}
	claims, _ := data["claims"].(map[string]any)
	if claims == nil {
		claims = map[string]any{}
	}
	var displayName *string
	if dn, ok := data["display_name"].(string); ok {
		displayName = &dn
	}
	return &Principal{
		SubjectID:   subject,
		Roles:       FilterKnownRoles(roles),
		Scopes:      scopes,
		Claims:      claims,
		DisplayName: displayName,
	}, nil
}

func filterMap(m map[string]string, keep func(string) bool) map[string]string {
	out := map[string]string{}
	for k, v := range m {
		if keep(k) {
			out[k] = v
		}
	}
	return out
}

func stringList(v any) []string {
	items, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, asStr(it))
	}
	return out
}
