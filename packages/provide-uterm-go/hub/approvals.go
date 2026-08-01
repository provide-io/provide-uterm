//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "sync"

// ApprovalStatus is the lifecycle state of an approval request. Port of
// provide.uterm.server.bridge.hub.approvals.ApprovalStatus.
type ApprovalStatus string

// Approval statuses.
const (
	ApprovalPending  ApprovalStatus = "pending"
	ApprovalApproved ApprovalStatus = "approved"
	ApprovalRejected ApprovalStatus = "rejected"
	ApprovalTimeout  ApprovalStatus = "timeout"
	ApprovalRefused  ApprovalStatus = "refused"
)

// ApprovalRequest is a held command awaiting an approve/reject decision. Port
// of the Python ApprovalRequest dataclass.
type ApprovalRequest struct {
	ID          string
	WorkerID    string
	SubmitterID string
	Command     string
	Status      ApprovalStatus
	CreatedAt   float64
	ExpiresAt   float64
	GroupID     *string
	IsFanout    bool
	// OriginBrowser and OriginGeneration are internal capability-fence data.
	// They are intentionally absent from approval route serialization.
	OriginBrowser    BrowserConn
	OriginGeneration uint64
}

// approvalPruneTTL is how long a terminal-state request lingers past its
// expiry before being pruned. Mirrors the Python PRUNE_TTL (1 hour).
const approvalPruneTTL = 3600.0

// InMemoryApprovalStore is an in-memory store for approval requests. Port of
// provide.uterm.server.bridge.hub.approvals.InMemoryApprovalStore.
//
// A mutex makes the check-then-set Resolve/Claim and the CleanupExpired scan
// safe against concurrent mutation. OnExpired, if set, is invoked (outside the
// lock) for every PENDING request that just timed out.
type InMemoryApprovalStore struct {
	mu       sync.Mutex
	requests map[string]*ApprovalRequest
	clock    Clock

	// OnExpired is notified with the id of each request that transitions to
	// TIMEOUT during CleanupExpired. It runs outside the lock.
	OnExpired func(string)
}

// NewInMemoryApprovalStore builds an empty store. clock supplies the wall
// clock used by CleanupExpired; nil selects the real clock.
func NewInMemoryApprovalStore(clock Clock) *InMemoryApprovalStore {
	return &InMemoryApprovalStore{
		requests: map[string]*ApprovalRequest{},
		clock:    orDefaultClock(clock),
	}
}

// Add inserts a copied request, rejecting duplicate IDs so a stale resolver
// can never claim a different request through identifier reuse.
func (s *InMemoryApprovalStore) Add(req *ApprovalRequest) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.requests[req.ID]; exists {
		return false
	}
	s.requests[req.ID] = cloneApprovalRequest(req)
	return true
}

// Get returns the request for requestID, or nil if unknown.
func (s *InMemoryApprovalStore) Get(requestID string) *ApprovalRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return cloneApprovalRequest(s.requests[requestID])
}

func cloneApprovalRequest(req *ApprovalRequest) *ApprovalRequest {
	if req == nil {
		return nil
	}
	copy := *req
	if req.GroupID != nil {
		groupID := *req.GroupID
		copy.GroupID = &groupID
	}
	return &copy
}

// Resolve transitions a PENDING request to status. A non-pending request is
// left unchanged. Superseded by Claim for one-shot handling; retained for
// direct/test use.
func (s *InMemoryApprovalStore) Resolve(requestID string, status ApprovalStatus) {
	s.mu.Lock()
	defer s.mu.Unlock()
	req, ok := s.requests[requestID]
	if ok && req.Status == ApprovalPending {
		req.Status = status
	}
}

// Claim atomically transitions a PENDING request to status, returning true
// only for the caller that performs the transition (so a held command is
// injected exactly once under concurrent approve/reject).
func (s *InMemoryApprovalStore) Claim(requestID string, status ApprovalStatus) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	req, ok := s.requests[requestID]
	if !ok || req.Status != ApprovalPending {
		return false
	}
	req.Status = status
	return true
}

// SetStatus records the terminal outcome after the one-shot claim has won.
func (s *InMemoryApprovalStore) SetStatus(requestID string, status ApprovalStatus) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if req := s.requests[requestID]; req != nil {
		req.Status = status
	}
}

// CleanupExpired times out PENDING requests past their expiry and prunes
// terminal-state requests that expired more than approvalPruneTTL ago. The
// OnExpired callback is invoked (outside the lock) for each newly timed-out id.
func (s *InMemoryApprovalStore) CleanupExpired() {
	now := s.clock.Wall()

	var expiredIDs []string
	s.mu.Lock()
	for reqID, req := range s.requests {
		switch {
		case req.Status == ApprovalPending && req.ExpiresAt < now:
			req.Status = ApprovalTimeout
			expiredIDs = append(expiredIDs, req.ID)
		case req.Status != ApprovalPending && (req.ExpiresAt+approvalPruneTTL) < now:
			delete(s.requests, reqID)
		}
	}
	s.mu.Unlock()

	if s.OnExpired != nil {
		for _, reqID := range expiredIDs {
			s.OnExpired(reqID)
		}
	}
}
