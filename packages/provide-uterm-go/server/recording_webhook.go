//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
)

// WebhookRecordingStore is a recording.Store that delegates to an External
// Management Tier webhook. Port of provide.uterm.server.recording.
// WebhookRecordingStore.
//
// Writes POST {"session_id", "action", ...payload} to URL (action "start"
// with "metadata", "append" with "events", "end"); reads GET
// URL/{session_id}/meta and URL/{session_id}/entries. Every call is best
// effort, as in the reference: the egress guard runs first, a refused target
// or any failure is swallowed, writes never return an error, and a read that
// fails comes back as an empty recording. Redirects are not followed, as the
// reference's HTTP client does not follow them.
type WebhookRecordingStore struct {
	URL     string
	Secret  string
	Timeout time.Duration

	guard  *EgressGuard
	client *http.Client
}

var _ recording.Store = (*WebhookRecordingStore)(nil)

// NewWebhookRecordingStore builds a webhook store; guard is the SSRF guard
// every request passes first (assert_webhook_target_allowed).
func NewWebhookRecordingStore(rawURL, secret string, timeout time.Duration, guard *EgressGuard) *WebhookRecordingStore {
	return &WebhookRecordingStore{
		URL:     rawURL,
		Secret:  secret,
		Timeout: timeout,
		guard:   guard,
		client: &http.Client{
			Timeout:       timeout,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

// StartSession posts the "start" action with the recording's metadata.
func (s *WebhookRecordingStore) StartSession(sessionID string, metadata map[string]any) error {
	s.post(sessionID, "start", map[string]any{"metadata": metadata})
	return nil
}

// AppendEvents posts the "append" action with a batch of events.
func (s *WebhookRecordingStore) AppendEvents(sessionID string, events []recording.Event) error {
	s.post(sessionID, "append", map[string]any{"events": events})
	return nil
}

// EndSession posts the "end" action.
func (s *WebhookRecordingStore) EndSession(sessionID string) error {
	s.post(sessionID, "end", map[string]any{})
	return nil
}

// RecordingMeta reads the endpoint's meta, or an absent recording's when the
// endpoint gives nothing usable.
func (s *WebhookRecordingStore) RecordingMeta(sessionID string) (recording.Meta, error) {
	missing := recording.Meta{SessionID: sessionID}
	raw := s.get(sessionID, "meta", nil)
	var probe map[string]any
	if json.Unmarshal(raw, &probe) != nil || len(probe) == 0 {
		return missing, nil
	}
	var meta recording.Meta
	if json.Unmarshal(raw, &meta) != nil {
		return missing, nil
	}
	return meta, nil
}

// GetEntries reads the endpoint's entries, passing the query through.
func (s *WebhookRecordingStore) GetEntries(sessionID string, q recording.Query) ([]recording.Event, error) {
	limit := q.Limit
	if limit == 0 {
		limit = 200 // the reference's get_entries default
	}
	params := url.Values{"limit": {strconv.Itoa(limit)}}
	if q.Offset != nil {
		params.Set("offset", strconv.Itoa(*q.Offset))
	}
	if q.Event != "" {
		params.Set("event", q.Event)
	}
	var body struct {
		Entries []recording.Event `json:"entries"`
	}
	if json.Unmarshal(s.get(sessionID, "entries", params), &body) != nil || body.Entries == nil {
		return []recording.Event{}, nil
	}
	return body.Entries, nil
}

// GetPath reports no local file.
func (s *WebhookRecordingStore) GetPath(string) (string, error) { return "", nil }

func (s *WebhookRecordingStore) allowed(ctx context.Context) bool {
	return s.guard.AssertWebhookTargetAllowed(ctx, s.URL) == nil
}

func (s *WebhookRecordingStore) authorize(req *http.Request) {
	if s.Secret != "" {
		req.Header.Set("Authorization", "Bearer "+s.Secret)
	}
}

func (s *WebhookRecordingStore) post(sessionID, action string, payload map[string]any) {
	ctx, cancel := context.WithTimeout(context.Background(), s.Timeout)
	defer cancel()
	if !s.allowed(ctx) {
		return
	}
	data := map[string]any{"session_id": sessionID, "action": action}
	for k, v := range payload {
		data[k] = v
	}
	body, err := json.Marshal(data)
	if err != nil {
		return
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.URL, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	s.authorize(req)
	if resp, err := s.client.Do(req); err == nil {
		_ = resp.Body.Close()
	}
}

// get returns the body of a 200 answer, or nil.
func (s *WebhookRecordingStore) get(sessionID, action string, params url.Values) []byte {
	ctx, cancel := context.WithTimeout(context.Background(), s.Timeout)
	defer cancel()
	if !s.allowed(ctx) {
		return nil
	}
	target := s.URL + "/" + url.PathEscape(sessionID) + "/" + action
	if len(params) > 0 {
		target += "?" + params.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil
	}
	s.authorize(req)
	resp, err := s.client.Do(req)
	if err != nil {
		return nil
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil
	}
	return raw
}
