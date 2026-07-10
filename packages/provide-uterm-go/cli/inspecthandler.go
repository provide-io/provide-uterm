//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// hopHeadersOut are stripped from the request before forwarding upstream
// (matching inspect.py's fwd_headers filter).
var hopHeadersOut = map[string]struct{}{"host": {}, "transfer-encoding": {}}

// respStripHeaders are removed from the upstream response before it is written
// back to the local client (matching inspect.py's resp_headers filter).
var respStripHeaders = map[string]struct{}{"transfer-encoding": {}, "content-encoding": {}}

// handle is the reverse-proxy handler: it inspects, optionally intercepts, then
// forwards the request to the local target and relays the response. It is the Go
// port of inspect.py's _proxy_app.
func (s *inspectSession) handle(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	reqBody, _ := io.ReadAll(r.Body)
	method := r.Method
	path := r.URL.Path
	qs := r.URL.RawQuery
	targetURL := fmt.Sprintf("http://127.0.0.1:%d%s", s.targetPort, path)
	if qs != "" {
		targetURL += "?" + qs
	}
	reqHeaders := flattenHeaders(r.Header)
	reqCT := reqHeaders["content-type"]
	rid := s.nextRID()

	urlForEvent := path
	if qs != "" {
		urlForEvent = path + "?" + qs
	}

	if s.gate.InspectEnabled() {
		event := map[string]any{
			"type": "http_req", "id": rid, "ts": nowUnix(),
			"method": method, "url": urlForEvent, "headers": reqHeaders,
			"intercepted": s.gate.Enabled(),
		}
		for k, v := range tunnelclient.EncodeBody(reqBody, reqCT) {
			event[k] = v
		}
		s.sendHTTP(ctx, event)
		logLine := tunnelclient.FormatLogLine(method, path, -1, -1, len(reqBody))
		_, _ = fmt.Fprintln(s.errw, logLine)
		_ = s.client.SendData(ctx, []byte(logLine+"\n"), tunnelclient.ChannelData)
	}

	fwdHeaders := filterHeaders(reqHeaders, hopHeadersOut)
	fwdBody := reqBody

	if s.gate.Enabled() && s.gate.InspectEnabled() {
		decision := s.gate.AwaitDecision(ctx, rid)
		switch decision.Action {
		case "drop":
			s.writeDrop(ctx, w, rid)
			return
		case "modify":
			if decision.Headers != nil {
				fwdHeaders = decision.Headers
			}
			if decision.Body != nil {
				fwdBody = decision.Body
			}
		}
	}

	s.forward(ctx, w, forwardReq{
		method: method, url: targetURL, path: path, rid: rid,
		headers: fwdHeaders, body: fwdBody,
	})
}

// forwardReq bundles the resolved upstream request parameters.
type forwardReq struct {
	method, url, path, rid string
	headers                map[string]string
	body                   []byte
}

// forward sends the request upstream and relays the response, emitting the
// http_res inspection event and log line when inspection is enabled.
func (s *inspectSession) forward(ctx context.Context, w http.ResponseWriter, fr forwardReq) {
	t0 := time.Now()
	req, err := http.NewRequestWithContext(ctx, fr.method, fr.url, bytes.NewReader(fr.body))
	if err != nil {
		s.writeBadGateway(w, err)
		return
	}
	for k, v := range fr.headers {
		req.Header.Set(k, v)
	}
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse // httpx does not follow redirects by default
	}}
	upstream, err := client.Do(req)
	if err != nil {
		s.writeBadGateway(w, err)
		return
	}
	defer func() { _ = upstream.Body.Close() }()
	respBody, _ := io.ReadAll(upstream.Body)
	durationMs := float64(time.Since(t0).Microseconds()) / 1000.0

	if s.gate.InspectEnabled() {
		respHeaders := flattenHeaders(upstream.Header)
		event := map[string]any{
			"type": "http_res", "id": fr.rid, "ts": nowUnix(),
			"status": upstream.StatusCode, "status_text": statusText(upstream.StatusCode),
			"headers": respHeaders, "duration_ms": round1(durationMs),
		}
		for k, v := range tunnelclient.EncodeBody(respBody, upstream.Header.Get("Content-Type")) {
			event[k] = v
		}
		s.sendHTTP(ctx, event)
		logLine := tunnelclient.FormatLogLine(fr.method, fr.path, upstream.StatusCode, durationMs, len(respBody))
		_, _ = fmt.Fprintln(s.errw, logLine)
		_ = s.client.SendData(ctx, []byte(logLine+"\n"), tunnelclient.ChannelData)
	}

	for k, vals := range upstream.Header {
		if _, strip := respStripHeaders[strings.ToLower(k)]; strip {
			continue
		}
		for _, v := range vals {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(upstream.StatusCode)
	_, _ = w.Write(respBody)
}

// writeDrop responds 502 for an intercepted-and-dropped request and emits the
// synthetic http_res "Dropped" event.
func (s *inspectSession) writeDrop(ctx context.Context, w http.ResponseWriter, rid string) {
	s.sendHTTP(ctx, map[string]any{
		"type": "http_res", "id": rid, "ts": nowUnix(),
		"status": 502, "status_text": "Dropped", "headers": map[string]any{},
		"body_size": 0, "duration_ms": 0,
	})
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(http.StatusBadGateway)
	_, _ = w.Write([]byte("Request dropped by interceptor"))
}

// writeBadGateway responds 502 for an upstream forwarding failure.
func (s *inspectSession) writeBadGateway(w http.ResponseWriter, err error) {
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(http.StatusBadGateway)
	_, _ = fmt.Fprintf(w, "Bad Gateway: %v", err)
}

// receiveActions reads http_action / toggle messages from the tunnel and drives
// the intercept gate. It is the Go port of inspect.py's _ws_action_receiver:
// binary frames carry ChannelHTTP JSON, text frames carry bare JSON.
func (s *inspectSession) receiveActions(ctx context.Context) {
	for {
		isText, data, err := s.client.RecvMessage(ctx)
		if err != nil {
			s.gate.CancelAll("forward")
			return
		}
		msg, ok := decodeActionMessage(isText, data)
		if !ok {
			continue
		}
		s.dispatchAction(ctx, msg)
	}
}

// decodeActionMessage parses one inbound message into a JSON object, applying
// the same binary/text rules as inspect.py (text frames must be one of the
// known action types).
func decodeActionMessage(isText bool, data []byte) (map[string]any, bool) {
	var msg map[string]any
	if isText {
		if json.Unmarshal(data, &msg) != nil {
			return nil, false
		}
		switch typeOf(msg) {
		case "http_action", "http_intercept_toggle", "http_inspect_toggle":
			return msg, true
		default:
			return nil, false
		}
	}
	if len(data) <= 2 {
		return nil, false
	}
	frame, derr := tunnelclient.DecodeFrame(data)
	if derr != nil || frame.Channel != tunnelclient.ChannelHTTP {
		return nil, false
	}
	if json.Unmarshal(frame.Payload, &msg) != nil {
		return nil, false
	}
	return msg, true
}

// dispatchAction applies one decoded action message to the gate.
func (s *inspectSession) dispatchAction(ctx context.Context, msg map[string]any) {
	switch typeOf(msg) {
	case "http_action":
		decision := tunnelclient.ParseActionMessage(msg)
		rid, _ := msg["id"].(string)
		s.gate.Resolve(rid, decision)
	case "http_intercept_toggle":
		s.gate.SetEnabled(boolOf(msg["enabled"], false))
		if !s.gate.Enabled() {
			s.gate.CancelAll("forward")
		}
		s.broadcastState(ctx)
	case "http_inspect_toggle":
		s.gate.SetInspectEnabled(boolOf(msg["enabled"], true))
		if !s.gate.InspectEnabled() {
			s.gate.CancelAll("forward")
			s.gate.SetEnabled(false)
		}
		s.broadcastState(ctx)
	}
}

// flattenHeaders lowercases header keys and keeps the last value for duplicates,
// matching the ASGI-scope dict comprehension in inspect.py.
func flattenHeaders(h http.Header) map[string]string {
	out := make(map[string]string, len(h))
	for k, vals := range h {
		if len(vals) > 0 {
			out[strings.ToLower(k)] = vals[len(vals)-1]
		}
	}
	return out
}

// filterHeaders returns a copy of headers with any key present in drop removed.
func filterHeaders(headers map[string]string, drop map[string]struct{}) map[string]string {
	out := make(map[string]string, len(headers))
	for k, v := range headers {
		if _, bad := drop[strings.ToLower(k)]; bad {
			continue
		}
		out[k] = v
	}
	return out
}

func typeOf(msg map[string]any) string { t, _ := msg["type"].(string); return t }

// boolOf coerces a JSON value to bool, using def when the key is absent/null.
func boolOf(v any, def bool) bool {
	if b, ok := v.(bool); ok {
		return b
	}
	return def
}

func nowUnix() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func round1(v float64) float64 { return float64(int64(v*10+0.5)) / 10 }

// statusText returns the reason phrase for a status code (upstream.reason_phrase
// analogue).
func statusText(code int) string { return http.StatusText(code) }
