//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

type lifecycleClaim struct {
	Status   string         `json:"status"`
	Expected map[string]any `json:"expected"`
}

type lifecycleScenario struct {
	ID    string `json:"id"`
	Input struct {
		Operation      string `json:"operation"`
		Transport      string `json:"transport"`
		WorkerID       string `json:"worker_id"`
		Payload        string `json:"payload"`
		FragmentCount  int    `json:"fragment_count"`
		OversizedBytes int    `json:"oversized_bytes"`
		Case           string `json:"case"`
	} `json:"input"`
	Expected map[string]any            `json:"expected"`
	Backends map[string]lifecycleClaim `json:"backends"`
}

type lifecycleContract struct {
	ResultDefaults map[string]any      `json:"result_defaults"`
	Scenarios      []lifecycleScenario `json:"scenarios"`
}

type lifecycleObservation struct {
	ID                      string   `json:"id"`
	Status                  string   `json:"status"`
	Route                   string   `json:"route"`
	StatusCode              int      `json:"status_code"`
	Error                   *string  `json:"error"`
	FragmentCount           int      `json:"fragment_count"`
	AcceptedConnections     int      `json:"accepted_connections"`
	RejectedConnections     int      `json:"rejected_connections"`
	QuotaRecovered          bool     `json:"quota_recovered"`
	DeliveredPayloads       []string `json:"delivered_payloads"`
	ResumeSucceeded         bool     `json:"resume_succeeded"`
	OwnershipRestored       bool     `json:"ownership_restored"`
	ReplayRejected          bool     `json:"replay_rejected"`
	NonOwnerRefused         bool     `json:"non_owner_refused"`
	PreFinalActions         int      `json:"pre_final_actions"`
	PostFinalActions        int      `json:"post_final_actions"`
	OversizedRefused        bool     `json:"oversized_refused"`
	SetupRollbackVerified   bool     `json:"setup_rollback_verified"`
	PolicyDecision          *string  `json:"policy_decision"`
	SignedRequest           bool     `json:"signed_request"`
	CompetingOwnerPreserved bool     `json:"competing_owner_preserved"`
}

func emptyLifecycleObservation(s lifecycleScenario) lifecycleObservation {
	return lifecycleObservation{
		ID: s.ID, Status: s.Backends["go"].Status, DeliveredPayloads: []string{},
	}
}

func lifecycleString(value string) *string { return &value }

func TestSessionLifecycleSecurityScenarios(t *testing.T) {
	contractPath := os.Getenv("SESSION_LIFECYCLE_SCENARIO_CONTRACT")
	if contractPath == "" {
		contractPath = filepath.Join("..", "..", "..", "spec", "session_lifecycle_security_scenarios.json")
	}
	content, err := os.ReadFile(contractPath)
	if err != nil {
		t.Fatal(err)
	}
	var contract lifecycleContract
	if err := json.Unmarshal(content, &contract); err != nil {
		t.Fatal(err)
	}

	observations := make([]lifecycleObservation, 0, len(contract.Scenarios))
	applicable := make([]lifecycleScenario, 0, len(contract.Scenarios))
	for _, scenario := range contract.Scenarios {
		if scenario.Backends["go"].Status == "unserved" {
			continue
		}
		applicable = append(applicable, scenario)
		observations = append(observations, executeLifecycleScenario(t, scenario))
	}
	if output := os.Getenv("SESSION_LIFECYCLE_SCENARIO_OUTPUT"); output != "" {
		encoded, err := json.MarshalIndent(observations, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(output, append(encoded, '\n'), 0o600); err != nil {
			t.Fatal(err)
		}
		return
	}
	for index, scenario := range applicable {
		actualJSON, _ := json.Marshal(observations[index])
		var actual map[string]any
		_ = json.Unmarshal(actualJSON, &actual)
		expected := map[string]any{}
		for key, value := range contract.ResultDefaults {
			expected[key] = value
		}
		for key, value := range scenario.Expected {
			expected[key] = value
		}
		for key, value := range scenario.Backends["go"].Expected {
			expected[key] = value
		}
		for key, value := range expected {
			if !reflect.DeepEqual(actual[key], value) {
				t.Fatalf("%s.%s=%#v want=%#v", scenario.ID, key, actual[key], value)
			}
		}
	}
}

func executeLifecycleScenario(t *testing.T, scenario lifecycleScenario) lifecycleObservation {
	t.Helper()
	switch scenario.Input.Operation {
	case "fragment_message":
		return executeLifecycleFragmentation(t, scenario)
	case "browser_quota":
		return executeLifecycleQuota(t, scenario)
	case "governed_input":
		return executeLifecycleUnsupportedGovernance(t, scenario)
	case "resume_ownership":
		return executeLifecycleResume(t, scenario)
	case "non_owner_hijack_step":
		return executeLifecycleNonOwnerStep(t, scenario)
	default:
		t.Fatalf("unsupported lifecycle operation %q", scenario.Input.Operation)
		return lifecycleObservation{}
	}
}

func executeLifecycleFragmentation(t *testing.T, scenario lifecycleScenario) lifecycleObservation {
	t.Helper()
	input := scenario.Input
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add(input.WorkerID, "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	observation := emptyLifecycleObservation(scenario)
	observation.Route = input.Transport + "_websocket"
	observation.StatusCode = http.StatusSwitchingProtocols
	observation.FragmentCount = input.FragmentCount

	switch input.Transport {
	case "browser":
		worker := ts.setupWorker(t, input.WorkerID)
		browser := dialBrowser(t, ctx, wsBase+"/ws/browser/"+input.WorkerID+"/term", "admin1", "admin")
		browser.waitFrame(t, "hello", 5*time.Second)
		browser.send(t, ctx, map[string]any{"type": "hijack_request"})
		browser.waitFrameWhere(t, "hijack_state", 5*time.Second, func(frame map[string]any) bool { return frame["owner"] == "me" })
		baseline := len(workerSent(worker))
		encoded, err := controlchannel.EncodeControlFrame(map[string]any{"type": "input", "data": input.Payload})
		if err != nil {
			t.Fatal(err)
		}
		writeLifecycleFragments(t, ctx, browser.conn, websocket.MessageText, []byte(encoded), input.FragmentCount, func() {
			observation.PreFinalActions = len(workerSent(worker)) - baseline
		})
		waitUntil(t, 5*time.Second, func() bool {
			for _, sent := range workerSent(worker)[baseline:] {
				if sent == input.Payload {
					return true
				}
			}
			return false
		})
		observation.DeliveredPayloads = []string{input.Payload}
		observation.PostFinalActions = 1
		_ = browser.conn.Write(ctx, websocket.MessageText, bytes.Repeat([]byte("x"), input.OversizedBytes))
		waitUntil(t, 5*time.Second, func() bool { return ts.hub.BrowserCount(context.Background(), input.WorkerID) == 0 })
		observation.OversizedRefused = true
	case "worker", "tunnel":
		path := "/ws/worker/" + input.WorkerID + "/term"
		messageType := websocket.MessageText
		payload := []byte(input.Payload)
		if input.Transport == "tunnel" {
			path = "/tunnel/" + input.WorkerID
			messageType = websocket.MessageBinary
			payload = tunnelclient.EncodeFrame(tunnelclient.ChannelData, payload, tunnelclient.FlagData)
		}
		worker, _, err := websocket.Dial(ctx, wsBase+path, nil)
		if err != nil {
			t.Fatalf("%s dial: %v", input.Transport, err)
		}
		defer func() { _ = worker.Close(websocket.StatusNormalClosure, "") }()
		waitUntil(t, 5*time.Second, func() bool {
			return boolField(ts.hub.RegisterBrowserStateSnapshot(input.WorkerID, nil), "worker_online", false)
		})
		browser := dialBrowser(t, ctx, wsBase+"/ws/browser/"+input.WorkerID+"/term", "admin1", "admin")
		defer func() { _ = browser.conn.Close(websocket.StatusNormalClosure, "") }()
		browser.waitFrame(t, "hello", 5*time.Second)
		browser.send(t, ctx, map[string]any{"type": "ping"})
		browser.waitFrame(t, "pong", 5*time.Second)
		writeLifecycleFragments(t, ctx, worker, messageType, payload, input.FragmentCount, func() {
			select {
			case data := <-browser.data:
				t.Fatalf("%s acted before final fragment: %q", input.Transport, data)
			default:
			}
		})
		select {
		case data := <-browser.data:
			if data != input.Payload {
				t.Fatalf("fragmented %s payload = %q", input.Transport, data)
			}
			observation.DeliveredPayloads = []string{data}
		case <-time.After(5 * time.Second):
			t.Fatalf("fragmented %s output not delivered", input.Transport)
		}
		observation.PostFinalActions = 1
		_ = worker.Write(ctx, messageType, bytes.Repeat([]byte("x"), input.OversizedBytes))
		waitUntil(t, 5*time.Second, func() bool {
			return !boolField(ts.hub.RegisterBrowserStateSnapshot(input.WorkerID, nil), "worker_online", false)
		})
		observation.OversizedRefused = true
	default:
		t.Fatalf("unknown fragmentation transport %q", input.Transport)
	}
	return observation
}

func writeLifecycleFragments(
	t *testing.T, ctx context.Context, conn *websocket.Conn, messageType websocket.MessageType, payload []byte, count int,
	beforeClose func(),
) {
	t.Helper()
	writer, err := conn.Writer(ctx, messageType)
	if err != nil {
		t.Fatal(err)
	}
	for index := 0; index < count; index++ {
		start := len(payload) * index / count
		end := len(payload) * (index + 1) / count
		if _, err := writer.Write(payload[start:end]); err != nil {
			t.Fatal(err)
		}
	}
	beforeClose()
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
}

func executeLifecycleQuota(t *testing.T, scenario lifecycleScenario) lifecycleObservation {
	t.Helper()
	var setupCalls atomic.Int32
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock: deps.Clock, MaxConnectionsPerPrincipal: 1, OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
		deps.BrowserSetupHook = func() error {
			if setupCalls.Add(1) == 1 {
				return context.Canceled
			}
			return nil
		}
	})
	ts.srv.MarkReady()
	ts.reg.add(scenario.Input.WorkerID, "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsURL := "ws" + strings.TrimPrefix(httpSrv.URL, "http") + "/ws/browser/" + scenario.Input.WorkerID + "/term"
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	headers := http.Header{"X-Subject": {"quota-user"}, "X-Role": {"admin"}}

	failed, _, err := websocket.Dial(ctx, wsURL, &websocket.DialOptions{HTTPHeader: headers})
	if err != nil {
		t.Fatalf("failed-setup dial: %v", err)
	}
	_, _, _ = failed.Read(ctx)
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.BrowserCount(context.Background(), scenario.Input.WorkerID) == 0 })
	rollback := ts.hub.BrowserCount(context.Background(), scenario.Input.WorkerID) == 0

	first := dialBrowserWithHeaders(t, ctx, wsURL, headers)
	first.waitFrame(t, "hello", 5*time.Second)
	rejected, _, err := websocket.Dial(ctx, wsURL, &websocket.DialOptions{HTTPHeader: headers})
	if err != nil {
		t.Fatalf("quota rejection dial: %v", err)
	}
	_, _, rejectionErr := rejected.Read(ctx)
	status := websocket.CloseStatus(rejectionErr)
	if status != websocket.StatusPolicyViolation {
		t.Fatalf("quota close status = %d, want 1008", status)
	}
	_ = first.conn.Close(websocket.StatusNormalClosure, "")
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.BrowserCount(context.Background(), scenario.Input.WorkerID) == 0 })
	recovered := dialBrowserWithHeaders(t, ctx, wsURL, headers)
	recovered.waitFrame(t, "hello", 5*time.Second)
	_ = recovered.conn.Close(websocket.StatusNormalClosure, "")

	observation := emptyLifecycleObservation(scenario)
	observation.Route = "browser_websocket"
	observation.StatusCode = int(websocket.StatusPolicyViolation)
	observation.Error = lifecycleString("too_many_connections")
	observation.AcceptedConnections = 2
	observation.RejectedConnections = 1
	observation.QuotaRecovered = true
	observation.SetupRollbackVerified = rollback
	return observation
}

func executeLifecycleUnsupportedGovernance(t *testing.T, scenario lifecycleScenario) lifecycleObservation {
	t.Helper()
	url := "https://policy.example.test/input"
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Governance.PolicyWebhookURL = &url
	})
	ts.srv.MarkReady()
	worker := ts.setupWorker(t, scenario.Input.WorkerID)
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()

	client := httpSrv.Client()
	createBody := `{"name":"governed","worker_ids":["` + scenario.Input.WorkerID + `"]}`
	code, body := lifecycleHTTPRequest(t, client, http.MethodPost, httpSrv.URL+"/api/fanout/groups", createBody)
	if code != http.StatusOK {
		t.Fatalf("create governed group = %d: %s", code, body)
	}
	var created map[string]any
	if err := json.Unmarshal(body, &created); err != nil {
		t.Fatal(err)
	}
	groupID, _ := created["group_id"].(string)
	before := len(workerSent(worker))
	code, body = lifecycleHTTPRequest(t, client, http.MethodPost, httpSrv.URL+"/api/fanout/groups/"+groupID+"/send", `{"data":"`+scenario.Input.Payload+`"}`)
	if code != http.StatusNotImplemented || len(workerSent(worker)) != before {
		t.Fatalf("configured governance = %d body=%s worker=%v", code, body, workerSent(worker))
	}

	observation := emptyLifecycleObservation(scenario)
	observation.Route = "http"
	observation.StatusCode = http.StatusNotImplemented
	observation.Error = lifecycleString("unsupported_governance")
	observation.PolicyDecision = lifecycleString("unsupported")
	return observation
}

func lifecycleHTTPRequest(t *testing.T, client *http.Client, method, url, body string) (int, []byte) {
	t.Helper()
	req, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Subject", "admin1")
	req.Header.Set("X-Role", "admin")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	var response bytes.Buffer
	_, _ = response.ReadFrom(resp.Body)
	return resp.StatusCode, response.Bytes()
}

func executeLifecycleResume(t *testing.T, scenario lifecycleScenario) lifecycleObservation {
	t.Helper()
	ts := resumeTestServer(t)
	ts.reg.add(scenario.Input.WorkerID, "admin1", "public")
	ts.setupWorker(t, scenario.Input.WorkerID)
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	path := base + "/ws/browser/" + scenario.Input.WorkerID + "/term"

	original := dialBrowser(t, ctx, path, "resume-user", "admin")
	hello := original.waitFrame(t, "hello", 5*time.Second)
	token, _ := hello["resume_token"].(string)
	original.send(t, ctx, map[string]any{"type": "hijack_request"})
	original.waitFrameWhere(t, "hijack_state", 5*time.Second, func(frame map[string]any) bool { return frame["owner"] == "me" })
	_ = original.conn.Close(websocket.StatusNormalClosure, "")
	waitUntil(t, 5*time.Second, func() bool { return !ts.hub.CheckStillHijacked(scenario.Input.WorkerID) })

	observation := emptyLifecycleObservation(scenario)
	observation.Route = "browser_websocket"
	observation.StatusCode = http.StatusSwitchingProtocols
	if scenario.Input.Case == "current_owner" {
		reconnected := dialBrowser(t, ctx, path, "resume-user", "admin")
		defer func() { _ = reconnected.conn.Close(websocket.StatusNormalClosure, "") }()
		reconnected.waitFrame(t, "hello", 5*time.Second)
		reconnected.send(t, ctx, map[string]any{"type": "resume", "token": token})
		resumed := reconnected.waitFrameWhere(t, "hello", 5*time.Second, func(frame map[string]any) bool { return frame["resumed"] == true })
		observation.ResumeSucceeded = true
		observation.OwnershipRestored = resumed["hijacked_by_me"] == true
		reconnected.send(t, ctx, map[string]any{"type": "resume", "token": token})
		reconnected.send(t, ctx, map[string]any{"type": "ping"})
		observation.ReplayRejected = !lifecycleResumedBeforePong(t, reconnected)
		return observation
	}

	competitor := dialBrowser(t, ctx, path, "new-owner", "admin")
	defer func() { _ = competitor.conn.Close(websocket.StatusNormalClosure, "") }()
	competitor.waitFrame(t, "hello", 5*time.Second)
	competitor.send(t, ctx, map[string]any{"type": "hijack_request"})
	competitor.waitFrameWhere(t, "hijack_state", 5*time.Second, func(frame map[string]any) bool { return frame["owner"] == "me" })
	stale := dialBrowser(t, ctx, path, "resume-user", "admin")
	defer func() { _ = stale.conn.Close(websocket.StatusNormalClosure, "") }()
	stale.waitFrame(t, "hello", 5*time.Second)
	stale.send(t, ctx, map[string]any{"type": "resume", "token": token})
	stale.send(t, ctx, map[string]any{"type": "ping"})
	observation.ResumeSucceeded = lifecycleResumedBeforePong(t, stale)
	competitor.send(t, ctx, map[string]any{"type": "heartbeat"})
	competitor.waitFrame(t, "heartbeat_ack", 5*time.Second)
	observation.CompetingOwnerPreserved = !observation.ResumeSucceeded
	return observation
}

func lifecycleResumedBeforePong(t *testing.T, browser *browserClient) bool {
	t.Helper()
	resumed := false
	deadline := time.After(5 * time.Second)
	for {
		select {
		case frame := <-browser.frames:
			if frame["type"] == "hello" && frame["resumed"] == true {
				resumed = true
			}
			if frame["type"] == "pong" {
				return resumed
			}
		case <-deadline:
			t.Fatal("timed out waiting for resume replay probe")
			return false
		}
	}
}

func executeLifecycleNonOwnerStep(t *testing.T, scenario lifecycleScenario) lifecycleObservation {
	t.Helper()
	ts := newTestServer(t, nil)
	worker := ts.setupWorker(t, scenario.Input.WorkerID)
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	path := base + "/ws/browser/" + scenario.Input.WorkerID + "/term"
	owner := dialBrowser(t, ctx, path, "owner-user", "admin")
	defer func() { _ = owner.conn.Close(websocket.StatusNormalClosure, "") }()
	owner.waitFrame(t, "hello", 5*time.Second)
	owner.send(t, ctx, map[string]any{"type": "hijack_request"})
	owner.waitFrameWhere(t, "hijack_state", 5*time.Second, func(frame map[string]any) bool { return frame["owner"] == "me" })
	nonOwner := dialBrowser(t, ctx, path, "other-user", "admin")
	defer func() { _ = nonOwner.conn.Close(websocket.StatusNormalClosure, "") }()
	nonOwner.waitFrame(t, "hello", 5*time.Second)
	before := len(workerSent(worker))
	nonOwner.send(t, ctx, map[string]any{"type": "hijack_step"})
	nonOwner.send(t, ctx, map[string]any{"type": "ping"})
	nonOwner.waitFrame(t, "pong", 5*time.Second)
	after := workerSent(worker)
	refused := len(after) == before
	for _, payload := range after[before:] {
		if strings.Contains(payload, `"action":"step"`) {
			refused = false
		}
	}
	observation := emptyLifecycleObservation(scenario)
	observation.Route = "browser_websocket"
	observation.StatusCode = http.StatusSwitchingProtocols
	observation.NonOwnerRefused = refused
	return observation
}
