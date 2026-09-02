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
	"time"

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
		ReadableMembers []string `json:"readable_members"`
		// RegisteredMembers is the optional set the routes' session registry
		// knows about. Absent (nil) means "default to readable_members".
		RegisteredMembers []string `json:"registered_members"`
		// ControllerReadableMembers is the optional set the fan-out
		// controller's own authorizer treats as readable. Absent (nil) means
		// "default to readable_members". It never feeds the server/routes
		// authorizer — the contract requires the two to be able to disagree.
		ControllerReadableMembers []string `json:"controller_readable_members"`
		RevokeBeforeSend          []string `json:"revoke_before_send"`
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
	routeMemberStored     bool
	routeGroupStored      bool
	routeWorkerConnected  bool
	routeObserverAttached bool
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
	mu         sync.Mutex
	bus        *hub.EventBus
	accepted   map[string]bool
	output     map[string]string
	continuous bool
	budget     time.Duration
	delivered  []string
	observers  []string
}

func newSemanticHub(input semanticInput) *semanticHub {
	accepted := map[string]bool{}
	for _, workerID := range input.Workers.AcceptedMembers {
		accepted[workerID] = true
	}
	budgetMS := input.MaxResponseMS
	if budgetMS <= 0 {
		budgetMS = defaultMaxResponseMS
	}
	bus := hub.NewEventBus(hub.EventBusOptions{})
	if input.Workers.Continuous {
		// A worker that never stops talking, stated rather than simulated. The
		// contract is that a member with output STILL QUEUED as the collect
		// exits is reported cut short; racing real producers against the
		// collector to arrange that tested the Go scheduler, not the rule, and
		// lost -- `total_response_deadline.failed_members=[] want=["w1"]` in
		// CI. Answering the depth directly tests the derivation itself, the
		// way the TypeScript and C# harnesses already do.
		bus.PendingOverride = func(workerID string) (int, bool) {
			if accepted[workerID] {
				return 1, true
			}
			return 0, false
		}
	}
	return &semanticHub{
		bus: bus, accepted: accepted,
		output: input.Workers.ImmediateOutput, continuous: input.Workers.Continuous,
		budget: time.Duration(budgetMS) * time.Millisecond,
	}
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
		// Continuity is stated, not raced -- see newSemanticHub, where the
		// bus is told this worker always has output pending.
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

type semanticRouteRecorder struct {
	mu   sync.Mutex
	sent []string
}

func (r *semanticRouteRecorder) SendText(_ context.Context, payload string) error {
	r.mu.Lock()
	r.sent = append(r.sent, payload)
	r.mu.Unlock()
	return nil
}

func (r *semanticRouteRecorder) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.sent)
}

type semanticRouteTracking struct {
	workers          map[string]*semanticRouteRecorder
	observers        map[string]*semanticRouteRecorder
	memberStored     bool
	workerConnected  bool
	observerAttached bool
}

func trackSemanticRouteFanout(t *testing.T, ts *testServer, input semanticInput) *semanticRouteTracking {
	t.Helper()
	tracking := &semanticRouteTracking{
		workers:          map[string]*semanticRouteRecorder{},
		observers:        map[string]*semanticRouteRecorder{},
		memberStored:     len(input.Group.Members) > 0,
		workerConnected:  len(input.Workers.AcceptedMembers) > 0,
		observerAttached: len(input.Group.Members) > 0,
	}
	accepted := map[string]bool{}
	for _, workerID := range input.Workers.AcceptedMembers {
		accepted[workerID] = true
	}
	for _, workerID := range input.Group.Members {
		ts.reg.add(workerID, input.Group.Creator, "public")
		if _, ok := ts.reg.GetDefinition(context.Background(), workerID); !ok {
			tracking.memberStored = false
		}
		if accepted[workerID] {
			worker := &semanticRouteRecorder{}
			if _, err := ts.hub.RegisterWorker(context.Background(), workerID, worker); err != nil {
				t.Fatalf("register semantic route worker %s: %v", workerID, err)
			}
			tracking.workers[workerID] = worker
			state := ts.hub.Registry.Get(workerID)
			if state == nil || state.WorkerWS != worker {
				tracking.workerConnected = false
			}
		}
		observer := &semanticRouteRecorder{}
		if _, err := ts.hub.RegisterBrowser(context.Background(), workerID, observer, "admin", false); err != nil {
			t.Fatalf("register semantic route observer %s: %v", workerID, err)
		}
		tracking.observers[workerID] = observer
		state := ts.hub.Registry.Get(workerID)
		if state == nil {
			tracking.observerAttached = false
		} else if _, ok := state.Browsers[observer]; !ok {
			tracking.observerAttached = false
		}
	}
	return tracking
}

func observeSemanticRoute(
	s semanticScenario, recorderCode int, recorderBody string, tracking *semanticRouteTracking, groupStored bool,
) semanticObservation {
	observation := emptySemanticObservation(s, recorderCode, canonicalGoRouteError(recorderCode, recorderBody))
	if tracking == nil {
		return observation
	}
	observation.routeMemberStored = tracking.memberStored
	observation.routeGroupStored = groupStored
	observation.routeWorkerConnected = tracking.workerConnected
	observation.routeObserverAttached = tracking.observerAttached
	for _, workerID := range s.Input.Group.Members {
		if worker := tracking.workers[workerID]; worker != nil && worker.count() > 0 {
			observation.DeliveredWorkers = append(observation.DeliveredWorkers, workerID)
		}
		if observer := tracking.observers[workerID]; observer != nil && observer.count() > 0 {
			observation.ObserverNotifications = append(observation.ObserverNotifications, workerID)
		}
	}
	return observation
}

type semanticAuthorizer struct {
	readable map[string]bool
}

// semanticReadableSet resolves what the *server* (routes) authorizer treats as
// readable: visibility.readable_members minus the revoke_before_send
// withdrawals. It deliberately ignores controller_readable_members.
func semanticReadableSet(input semanticInput) map[string]bool {
	readable := map[string]bool{}
	for _, workerID := range input.Visibility.ReadableMembers {
		readable[workerID] = true
	}
	for _, workerID := range input.Visibility.RevokeBeforeSend {
		delete(readable, workerID)
	}
	return readable
}

// semanticControllerReadableSet resolves what the *controller's own*
// authorizer treats as readable. The contract default when
// controller_readable_members is absent (nil, not an empty list) is
// readable_members; revoke_before_send still withdraws from the result.
func semanticControllerReadableSet(input semanticInput) map[string]bool {
	members := input.Visibility.ControllerReadableMembers
	if members == nil {
		members = input.Visibility.ReadableMembers
	}
	readable := map[string]bool{}
	for _, workerID := range members {
		readable[workerID] = true
	}
	for _, workerID := range input.Visibility.RevokeBeforeSend {
		delete(readable, workerID)
	}
	return readable
}

// semanticRegisteredMembers resolves the sessions the routes' session registry
// knows about. The contract default when registered_members is absent (nil,
// not an empty list) is readable_members, and the effective set is the union
// with readable_members because a readable session has to exist to be
// readable. Order follows the group's declared membership so the positional
// "first offending member decides" rule stays observable.
func semanticRegisteredMembers(input semanticInput) []string {
	registered := input.Visibility.RegisteredMembers
	if registered == nil {
		registered = input.Visibility.ReadableMembers
	}
	seen := map[string]bool{}
	effective := []string{}
	for _, workerID := range append(append([]string(nil), registered...), input.Visibility.ReadableMembers...) {
		if !seen[workerID] {
			seen[workerID] = true
			effective = append(effective, workerID)
		}
	}
	return effective
}

// semanticRouteAuthorizer is the server-side authorization provider used by the
// contract's create scenarios. Everything but session reads delegates to the
// normal RBAC provider (so the global-admin boundary is unchanged); reads
// answer from the scenario's server-side readable set, which lets a scenario
// register a session the caller must not be able to read.
type semanticRouteAuthorizer struct {
	serverauth.LocalAuthorizationProvider
	readable map[string]bool
}

func (a *semanticRouteAuthorizer) CanReadSession(
	_ *serverauth.Principal, session *serverconfig.SessionDefinition,
) bool {
	return session != nil && a.readable[session.SessionID]
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

// defaultMaxResponseMS is the response budget for scenarios that do not ask for
// one. Nineteen of the twenty scenarios test authorization semantics; only
// total_response_deadline tests the deadline, and it names its own 20ms. So
// this number is incidental to every scenario that inherits it and must be far
// enough out that the clock never decides one.
//
// It was 100ms in all four ports. A member whose budget expires is reported in
// failed_members by design, and under load the C# port reached that state on a
// member that was authorized and delivered to, reporting [w1 w2] where the
// contract says [w2]. Go had more headroom than C# did -- C# was quiescing for
// 25ms against a 100ms budget, since raised to 5000 and lowered to 1ms like the
// rest of us -- but the fragility is the same shape, so the default moves in
// every port rather than only the one that failed first.
//
// Costs nothing: a collect returns once output has been quiet for QuiesceMS,
// not when the budget runs out.
const defaultMaxResponseMS = 5000

func buildSemanticController(t *testing.T, input semanticInput) (*fanout.Controller, *semanticHub) {
	t.Helper()
	readable := semanticControllerReadableSet(input)
	h := newSemanticHub(input)
	config := fanout.Config{Clock: hub.NewManualClock(1), IDGen: func() string { return "approval" }}
	if !input.OmitAuthorizers {
		config.Authorizer = &semanticAuthorizer{readable: readable}
	}
	controller := fanout.NewController(h, config)
	group := &fanout.Group{
		GroupID: input.Group.ID, Name: "fixture-group", WorkerIDs: append([]string(nil), input.Group.Members...),
		CreatedBy: input.Group.Creator, Grants: append([]string(nil), input.Group.Grants...), Mode: "parallel",
		QuiesceMS: 1, MaxResponseMS: defaultMaxResponseMS, DivergenceThreshold: 0.8,
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
	// "no read access" then "authorization" — checked after "admin"/"unknown
	// fan-out" to match the Python canonicalizer's precedence exactly.
	if strings.Contains(body, "no read access") {
		return semanticError("member_read_forbidden")
	}
	if strings.Contains(body, "authorization") {
		return semanticError("authorization_unavailable")
	}
	if code == 501 {
		return semanticError("unsupported_fail_closed")
	}
	return semanticError("request_failed")
}

// executeSemanticRouteCreate drives POST /api/fanout/groups against a server
// whose registry holds the scenario's effective registered set. The fan-out
// controller is rebuilt with an authorizer over controller_readable_members so
// it can be strictly more permissive than the route's: the contract requires
// that a controller which would happily accept a member still cannot widen the
// route's admission decision.
func executeSemanticRouteCreate(t *testing.T, ts *testServer, s semanticScenario) semanticObservation {
	t.Helper()
	input := s.Input
	for _, workerID := range semanticRegisteredMembers(input) {
		ts.reg.add(workerID, input.Group.Creator, "public")
	}
	config := fanout.Config{Clock: ts.srv.clock}
	// omit_authorizers models a controller whose authorization dependency was
	// never wired: the interface field stays nil (assigning a typed nil would
	// still read as a non-nil interface), so the route's wiring gate fires.
	if !input.OmitAuthorizers {
		config.Authorizer = &semanticAuthorizer{readable: semanticControllerReadableSet(input)}
	}
	ts.srv.fanout = fanout.NewController(ts.srv.deps.Hub, config)
	body, _ := json.Marshal(map[string]any{"name": "fixture-group", "worker_ids": input.Group.Members})
	recorder := ts.do("POST", "/api/fanout/groups", string(body), semanticHeaders(input.Actor))
	return emptySemanticObservation(s, recorder.Code, canonicalGoRouteError(recorder.Code, recorder.Body.String()))
}

func executeSemanticRoute(t *testing.T, s semanticScenario) semanticObservation {
	t.Helper()
	input := s.Input
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.FanoutAllowUnknownMembers = input.Group.AllowUnknownMembers
		if input.Policy.Action != "allow" {
			url := "https://policy.example.test/fanout"
			cfg.Governance.PolicyWebhookURL = &url
		}
		// Create scenarios pin the routes' member-admission rule, so the
		// server needs an authorizer that can refuse a *registered* member.
		// Send scenarios keep the default RBAC provider so their pre-created
		// fixture group is unaffected.
		if input.Operation == "create" {
			deps.Authz = serverauth.NewAuthorizationServiceWith(
				&semanticRouteAuthorizer{readable: semanticReadableSet(input)},
			)
		}
	})
	var tracking *semanticRouteTracking
	if input.Operation == "send" && input.Policy.Action != "allow" {
		tracking = trackSemanticRouteFanout(t, ts, input)
	}
	if input.Operation == "create" {
		return executeSemanticRouteCreate(t, ts, s)
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
	groupStored := ts.srv.fanout.GetGroup(input.Group.ID, input.Group.Creator) != nil
	body, _ := json.Marshal(map[string]any{"data": input.Command})
	recorder := ts.do("POST", "/api/fanout/groups/"+input.Group.ID+"/send", string(body), semanticHeaders(input.Actor))
	return observeSemanticRoute(s, recorder.Code, recorder.Body.String(), tracking, groupStored)
}

func TestSemanticConfiguredGovernanceRouteUsesLiveFanoutState(t *testing.T) {
	scenario := semanticScenario{
		ID: "configured-governance-live-state",
		Input: semanticInput{
			Operation: "send",
			Actor: semanticActor{
				Subject:       "admin",
				Authenticated: true,
				Roles:         []string{"admin"},
			},
			Group: semanticGroup{
				ID:      "g1",
				Creator: "admin",
				Members: []string{"w1"},
			},
			Command: "rm -rf /",
		},
		Backends: map[string]semanticClaim{
			"go": {Status: "unsupported_fail_closed"},
		},
	}
	scenario.Input.Policy.Action = "deny"
	scenario.Input.Workers.AcceptedMembers = []string{"w1"}

	observation := executeSemanticRoute(t, scenario)

	if !observation.routeMemberStored || !observation.routeGroupStored ||
		!observation.routeWorkerConnected || !observation.routeObserverAttached {
		t.Fatalf("route evidence = member stored:%t group stored:%t worker connected:%t observer attached:%t",
			observation.routeMemberStored, observation.routeGroupStored,
			observation.routeWorkerConnected, observation.routeObserverAttached)
	}
	if observation.StatusCode != 501 {
		t.Fatalf("status = %d, want 501", observation.StatusCode)
	}
	if len(observation.DeliveredWorkers) != 0 {
		t.Fatalf("delivered workers = %v, want none", observation.DeliveredWorkers)
	}
	if len(observation.ObserverNotifications) != 0 {
		t.Fatalf("observer notifications = %v, want none", observation.ObserverNotifications)
	}
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
