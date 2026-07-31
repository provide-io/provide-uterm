//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fanout"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

type semanticActor struct {
	Subject       string   `json:"subject"`
	Authenticated bool     `json:"authenticated"`
	Roles         []string `json:"roles"`
}

type semanticGroup struct {
	ID                  string   `json:"id"`
	Creator             string   `json:"creator"`
	Members             []string `json:"members"`
	Grants              []string `json:"grants"`
	AllowUnknownMembers bool     `json:"allow_unknown_members"`
}

type semanticInput struct {
	Surface    string        `json:"surface"`
	Operation  string        `json:"operation"`
	Actor      semanticActor `json:"actor"`
	Group      semanticGroup `json:"group"`
	Visibility struct {
		ReadableMembers  []string `json:"readable_members"`
		RevokeBeforeSend []string `json:"revoke_before_send"`
	} `json:"visibility"`
	Policy struct {
		Action string `json:"action"`
	} `json:"policy"`
	Workers struct {
		AcceptedMembers []string          `json:"accepted_members"`
		ImmediateOutput map[string]string `json:"immediate_output"`
		Continuous      bool              `json:"continuous_output"`
	} `json:"workers"`
	Command          string   `json:"command"`
	OmitAuthorizers  bool     `json:"omit_authorizers"`
	MutationMember   string   `json:"mutation_member"`
	ConcurrentGrants []string `json:"concurrent_grants"`
	MaxResponseMS    int      `json:"max_response_ms"`
}

type semanticClaim struct {
	Status   string         `json:"status"`
	Expected map[string]any `json:"expected"`
}

type semanticScenario struct {
	ID       string                   `json:"id"`
	Input    semanticInput            `json:"input"`
	Expected map[string]any           `json:"expected"`
	Backends map[string]semanticClaim `json:"backends"`
}

type semanticContract struct {
	Scenarios []semanticScenario `json:"scenarios"`
}

type semanticObservation struct {
	ID                    string            `json:"id"`
	Status                string            `json:"status"`
	StatusCode            int               `json:"status_code"`
	Error                 *string           `json:"error"`
	ApprovalRequired      bool              `json:"approval_required"`
	ApprovalID            *string           `json:"approval_id"`
	Command               string            `json:"command"`
	DeliveredWorkers      []string          `json:"delivered_workers"`
	ObserverNotifications []string          `json:"observer_notifications"`
	FailedMembers         []string          `json:"failed_members"`
	Output                map[string]string `json:"output"`
}

func semanticError(value string) *string { return &value }

func emptySemanticObservation(s semanticScenario, code int, err *string) semanticObservation {
	return semanticObservation{
		ID: s.ID, Status: s.Backends["go"].Status, StatusCode: code, Error: err, Command: s.Input.Command,
		DeliveredWorkers: []string{}, ObserverNotifications: []string{}, FailedMembers: []string{},
		Output: map[string]string{},
	}
}

type semanticHub struct {
	mu        sync.Mutex
	bus       *hub.EventBus
	accepted  map[string]bool
	output    map[string]string
	delivered []string
	observers []string
}

func newSemanticHub(input semanticInput) *semanticHub {
	accepted := map[string]bool{}
	for _, workerID := range input.Workers.AcceptedMembers {
		accepted[workerID] = true
	}
	return &semanticHub{bus: hub.NewEventBus(hub.EventBusOptions{}), accepted: accepted, output: input.Workers.ImmediateOutput}
}

func (h *semanticHub) SendWorker(_ context.Context, workerID string, _ map[string]any) (bool, error) {
	h.mu.Lock()
	accepted := h.accepted[workerID]
	if accepted {
		h.delivered = append(h.delivered, workerID)
	}
	h.mu.Unlock()
	if accepted {
		h.bus.Enqueue(workerID, map[string]any{
			"type": "term", "data": map[string]any{"data": h.output[workerID]},
		})
	}
	return accepted, nil
}

func (h *semanticHub) Broadcast(_ context.Context, workerID string, message map[string]any) error {
	if message["type"] == "fanout_input" {
		h.mu.Lock()
		h.observers = append(h.observers, workerID)
		h.mu.Unlock()
	}
	return nil
}

func (h *semanticHub) EventBus() *hub.EventBus { return h.bus }

type semanticAuthorizer struct {
	readable map[string]bool
}

func (a *semanticAuthorizer) IsGlobalAdmin(principal *serverauth.Principal) bool {
	return principal != nil && principal.Roles.Has("admin") && principal.AdminSessionScope == nil
}

func (a *semanticAuthorizer) CanReadMember(_ context.Context, _ *serverauth.Principal, workerID string) bool {
	return a.readable[workerID]
}

func semanticPrincipal(actor semanticActor) *serverauth.Principal {
	if !actor.Authenticated {
		return nil
	}
	return &serverauth.Principal{
		SubjectID: actor.Subject, Roles: serverauth.NewSet(actor.Roles...), Scopes: serverauth.NewSet("*"),
	}
}

func buildSemanticController(t *testing.T, input semanticInput) (*fanout.Controller, *semanticHub) {
	t.Helper()
	readable := map[string]bool{}
	for _, workerID := range input.Visibility.ReadableMembers {
		readable[workerID] = true
	}
	for _, workerID := range input.Visibility.RevokeBeforeSend {
		delete(readable, workerID)
	}
	h := newSemanticHub(input)
	config := fanout.Config{Clock: hub.NewManualClock(1), IDGen: func() string { return "approval" }}
	if !input.OmitAuthorizers {
		config.Authorizer = &semanticAuthorizer{readable: readable}
	}
	controller := fanout.NewController(h, config)
	group := &fanout.Group{
		GroupID: input.Group.ID, Name: "fixture-group", WorkerIDs: append([]string(nil), input.Group.Members...),
		CreatedBy: input.Group.Creator, Grants: append([]string(nil), input.Group.Grants...), Mode: "parallel",
		QuiesceMS: 1, MaxResponseMS: 100, DivergenceThreshold: 0.8,
	}
	if _, err := controller.CreateGroup(group, input.Group.Creator); err != nil {
		t.Fatal(err)
	}
	return controller, h
}

func fromSemanticResult(s semanticScenario, result fanout.Result, h *semanticHub) semanticObservation {
	observation := emptySemanticObservation(s, 200, nil)
	h.mu.Lock()
	observation.DeliveredWorkers = append(observation.DeliveredWorkers, h.delivered...)
	observation.ObserverNotifications = append(observation.ObserverNotifications, h.observers...)
	h.mu.Unlock()
	observation.Command = result.Command
	observation.FailedMembers = append(observation.FailedMembers, result.FailedSessions...)
	for _, row := range result.Results {
		if row.OK && row.OutputDelta != nil {
			observation.Output[row.WorkerID] = *row.OutputDelta
		}
	}
	return observation
}

func semanticHeaders(actor semanticActor) map[string]string {
	if !actor.Authenticated {
		return nil
	}
	role := "viewer"
	if len(actor.Roles) > 0 {
		role = actor.Roles[0]
	}
	return map[string]string{"X-Subject": actor.Subject, "X-Role": role}
}

func canonicalGoRouteError(code int, body string) *string {
	if code < 400 {
		return nil
	}
	if code == 401 {
		return semanticError("authentication_required")
	}
	if strings.Contains(body, "admin") {
		return semanticError("global_admin_required")
	}
	if strings.Contains(body, "unknown fan-out") {
		return semanticError("unknown_member")
	}
	if code == 501 {
		return semanticError("unsupported_fail_closed")
	}
	return semanticError("request_failed")
}

func executeSemanticRoute(t *testing.T, s semanticScenario) semanticObservation {
	t.Helper()
	input := s.Input
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.FanoutAllowUnknownMembers = input.Group.AllowUnknownMembers
		if input.Policy.Action != "allow" {
			url := "https://policy.example.test/fanout"
			cfg.Governance.PolicyWebhookURL = &url
		}
	})
	if input.Operation == "create" {
		body, _ := json.Marshal(map[string]any{"name": "fixture-group", "worker_ids": input.Group.Members})
		recorder := ts.do("POST", "/api/fanout/groups", string(body), semanticHeaders(input.Actor))
		return emptySemanticObservation(s, recorder.Code, canonicalGoRouteError(recorder.Code, recorder.Body.String()))
	}
	if input.Actor.Authenticated && input.Actor.Roles[0] == "admin" {
		body, _ := json.Marshal(map[string]any{"name": "fixture-group", "worker_ids": input.Group.Members})
		created := ts.do("POST", "/api/fanout/groups", string(body), map[string]string{
			"X-Subject": input.Group.Creator, "X-Role": "admin",
		})
		if created.Code == 200 {
			var response map[string]any
			_ = json.Unmarshal(created.Body.Bytes(), &response)
			input.Group.ID = response["group_id"].(string)
		}
	}
	body, _ := json.Marshal(map[string]any{"data": input.Command})
	recorder := ts.do("POST", "/api/fanout/groups/"+input.Group.ID+"/send", string(body), semanticHeaders(input.Actor))
	return emptySemanticObservation(s, recorder.Code, canonicalGoRouteError(recorder.Code, recorder.Body.String()))
}

func executeSemanticStore(t *testing.T, s semanticScenario) semanticObservation {
	t.Helper()
	input := s.Input
	store := fanout.NewInMemoryStore()
	group := &fanout.Group{GroupID: input.Group.ID, CreatedBy: input.Group.Creator, WorkerIDs: input.Group.Members}
	store.Save(group)
	switch input.Operation {
	case "store_read_isolation":
		read, _ := store.Get(input.Group.ID)
		read.WorkerIDs = append(read.WorkerIDs, input.MutationMember)
		again, _ := store.Get(input.Group.ID)
		if reflect.DeepEqual(read.WorkerIDs, again.WorkerIDs) {
			t.Fatal("store returned an aliased group")
		}
	case "store_atomic_update":
		var wait sync.WaitGroup
		for _, grantee := range input.ConcurrentGrants {
			wait.Add(1)
			go func(value string) { defer wait.Done(); store.GrantAccess(input.Group.ID, value, input.Group.Creator) }(grantee)
		}
		wait.Wait()
		stored, _ := store.Get(input.Group.ID)
		sort.Strings(stored.Grants)
		want := append([]string(nil), input.ConcurrentGrants...)
		sort.Strings(want)
		if !reflect.DeepEqual(stored.Grants, want) {
			t.Fatalf("grants=%v want=%v", stored.Grants, want)
		}
	default:
		t.Fatalf("unsupported store operation %q", input.Operation)
	}
	return emptySemanticObservation(s, 200, nil)
}

func executeSemanticScenario(t *testing.T, s semanticScenario) semanticObservation {
	t.Helper()
	if s.Input.Surface == "rest" || s.Input.Surface == "rest_release" {
		return executeSemanticRoute(t, s)
	}
	if s.Input.Surface == "store" {
		return executeSemanticStore(t, s)
	}
	controller, semanticHub := buildSemanticController(t, s.Input)
	result, err := controller.Send(
		context.Background(), s.Input.Group.ID, s.Input.Command, semanticPrincipal(s.Input.Actor), 0, s.Input.MaxResponseMS,
	)
	if err != nil {
		observation := emptySemanticObservation(s, 403, semanticError("authorization_unavailable"))
		if errors.Is(err, fanout.ErrPrincipalRequired) {
			observation.StatusCode = 401
			observation.Error = semanticError("authentication_required")
		} else if errors.Is(err, fanout.ErrAdminRequired) {
			observation.Error = semanticError("global_admin_required")
		}
		return observation
	}
	return fromSemanticResult(s, result, semanticHub)
}

func expectedSemanticScenario(s semanticScenario) map[string]any {
	expected := map[string]any{}
	for key, value := range s.Expected {
		expected[key] = value
	}
	for key, value := range s.Backends["go"].Expected {
		expected[key] = value
	}
	return expected
}

func TestFanoutSecurityScenarios(t *testing.T) {
	contractPath := os.Getenv("FANOUT_SECURITY_SCENARIO_CONTRACT")
	if contractPath == "" {
		contractPath = filepath.Join("..", "..", "..", "spec", "fanout_security_scenarios.json")
	}
	content, err := os.ReadFile(contractPath)
	if err != nil {
		t.Fatal(err)
	}
	var contract semanticContract
	if err := json.Unmarshal(content, &contract); err != nil {
		t.Fatal(err)
	}
	observations := []semanticObservation{}
	for _, scenario := range contract.Scenarios {
		if scenario.Backends["go"].Status == "unserved" {
			continue
		}
		observations = append(observations, executeSemanticScenario(t, scenario))
	}
	if os.Getenv("FANOUT_SECURITY_SCENARIO_OUTPUT") == "" {
		for index, scenario := range contract.Scenarios {
			if scenario.Backends["go"].Status == "unserved" {
				continue
			}
			actualJSON, _ := json.Marshal(observations[index])
			var actual map[string]any
			_ = json.Unmarshal(actualJSON, &actual)
			for key, expected := range expectedSemanticScenario(scenario) {
				if !reflect.DeepEqual(actual[key], expected) {
					t.Fatalf("%s.%s=%#v want=%#v", scenario.ID, key, actual[key], expected)
				}
			}
		}
	}
	if outputPath := os.Getenv("FANOUT_SECURITY_SCENARIO_OUTPUT"); outputPath != "" {
		encoded, err := json.MarshalIndent(observations, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(outputPath, append(encoded, '\n'), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}
