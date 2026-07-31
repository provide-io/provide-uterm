//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

type securityScenarioClaim struct {
	Status   string         `json:"status"`
	Expected map[string]any `json:"expected"`
}

type securityScenario struct {
	ID       string                           `json:"id"`
	Expected map[string]any                   `json:"expected"`
	Backends map[string]securityScenarioClaim `json:"backends"`
}

type securityContract struct {
	Scenarios []securityScenario `json:"scenarios"`
}

type securityObservation struct {
	ID                    string            `json:"id"`
	Status                string            `json:"status"`
	StatusCode            int               `json:"status_code"`
	Error                 *string           `json:"error"`
	ApprovalRequired      bool              `json:"approval_required"`
	ApprovalID            *string           `json:"approval_id"`
	DeliveredWorkers      []string          `json:"delivered_workers"`
	ObserverNotifications []string          `json:"observer_notifications"`
	FailedMembers         []string          `json:"failed_members"`
	Output                map[string]string `json:"output"`
}

func observationError(value string) *string { return &value }

func emptySecurityObservation(s securityScenario, code int, err *string) securityObservation {
	return securityObservation{
		ID: s.ID, Status: s.Backends["go"].Status, StatusCode: code, Error: err,
		DeliveredWorkers: []string{}, ObserverNotifications: []string{}, FailedMembers: []string{},
		Output: map[string]string{},
	}
}

func resultSecurityObservation(s securityScenario, result Result, fh *fakeHub) securityObservation {
	observation := emptySecurityObservation(s, 200, nil)
	for _, call := range fh.sendCalls() {
		if fh.connected[call.WorkerID] {
			observation.DeliveredWorkers = append(observation.DeliveredWorkers, call.WorkerID)
		}
	}
	for _, call := range fh.broadcastCalls() {
		if call.Msg["type"] == "fanout_input" {
			observation.ObserverNotifications = append(observation.ObserverNotifications, call.WorkerID)
		}
	}
	observation.FailedMembers = append(observation.FailedMembers, result.FailedSessions...)
	for _, entry := range result.Results {
		if entry.OK && entry.OutputDelta != nil {
			observation.Output[entry.WorkerID] = *entry.OutputDelta
		}
	}
	return observation
}

func scenarioController(t *testing.T, members, connected []string, denied map[string]bool, output string) (*Controller, *fakeHub) {
	t.Helper()
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, connected...)
	fh.onSend = func(workerID string) {
		if fh.connected[workerID] {
			bus.Enqueue(workerID, map[string]any{"type": "term", "data": map[string]any{"data": output}})
		}
	}
	authorizer := allowAllAuthorizer()
	authorizer.denied = denied
	controller := NewController(fh, Config{
		Clock: hub.NewManualClock(1), IDGen: func() string { return "send" }, Authorizer: authorizer,
	})
	if _, err := controller.CreateGroup(newGroup(t, members, nil), "admin"); err != nil {
		t.Fatalf("CreateGroup: %v", err)
	}
	return controller, fh
}

func executeSecurityScenario(t *testing.T, s securityScenario) securityObservation {
	t.Helper()
	switch s.ID {
	case "unauthenticated_refusal":
		controller, fh := scenarioController(t, []string{"w1"}, []string{"w1"}, nil, "ok")
		_, err := controller.Send(context.Background(), "g1", "id", nil, 0, 0)
		if !errors.Is(err, ErrPrincipalRequired) || len(fh.sendCalls()) != 0 {
			t.Fatalf("unauthenticated refusal: err=%v sends=%v", err, fh.sendCalls())
		}
		return emptySecurityObservation(s, 401, observationError("authentication_required"))
	case "viewer_public_session_refusal":
		controller, fh := scenarioController(t, []string{"w1"}, []string{"w1"}, nil, "ok")
		viewer := &serverauth.Principal{SubjectID: "viewer", Roles: serverauth.NewSet("viewer"), Scopes: serverauth.NewSet("*")}
		_, err := controller.Send(context.Background(), "g1", "id", viewer, 0, 0)
		if !errors.Is(err, ErrAdminRequired) || len(fh.sendCalls()) != 0 {
			t.Fatalf("viewer refusal: err=%v sends=%v", err, fh.sendCalls())
		}
		return emptySecurityObservation(s, 403, observationError("global_admin_required"))
	case "dormant_member_default_reject":
		return emptySecurityObservation(s, 400, observationError("unknown_member"))
	case "dormant_member_permissive_admission":
		controller, _ := scenarioController(t, []string{"missing"}, nil, nil, "")
		if controller.GetGroup("g1", "admin") == nil {
			t.Fatal("permissive dormant group was not stored")
		}
		return emptySecurityObservation(s, 200, nil)
	case "current_authorization_revocation":
		controller, fh := scenarioController(t, []string{"w1", "w2"}, []string{"w1", "w2"}, map[string]bool{"w2": true}, "ok")
		result, err := controller.Send(context.Background(), "g1", "id", adminPrincipal("admin"), 0, 0)
		if err != nil {
			t.Fatal(err)
		}
		return resultSecurityObservation(s, result, fh)
	case "group_grant_non_bypass":
		controller, fh := scenarioController(t, []string{"w1"}, []string{"w1"}, map[string]bool{"w1": true}, "ok")
		controller.GrantAccess("g1", "admin", "admin")
		result, err := controller.Send(context.Background(), "g1", "id", adminPrincipal("admin"), 0, 0)
		if err != nil {
			t.Fatal(err)
		}
		return resultSecurityObservation(s, result, fh)
	case "partial_member_failure":
		controller, fh := scenarioController(t, []string{"w1", "w2"}, []string{"w1"}, nil, "ok")
		result, err := controller.Send(context.Background(), "g1", "id", adminPrincipal("admin"), 0, 0)
		if err != nil {
			t.Fatal(err)
		}
		return resultSecurityObservation(s, result, fh)
	case "policy_deny", "policy_hold_release":
		fh := newFakeHub(hub.NewEventBus(hub.EventBusOptions{}), "w1")
		controller := NewController(fh, Config{})
		_, _ = controller.CreateGroup(newGroup(t, []string{"w1"}, nil), "admin")
		_, err := controller.Send(context.Background(), "g1", "id", adminPrincipal("admin"), 0, 0)
		if !errors.Is(err, ErrAuthorizerUnavailable) || len(fh.sendCalls()) != 0 || len(fh.broadcastCalls()) != 0 {
			t.Fatalf("unsupported governance did not fail closed: err=%v", err)
		}
		return emptySecurityObservation(s, 501, observationError("unsupported_fail_closed"))
	case "missing_controller_dependencies":
		fh := newFakeHub(hub.NewEventBus(hub.EventBusOptions{}), "w1")
		controller := NewController(fh, Config{})
		_, _ = controller.CreateGroup(newGroup(t, []string{"w1"}, nil), "admin")
		_, err := controller.Send(context.Background(), "g1", "id", adminPrincipal("admin"), 0, 0)
		if !errors.Is(err, ErrAuthorizerUnavailable) || len(fh.sendCalls()) != 0 {
			t.Fatalf("missing dependency refusal: err=%v sends=%v", err, fh.sendCalls())
		}
		return emptySecurityObservation(s, 403, observationError("authorization_unavailable"))
	case "immediate_output_capture":
		controller, fh := scenarioController(t, []string{"w1"}, []string{"w1"}, nil, "immediate")
		result, err := controller.Send(context.Background(), "g1", "id", adminPrincipal("admin"), 0, 0)
		if err != nil {
			t.Fatal(err)
		}
		return resultSecurityObservation(s, result, fh)
	case "store_read_isolation":
		store := NewInMemoryStore()
		store.Save(newGroup(t, []string{"w1"}, nil))
		read, _ := store.Get("g1")
		read.WorkerIDs = append(read.WorkerIDs, "w2")
		again, _ := store.Get("g1")
		if len(again.WorkerIDs) != 1 {
			t.Fatalf("store alias escaped: %v", again.WorkerIDs)
		}
		return emptySecurityObservation(s, 200, nil)
	case "store_atomic_update":
		store := NewInMemoryStore()
		store.Save(newGroup(t, []string{"w1"}, nil))
		var wait sync.WaitGroup
		for _, grantee := range []string{"alice", "bob"} {
			wait.Add(1)
			go func(value string) { defer wait.Done(); store.GrantAccess("g1", value, "admin") }(grantee)
		}
		wait.Wait()
		group, _ := store.Get("g1")
		sort.Strings(group.Grants)
		if !reflect.DeepEqual(group.Grants, []string{"alice", "bob"}) {
			t.Fatalf("atomic grants = %v", group.Grants)
		}
		return emptySecurityObservation(s, 200, nil)
	default:
		t.Fatalf("unimplemented applicable scenario %q", s.ID)
		return securityObservation{}
	}
}

func runGoRouteScenarioTests(t *testing.T) {
	t.Helper()
	command := exec.Command("go", "test", "./server", "-run", "^(TestFanoutRequiresAuth|TestFanoutRequiresGlobalAdminBeforeParsingOrLookup|TestFanoutRejectsUnknownMembersByDefault|TestFanoutExplicitlyAllowsDormantMembers|TestFanoutRefusesConfiguredUnsupportedGovernance)$", "-count=1")
	command.Dir = ".."
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("server fan-out route scenarios: %v\n%s", err, output)
	}
}

func expectedGoScenario(s securityScenario) map[string]any {
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
	var contract securityContract
	if err := json.Unmarshal(content, &contract); err != nil {
		t.Fatal(err)
	}
	runGoRouteScenarioTests(t)
	observations := make([]securityObservation, 0)
	applicable := map[string]bool{}
	for _, scenario := range contract.Scenarios {
		if scenario.Backends["go"].Status == "unserved" {
			continue
		}
		applicable[scenario.ID] = true
		observation := executeSecurityScenario(t, scenario)
		actualJSON, _ := json.Marshal(observation)
		var actual map[string]any
		_ = json.Unmarshal(actualJSON, &actual)
		for key, value := range expectedGoScenario(scenario) {
			if !reflect.DeepEqual(actual[key], value) {
				t.Fatalf("%s.%s = %#v, want %#v", scenario.ID, key, actual[key], value)
			}
		}
		observations = append(observations, observation)
	}
	if len(observations) != len(applicable) {
		t.Fatalf("observed %d IDs, want %d", len(observations), len(applicable))
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
