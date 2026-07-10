//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package memory is the volatile in-memory control-plane backend. Port of
// provide.uterm.control.plane.memory.
//
// It holds all state in Go maps guarded by a mutex. Transactions are
// snapshot-isolated: Begin captures a snapshot and an independent working copy,
// and Commit runs optimistic-concurrency conflict detection so that a
// lease-acquire race yields exactly one winner — matching the SQLite backend's
// BEGIN IMMEDIATE serialization. It uses only the Go standard library.
package memory

import (
	"sort"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// sortApprovals orders approvals by (created_at, approval_id) ascending, the
// same total order the SQLite backend's “ORDER BY created_at ASC,
// approval_id ASC“ produces.
func sortApprovals(records []cp.ApprovalRecord) {
	sort.Slice(records, func(i, j int) bool {
		if records[i].CreatedAt != records[j].CreatedAt {
			return records[i].CreatedAt < records[j].CreatedAt
		}
		return records[i].ApprovalID < records[j].ApprovalID
	})
}
