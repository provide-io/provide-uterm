//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"strings"
	"testing"
)

func TestTransferProperties(t *testing.T) {
	if !NewTransferManager(30, "").AutoTransferEnabled() {
		t.Error("default enabled")
	}
	if NewTransferManager(0, "").AutoTransferEnabled() {
		t.Error("zero disabled")
	}
	if NewTransferManager(-1, "").AutoTransferEnabled() {
		t.Error("negative disabled")
	}
	if NewTransferManager(30, "").QueueMode() != "display" {
		t.Error("default mode")
	}
	if NewTransferManager(30, "replay").QueueMode() != "replay" {
		t.Error("replay mode")
	}
	// _warning_sent initializes false.
	if NewTransferManager(30, "").warningSent {
		t.Error("warning not false initially")
	}
	// autoIdleS default 30.
	if NewTransferManager(30, "").autoIdleS != 30 {
		t.Error("autoIdleS")
	}
}

func TestQueueKeystroke(t *testing.T) {
	tm := NewTransferManager(30, "")
	if tm.QueueKeystroke("u1", "ls") != "ls" {
		t.Error("basic")
	}
	tm2 := NewTransferManager(30, "")
	tm2.QueueKeystroke("u1", "l")
	if tm2.QueueKeystroke("u1", "s") != "ls" {
		t.Error("accumulate")
	}
	if NewTransferManager(30, "").QueueKeystroke("u1", "ls\r") != "ls↵" {
		t.Error("special key")
	}
}

func TestQueueOverflow(t *testing.T) {
	tm := NewTransferManager(30, "")
	tm.QueueKeystroke("u1", strings.Repeat("a", MaxQueueLength+50))
	if len([]rune(tm.GetQueueDisplay("u1"))) != MaxQueueLength {
		t.Error("overflow truncation")
	}
	// Exactly MaxQueueLength must NOT truncate (strict >).
	tm2 := NewTransferManager(30, "")
	tm2.QueueKeystroke("u1", strings.Repeat("a", MaxQueueLength))
	if len([]rune(tm2.FlushQueue("u1"))) != MaxQueueLength {
		t.Error("exact length truncated")
	}
}

func TestFlushClearDisplay(t *testing.T) {
	tm := NewTransferManager(30, "")
	tm.QueueKeystroke("u1", "hello")
	if tm.FlushQueue("u1") != "hello" || tm.FlushQueue("u1") != "" {
		t.Error("flush")
	}
	tm.QueueKeystroke("u1", "hello")
	tm.ClearQueue("u1")
	if tm.GetQueueDisplay("u1") != "" {
		t.Error("clear")
	}
	tm.ClearQueue("nonexistent") // must not panic
	if tm.FlushQueue("nonexistent") != "" || tm.GetQueueDisplay("nonexistent") != "" {
		t.Error("missing returns empty")
	}
	tm.QueueKeystroke("u2", "ls\r")
	if tm.GetQueueDisplay("u2") != "ls↵" {
		t.Error("display with data")
	}
}

func TestCheckAutoTransfer(t *testing.T) {
	// disabled
	if w, x := NewTransferManager(0, "").CheckAutoTransfer(999, []string{"u2"}); w || x {
		t.Error("disabled")
	}
	// no queued
	if w, x := NewTransferManager(30, "").CheckAutoTransfer(999, nil); w || x {
		t.Error("no queued")
	}
	// should transfer (== threshold and exceeds)
	if _, x := NewTransferManager(30, "").CheckAutoTransfer(30, []string{"u2"}); !x {
		t.Error("transfer at threshold")
	}
	if _, x := NewTransferManager(30, "").CheckAutoTransfer(45, []string{"u2"}); !x {
		t.Error("transfer exceeds")
	}
	// should warn
	if w, _ := NewTransferManager(30, "").CheckAutoTransfer(20, []string{"u2"}); !w {
		t.Error("warn at 20")
	}
	// below warn threshold
	if w, x := NewTransferManager(30, "").CheckAutoTransfer(15, []string{"u2"}); w || x {
		t.Error("below warn")
	}
	// small threshold clamps warn to 0
	if w, _ := NewTransferManager(5, "").CheckAutoTransfer(0.5, []string{"u2"}); !w {
		t.Error("small threshold warn")
	}
}

func TestCheckAutoTransferWarnLatch(t *testing.T) {
	tm := NewTransferManager(30, "")
	if w, _ := tm.CheckAutoTransfer(22, []string{"u2"}); !w {
		t.Error("first warn")
	}
	// second check same idle → no warn (latched, hits final return)
	if w, _ := tm.CheckAutoTransfer(25, []string{"u2"}); w {
		t.Error("should not re-warn")
	}
	// no queued resets latch
	tm.CheckAutoTransfer(22, nil)
	if w, _ := tm.CheckAutoTransfer(22, []string{"u2"}); !w {
		t.Error("warn again after reset")
	}
}

func TestCheckAutoTransferResetOnTransfer(t *testing.T) {
	tm := NewTransferManager(30, "")
	tm.CheckAutoTransfer(22, []string{"u2"}) // warn
	if _, x := tm.CheckAutoTransfer(30, []string{"u2"}); !x {
		t.Error("transfer")
	}
	if w, _ := tm.CheckAutoTransfer(22, []string{"u2"}); !w {
		t.Error("warn after transfer reset")
	}
}

func TestResetWarning(t *testing.T) {
	tm := NewTransferManager(30, "")
	tm.CheckAutoTransfer(22, []string{"u2"})
	tm.ResetWarning()
	if w, _ := tm.CheckAutoTransfer(22, []string{"u2"}); !w {
		t.Error("warn after explicit reset")
	}
}

func TestBuildTransferMessage(t *testing.T) {
	// display mode
	tmd := NewTransferManager(30, "display")
	tmd.QueueKeystroke("u2", "ls\r")
	msg := tmd.BuildTransferMessage("u1", "u2", "handover")
	if msg["type"] != "control_transfer" || msg["from_user_id"] != "u1" ||
		msg["to_user_id"] != "u2" || msg["reason"] != "handover" || msg["queued_keys"] != "ls↵" {
		t.Errorf("display transfer: %v", msg)
	}
	if tmd.GetQueueDisplay("u2") != "" {
		t.Error("display queue not cleared")
	}
	// replay mode
	tmr := NewTransferManager(30, "replay")
	tmr.QueueKeystroke("u2", "ls\r")
	if tmr.BuildTransferMessage("u1", "u2", "auto_idle")["queued_keys"] != "ls\r" {
		t.Error("replay raw keys")
	}
	if tmr.FlushQueue("u2") != "" {
		t.Error("replay queue not flushed")
	}
	// empty queues
	if NewTransferManager(30, "").BuildTransferMessage("u1", "u2", "admin_takeover")["queued_keys"] != "" {
		t.Error("empty display")
	}
	if NewTransferManager(30, "replay").BuildTransferMessage("u1", "u2", "lease_expired")["queued_keys"] != "" {
		t.Error("empty replay")
	}
}
