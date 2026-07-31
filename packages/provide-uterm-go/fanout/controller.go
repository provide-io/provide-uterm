//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package fanout ports the provide-uterm fan-out controller: a controller that
// multiplexes one operator's input to a GROUP of worker sessions and collects /
// compares their output (divergence detection). Port of
// provide.uterm.server.bridge.fanout.
//
// Deviation: the Python controller's fan-out policy gate (deny/hold/approval
// path) and the approval-store pruning subscription are NOT ported here — this
// port implements the standard-execution path (parallel + sequential broadcast,
// output collection, divergence flagging) that the REST + browser-WS + MCP
// surfaces exercise. Go's regexp is RE2 (linear time), so the Python ReDoS
// pattern-safety scanner is unnecessary for error_pattern validation; a length
// bound plus a compile check is retained.
package fanout

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// maxErrorPatternLen bounds a group's error_pattern. Port of
// rest_helpers.MAX_EXPECT_REGEX_LEN.
const maxErrorPatternLen = 200

var (
	// ErrPrincipalRequired means dispatch was attempted without a resolved,
	// authenticated principal.
	ErrPrincipalRequired = errors.New("fanout: authenticated principal is required")
	// ErrAuthorizerUnavailable means the controller was constructed without its
	// mandatory current-session authorization dependency.
	ErrAuthorizerUnavailable = errors.New("fanout: member authorizer is unavailable")
	// ErrAdminRequired means the resolved principal is not a global admin.
	ErrAdminRequired = errors.New("fanout: global admin role is required")
)

// Authorizer owns the two authorization decisions required immediately before
// dispatch. The controller iterates only the stored group's members, so callers
// cannot inject an arbitrary authorized subset.
type Authorizer interface {
	IsGlobalAdmin(*serverauth.Principal) bool
	CanReadMember(context.Context, *serverauth.Principal, string) bool
}

// Hub is the subset of the TermHub surface the controller needs. *hub.TermHub
// satisfies it; tests supply a fake to assert the broadcast + send behavior.
type Hub interface {
	// SendWorker delivers msg to workerID's worker socket. ok=false when the
	// worker is not connected / the send is refused.
	SendWorker(ctx context.Context, workerID string, msg map[string]any) (bool, error)
	// Broadcast delivers msg to all browser observers of workerID.
	Broadcast(ctx context.Context, workerID string, msg map[string]any) error
	// EventBus returns the hub's event bus (nil when unconfigured).
	EventBus() *hub.EventBus
}

// Controller orchestrates fan-out groups and broadcasts input to multiple
// sessions. Port of FanOutController.
type Controller struct {
	hub          Hub
	store        Store
	authorizer   Authorizer
	clock        hub.Clock
	maxGroupSize int
	newID        func() string
	openCapture  func(*hub.EventBus, string) (*Capture, error)
}

// Config configures a [Controller]. Zero values select production defaults.
type Config struct {
	// Store persists groups. nil → a fresh InMemoryStore.
	Store Store
	// Authorizer resolves current session membership and access. It is mandatory
	// for Send; nil controllers fail closed.
	Authorizer Authorizer
	// Clock supplies wall time for send_id timestamps. nil → real clock.
	Clock hub.Clock
	// MaxGroupSize bounds a group's worker count. 0 → 50.
	MaxGroupSize int
	// IDGen mints send_id / group_id values. nil → RFC-4122-less 32-hex ids
	// (matching Python's uuid4().hex shape).
	IDGen func() string
}

// NewController builds a controller wired to h.
func NewController(h Hub, cfg Config) *Controller {
	store := cfg.Store
	if store == nil {
		store = NewInMemoryStore()
	}
	clock := cfg.Clock
	if clock == nil {
		clock = hub.NewRealClock()
	}
	maxSize := cfg.MaxGroupSize
	if maxSize <= 0 {
		maxSize = 50
	}
	idGen := cfg.IDGen
	if idGen == nil {
		idGen = newHexID
	}
	return &Controller{
		hub: h, store: store, authorizer: cfg.Authorizer, clock: clock,
		maxGroupSize: maxSize, newID: idGen, openCapture: OpenCapture,
	}
}

// newHexID returns a 32-hex-char id, the shape of Python's uuid4().hex.
func newHexID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// -- Group CRUD --------------------------------------------------------------

// CreateGroup validates and persists a new group, returning its GroupID. It
// enforces the max group size and validates error_pattern (bounded length +
// compilable) exactly like the Python create_group. Port of create_group.
func (c *Controller) CreateGroup(group *Group, principal string) (string, error) {
	if len(group.WorkerIDs) > c.maxGroupSize {
		return "", fmt.Errorf("Group size %d exceeds max %d", len(group.WorkerIDs), c.maxGroupSize)
	}
	if _, err := validateErrorPattern(group.ErrorPattern); err != nil {
		return "", err
	}
	group.CreatedBy = principal
	c.store.Save(group)
	return group.GroupID, nil
}

// DeleteGroup deletes a group. Only the creator or a grantee (via
// authorizedGroup) can resolve it; the route layer enforces creator-only. Port
// of delete_group.
func (c *Controller) DeleteGroup(groupID, principal string) {
	if g := c.authorizedGroup(groupID, principal); g != nil {
		c.store.Delete(groupID)
	}
}

// GetGroup returns the group when principal is the creator or a grantee, else
// nil. Port of get_group.
func (c *Controller) GetGroup(groupID, principal string) *Group {
	return c.authorizedGroup(groupID, principal)
}

// ListGroups returns every group visible to principal. Port of list_groups.
func (c *Controller) ListGroups(principal string) []*Group {
	return c.store.ListForPrincipal(principal)
}

// GrantAccess adds grantee to the group's grants. Only the creator can grant.
// Port of grant_access.
func (c *Controller) GrantAccess(groupID, grantee, principal string) {
	c.store.GrantAccess(groupID, grantee, principal)
}

// authorizedGroup returns the group when principal is the creator or a grantee,
// else nil. Port of _authorized_group.
func (c *Controller) authorizedGroup(groupID, principal string) *Group {
	g, ok := c.store.Get(groupID)
	if !ok {
		return nil
	}
	if g.CreatedBy == principal || contains(g.Grants, principal) {
		return g
	}
	return nil
}

// -- Send --------------------------------------------------------------------

// Send reauthorizes the global-admin principal against every current stored
// member, dispatches only to allowed members, and reports all others failed.
// quiesceMS / maxResponseMS <= 0 fall back to the group defaults.
func (c *Controller) Send(
	ctx context.Context,
	groupID, data string,
	principal *serverauth.Principal,
	quiesceMS, maxResponseMS int,
) (Result, error) {
	empty := func() Result {
		return Result{
			GroupID: groupID, SendID: c.newID(), Command: data, SentAt: c.clock.Wall(),
			Results: []SessionResult{}, DivergentSessions: []string{}, FailedSessions: []string{},
		}
	}
	if principal == nil || principal.SubjectID == "" || principal.SubjectID == "anonymous" {
		return empty(), ErrPrincipalRequired
	}
	if c.authorizer == nil {
		return empty(), ErrAuthorizerUnavailable
	}
	if !c.authorizer.IsGlobalAdmin(principal) {
		return empty(), ErrAdminRequired
	}
	group := c.authorizedGroup(groupID, principal.SubjectID)
	if group == nil {
		return empty(), nil
	}
	allowed := make([]string, 0, len(group.WorkerIDs))
	refused := make([]string, 0)
	for _, workerID := range group.WorkerIDs {
		if c.authorizer.CanReadMember(ctx, principal, workerID) {
			allowed = append(allowed, workerID)
		} else {
			refused = append(refused, workerID)
		}
	}
	dispatchGroup := *group
	dispatchGroup.WorkerIDs = append([]string(nil), allowed...)
	qMS := quiesceMS
	if qMS <= 0 {
		qMS = group.QuiesceMS
	}
	mMS := maxResponseMS
	if mMS <= 0 {
		mMS = group.MaxResponseMS
	}
	var result Result
	if group.Mode == "sequential" {
		result = c.sendSequential(ctx, &dispatchGroup, data, qMS, mMS, principal.SubjectID)
	} else {
		result = c.sendParallel(ctx, &dispatchGroup, data, qMS, mMS, principal.SubjectID)
	}
	for _, wid := range refused {
		result.Results = append(result.Results, SessionResult{WorkerID: wid, OK: false})
		result.FailedSessions = append(result.FailedSessions, wid)
	}
	return result, nil
}

// notifyObservers tells each target session's observers that this input is
// fan-out-originated so they can distinguish it from a local hijack. Port of
// _notify_fanout_observers.
func (c *Controller) notifyObservers(ctx context.Context, group *Group, data, sendID, principal string) {
	for _, wid := range group.WorkerIDs {
		frame := map[string]any{
			"type":           "fanout_input",
			"group_id":       group.GroupID,
			"send_id":        sendID,
			"command":        data,
			"from_principal": principal,
		}
		_ = c.hub.Broadcast(ctx, wid, frame)
	}
}

// inputFrame builds the raw worker input frame.
func inputFrame(data string, ts float64) map[string]any {
	return map[string]any{"type": "input", "data": data, "ts": ts}
}

// sendParallel broadcasts to every worker concurrently then collects output
// concurrently. Port of _send_parallel.
func (c *Controller) sendParallel(ctx context.Context, group *Group, data string, quiesceMS, maxMS int, principal string) Result {
	sendID := c.newID()
	sentAt := c.clock.Wall()
	frame := inputFrame(data, sentAt)

	n := len(group.WorkerIDs)
	captures := make([]*Capture, n)
	captureOK := make([]bool, n)
	readyGroup := *group
	readyGroup.WorkerIDs = make([]string, 0, n)
	for i, wid := range group.WorkerIDs {
		capture, err := c.openCapture(c.hub.EventBus(), wid)
		if err != nil {
			continue
		}
		captures[i] = capture
		captureOK[i] = true
		readyGroup.WorkerIDs = append(readyGroup.WorkerIDs, wid)
	}
	c.notifyObservers(ctx, &readyGroup, data, sendID, principal)

	sendOK := make([]bool, n)
	deltas := make([]string, n)
	elapsed := make([]int, n)
	var workers sync.WaitGroup
	for i, wid := range group.WorkerIDs {
		if !captureOK[i] {
			continue
		}
		workers.Add(1)
		go func(i int, wid string) {
			defer workers.Done()
			defer captures[i].Close()
			ok, _ := c.hub.SendWorker(ctx, wid, frame)
			sendOK[i] = ok
			if ok {
				deltas[i], elapsed[i] = captures[i].Collect(ctx, quiesceMS, maxMS)
			}
		}(i, wid)
	}
	workers.Wait()

	results := make([]SessionResult, 0, n)
	failed := []string{}
	var successOutputs []string
	var successIdx []int
	for i, wid := range group.WorkerIDs {
		if sendOK[i] {
			d := deltas[i]
			results = append(results, SessionResult{WorkerID: wid, OK: true, OutputDelta: &d, ElapsedMS: elapsed[i]})
			successOutputs = append(successOutputs, d)
			successIdx = append(successIdx, len(results)-1)
		} else {
			results = append(results, SessionResult{WorkerID: wid, OK: false})
			failed = append(failed, wid)
		}
	}

	divergent := applyDivergence(results, successOutputs, successIdx, group.DivergenceThreshold)
	return Result{
		GroupID:           group.GroupID,
		SendID:            sendID,
		Command:           data,
		SentAt:            sentAt,
		Results:           results,
		DivergentSessions: divergent,
		FailedSessions:    failed,
	}
}

// sendSequential broadcasts to workers one at a time, optionally stopping on
// the first error-pattern match. Port of _send_sequential.
func (c *Controller) sendSequential(ctx context.Context, group *Group, data string, quiesceMS, maxMS int, principal string) Result {
	sendID := c.newID()
	sentAt := c.clock.Wall()
	frame := inputFrame(data, sentAt)
	// error_pattern is validated at create time; a compile failure here is
	// impossible for a persisted group, so a nil re disables the stop check.
	errRe, _ := validateErrorPattern(group.ErrorPattern)

	results := make([]SessionResult, 0, len(group.WorkerIDs))
	failed := []string{}
	var successOutputs []string
	var successIdx []int
	stopped := false

	for _, wid := range group.WorkerIDs {
		if stopped {
			results = append(results, SessionResult{WorkerID: wid, OK: false})
			failed = append(failed, wid)
			continue
		}
		capture, err := c.openCapture(c.hub.EventBus(), wid)
		if err != nil {
			results = append(results, SessionResult{WorkerID: wid, OK: false})
			failed = append(failed, wid)
			continue
		}
		memberGroup := *group
		memberGroup.WorkerIDs = []string{wid}
		c.notifyObservers(ctx, &memberGroup, data, sendID, principal)
		ok, _ := c.hub.SendWorker(ctx, wid, frame)
		if !ok {
			capture.Close()
			results = append(results, SessionResult{WorkerID: wid, OK: false})
			failed = append(failed, wid)
			continue
		}
		delta, elapsed := capture.Collect(ctx, quiesceMS, maxMS)
		capture.Close()
		d := delta
		results = append(results, SessionResult{WorkerID: wid, OK: true, OutputDelta: &d, ElapsedMS: elapsed})
		successOutputs = append(successOutputs, delta)
		successIdx = append(successIdx, len(results)-1)
		if group.StopOnFirstError && errRe != nil && errRe.MatchString(delta) {
			stopped = true
		}
	}

	divergent := applyDivergence(results, successOutputs, successIdx, group.DivergenceThreshold)
	return Result{
		GroupID:           group.GroupID,
		SendID:            sendID,
		Command:           data,
		SentAt:            sentAt,
		Results:           results,
		DivergentSessions: divergent,
		FailedSessions:    failed,
	}
}

// applyDivergence flags divergent successful results in place and returns the
// list of divergent worker ids. Shared by both send modes.
func applyDivergence(results []SessionResult, successOutputs []string, successIdx []int, threshold float64) []string {
	divergent := []string{}
	if len(successOutputs) == 0 {
		return divergent
	}
	flags := ComputeDivergence(successOutputs, threshold)
	for k, idx := range successIdx {
		if flags[k] {
			results[idx].Divergent = true
			divergent = append(divergent, results[idx].WorkerID)
		}
	}
	return divergent
}

// validateErrorPattern bounds and compiles a group's error_pattern. An empty
// pattern is valid and returns (nil, nil). Port of the compile_expect_regex
// bound applied by create_group (Go RE2 makes the ReDoS scanner unnecessary).
func validateErrorPattern(pattern string) (*regexp.Regexp, error) {
	if pattern == "" {
		return nil, nil
	}
	if len(pattern) > maxErrorPatternLen {
		return nil, fmt.Errorf("error_pattern too long: %d > %d", len(pattern), maxErrorPatternLen)
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil, fmt.Errorf("invalid error_pattern: %w", err)
	}
	return re, nil
}
