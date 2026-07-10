//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// egressGolden mirrors testdata/egress_golden.json, produced by the real Python
// guard (provide.uterm.server.egress) via gen_egress_golden.py. This test is
// the differential parity gate: the Go classifier must reproduce every Python
// verdict, so drift in either implementation fails here.
type egressGolden struct {
	IPs []struct {
		IP                   string `json:"ip"`
		BlockedDefault       bool   `json:"blocked_default"`
		BlockedDefaultReason string `json:"blocked_default_reason"`
		BlockedPrivate       bool   `json:"blocked_private"`
		BlockedPrivateReason string `json:"blocked_private_reason"`
	} `json:"ips"`
	Webhooks []struct {
		URL     string `json:"url"`
		Blocked bool   `json:"blocked"`
	} `json:"webhooks"`
}

func loadEgressGolden(t *testing.T) egressGolden {
	t.Helper()
	raw, err := os.ReadFile("testdata/egress_golden.json")
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var g egressGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	if len(g.IPs) == 0 || len(g.Webhooks) == 0 {
		t.Fatal("golden is empty")
	}
	return g
}

// blockVerdict runs AssertIPAllowed and returns (blocked, reason) in the
// golden's vocabulary ("metadata" / "private" / "").
func blockVerdict(ip string, blockPrivate bool) (bool, string) {
	err := AssertIPAllowed(ip, blockPrivate)
	if err == nil {
		return false, ""
	}
	if strings.Contains(err.Error(), "metadata") {
		return true, "metadata"
	}
	return true, "private"
}

func TestEgressIPClassificationParity(t *testing.T) {
	g := loadEgressGolden(t)
	for _, row := range g.IPs {
		blocked, reason := blockVerdict(row.IP, false)
		if blocked != row.BlockedDefault || reason != row.BlockedDefaultReason {
			t.Errorf("%s block_private=false: got (%v,%q) want (%v,%q)",
				row.IP, blocked, reason, row.BlockedDefault, row.BlockedDefaultReason)
		}
		blocked, reason = blockVerdict(row.IP, true)
		if blocked != row.BlockedPrivate || reason != row.BlockedPrivateReason {
			t.Errorf("%s block_private=true: got (%v,%q) want (%v,%q)",
				row.IP, blocked, reason, row.BlockedPrivate, row.BlockedPrivateReason)
		}
	}
}

func TestEgressWebhookParity(t *testing.T) {
	g := loadEgressGolden(t)
	// Every golden webhook target is a literal-IP host or malformed URL, so the
	// resolver must never fire; a call means the Go path diverged from Python.
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		t.Fatal("resolver called for a literal-IP webhook target")
		return nil, nil
	}, nil)
	for _, row := range g.Webhooks {
		err := guard.AssertWebhookTargetAllowed(context.Background(), row.URL)
		if (err != nil) != row.Blocked {
			t.Errorf("webhook %q: blocked=%v want %v (err=%v)", row.URL, err != nil, row.Blocked, err)
		}
	}
}
