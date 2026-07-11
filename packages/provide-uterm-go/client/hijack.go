//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// DefaultTimeout is the per-request timeout used when none is configured, in
// seconds — matching the Python HijackClient default of 20.0s.
const DefaultTimeout = 20 * time.Second

// DefaultEntityPrefix is the worker path prefix ("/worker" for provide-uterm,
// "/agent" for agent compatibility).
const DefaultEntityPrefix = "/worker"

// safeIDPattern matches a single safe URL path segment: no "/" (route forging)
// and no bare dot-segments (path traversal). Dotted ids like "session.1" are
// allowed; "." and ".." are rejected. Mirrors _ID_RE in hijack.py.
var safeIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

// safeID returns value if it is a single safe path segment, else an error.
// Caller/LLM-supplied ids are interpolated into request paths, so an id like
// "../../api/keys" would let the URL resolve to a different server route,
// escaping the per-method authz model. Port of _safe_id in hijack.py.
func safeID(value, kind string) (string, error) {
	if value == "" || value == "." || value == ".." || !safeIDPattern.MatchString(value) {
		return "", fmt.Errorf("invalid %s: %q", kind, value)
	}
	return value, nil
}

// HijackClient is a concurrency-safe REST client for the provide-uterm hijack +
// session API. It is a port of provide.uterm.client.hijack.HijackClient and
// hits the same paths, verbs, JSON bodies, and query params so it is
// cross-compatible with the Python server.
//
// The zero value is not usable; construct one with NewHijackClient. A single
// HijackClient (and its underlying *http.Client) is safe for concurrent use by
// multiple goroutines.
type HijackClient struct {
	baseURL      string
	entityPrefix string
	timeout      time.Duration
	headers      map[string]string
	httpClient   *http.Client
}

// Option configures a HijackClient.
type Option func(*HijackClient)

// WithEntityPrefix sets the worker path prefix (default "/worker").
func WithEntityPrefix(prefix string) Option {
	return func(c *HijackClient) { c.entityPrefix = strings.TrimRight(prefix, "/") }
}

// WithTimeout sets the default per-request timeout (default 20s).
func WithTimeout(d time.Duration) Option {
	return func(c *HijackClient) { c.timeout = d }
}

// WithHeaders sets extra headers sent with every request (e.g. auth tokens).
func WithHeaders(headers map[string]string) Option {
	return func(c *HijackClient) {
		c.headers = make(map[string]string, len(headers))
		for k, v := range headers {
			c.headers[k] = v
		}
	}
}

// WithHTTPClient injects a custom *http.Client (e.g. for a test transport).
// The client's own Timeout field is left untouched; per-request deadlines are
// applied via context so per-call timeout overrides keep working.
func WithHTTPClient(hc *http.Client) Option {
	return func(c *HijackClient) { c.httpClient = hc }
}

// NewHijackClient returns a HijackClient rooted at baseURL (e.g.
// "http://localhost:8780"). A trailing slash on baseURL is stripped.
func NewHijackClient(baseURL string, opts ...Option) *HijackClient {
	c := &HijackClient{
		baseURL:      strings.TrimRight(baseURL, "/"),
		entityPrefix: DefaultEntityPrefix,
		timeout:      DefaultTimeout,
		headers:      map[string]string{},
	}
	for _, opt := range opts {
		opt(c)
	}
	if c.httpClient == nil {
		c.httpClient = &http.Client{}
	}
	return c
}

// -- path helpers ---------------------------------------------------------

// wp builds the worker path "<prefix>/<worker_id>".
func (c *HijackClient) wp(workerID string) (string, error) {
	id, err := safeID(workerID, "worker_id")
	if err != nil {
		return "", err
	}
	return c.entityPrefix + "/" + id, nil
}

// hp builds the hijack path "<prefix>/<worker_id>/hijack/<hijack_id>".
func (c *HijackClient) hp(workerID, hijackID string) (string, error) {
	wid, err := safeID(workerID, "worker_id")
	if err != nil {
		return "", err
	}
	hid, err := safeID(hijackID, "hijack_id")
	if err != nil {
		return "", err
	}
	return c.entityPrefix + "/" + wid + "/hijack/" + hid, nil
}

// sp builds the session path "/api/sessions/<session_id>".
func (c *HijackClient) sp(sessionID string) (string, error) {
	id, err := safeID(sessionID, "session_id")
	if err != nil {
		return "", err
	}
	return "/api/sessions/" + id, nil
}

// -- request core ---------------------------------------------------------

// doRequest issues an HTTP request and returns the decoded body. On success it
// returns (body, nil). On a non-2xx response or a transport failure it returns
// (nil, *APIError) whose Body carries the decoded response, mirroring the
// (ok, body) contract of the Python _request helper.
func (c *HijackClient) doRequest(
	ctx context.Context,
	method, path string,
	body map[string]any,
	params url.Values,
	timeout time.Duration,
) (any, error) {
	logger := ptel.GetLogger(ctx, "provide.uterm.client.hijack")

	if timeout <= 0 {
		timeout = c.timeout
	}
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	fullURL, err := c.buildURL(path, params)
	if err != nil {
		return nil, c.transportError(logger, method, path, err)
	}

	var reader io.Reader
	if body != nil {
		encoded, mErr := json.Marshal(body)
		if mErr != nil {
			return nil, c.transportError(logger, method, path, mErr)
		}
		reader = bytes.NewReader(encoded)
	}

	req, err := http.NewRequestWithContext(reqCtx, method, fullURL, reader)
	if err != nil {
		return nil, c.transportError(logger, method, path, err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	for k, v := range c.headers {
		req.Header.Set(k, v)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, c.transportError(logger, method, path, err)
	}
	defer func() { _ = resp.Body.Close() }()

	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, c.transportError(logger, method, path, err)
	}

	decoded := decodeBody(raw)
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return decoded, nil
	}

	logger.Warn("HijackClient request failed",
		"method", method, "path", path,
		"status", resp.StatusCode, "body", sanitize(decoded))
	return nil, &APIError{
		StatusCode: resp.StatusCode,
		Body:       decoded,
		Message:    extractError(decoded),
	}
}

// buildURL joins the base URL, path, and query params into an absolute URL.
func (c *HijackClient) buildURL(path string, params url.Values) (string, error) {
	u, err := url.Parse(c.baseURL + path)
	if err != nil {
		return "", err
	}
	if len(params) > 0 {
		u.RawQuery = params.Encode()
	}
	return u.String(), nil
}

// transportError logs and wraps a transport-level failure, matching the
// Python client's (False, {"error": str(exc)}) fallback.
func (c *HijackClient) transportError(logger *slog.Logger, method, path string, err error) *APIError {
	logger.Warn("HijackClient request failed", "method", method, "path", path, "error", err.Error())
	return &APIError{
		StatusCode: 0,
		Transport:  true,
		Message:    err.Error(),
		Body:       map[string]any{"error": err.Error()},
	}
}

// requestObject issues a request whose successful body is a JSON object.
func (c *HijackClient) requestObject(
	ctx context.Context,
	method, path string,
	body map[string]any,
	params url.Values,
	timeout time.Duration,
) (map[string]any, error) {
	v, err := c.doRequest(ctx, method, path, body, params, timeout)
	if err != nil {
		return nil, err
	}
	if m, ok := v.(map[string]any); ok {
		return m, nil
	}
	// Defensive: an object endpoint returned a non-object body.
	return map[string]any{"raw": v}, nil
}

// requestAny issues a request whose successful body may be any JSON value
// (used by the list/array session endpoints).
func (c *HijackClient) requestAny(
	ctx context.Context,
	method, path string,
	params url.Values,
	timeout time.Duration,
) (any, error) {
	return c.doRequest(ctx, method, path, nil, params, timeout)
}

// decodeBody parses raw response bytes into a JSON value, falling back to
// {"raw": <text>} when the body is not valid JSON (matching Python's r.json()
// / {"raw": r.text} fallback).
func decodeBody(raw []byte) any {
	var v any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return map[string]any{"raw": string(raw)}
	}
	return v
}

// extractError renders a short error message from a decoded body: the "error"
// string field when present, else a compact JSON rendering.
func extractError(body any) string {
	if m, ok := body.(map[string]any); ok {
		if e, ok := m["error"].(string); ok {
			return e
		}
	}
	// body is always a value decoded from JSON (string/bool/nil/json.Number/
	// slice/map), so it always re-marshals; on the impossible error path
	// encoded is nil and we return "".
	encoded, _ := json.Marshal(body)
	return string(encoded)
}

func (c *HijackClient) GUIScreenshot(ctx context.Context, workerID, hijackID string) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	return c.requestObject(ctx, "GET", path+"/gui/screenshot", nil, nil, 0)
}

func (c *HijackClient) GUIClick(ctx context.Context, workerID, hijackID string, x, y int, button string) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"x": x, "y": y, "button": button}
	return c.requestObject(ctx, "POST", path+"/gui/click", body, nil, 0)
}

func (c *HijackClient) GUIType(ctx context.Context, workerID, hijackID string, text string) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"text": text}
	return c.requestObject(ctx, "POST", path+"/gui/type", body, nil, 0)
}

func (c *HijackClient) GUIKey(ctx context.Context, workerID, hijackID string, keyName string) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"key_name": keyName}
	return c.requestObject(ctx, "POST", path+"/gui/key", body, nil, 0)
}

func (c *HijackClient) GUIDrag(ctx context.Context, workerID, hijackID string, startX, startY, endX, endY int) (map[string]any, error) {
	path, err := c.hp(workerID, hijackID)
	if err != nil {
		return nil, err
	}
	body := map[string]any{"start_x": startX, "start_y": startY, "end_x": endX, "end_y": endY}
	return c.requestObject(ctx, "POST", path+"/gui/drag", body, nil, 0)
}
