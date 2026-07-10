//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"testing"
	"time"
)

func TestTermFrame(t *testing.T) {
	before := nowTS()
	f := Term("hi")
	after := nowTS()
	if f["type"] != "term" || f["data"] != "hi" {
		t.Fatalf("term frame = %v", f)
	}
	ts, ok := f["ts"].(float64)
	if !ok || ts < before || ts > after {
		t.Fatalf("term ts = %v (want in [%v,%v])", f["ts"], before, after)
	}
}

func TestTermAt(t *testing.T) {
	f := TermAt("hello", 1234.5)
	if f["ts"] != 1234.5 || f["data"] != "hello" {
		t.Fatalf("term at = %v", f)
	}
	// ts <= 0 means "now".
	f2 := TermAt("x", 0)
	if ts, ok := f2["ts"].(float64); !ok || ts <= 0 {
		t.Fatalf("term at now = %v", f2["ts"])
	}
}

func TestWorkerHello(t *testing.T) {
	f := WorkerHello("open")
	if f["type"] != "worker_hello" || f["input_mode"] != "open" {
		t.Fatalf("worker_hello = %v", f)
	}
	if _, ok := f["ts"].(float64); !ok {
		t.Fatalf("worker_hello ts missing: %v", f)
	}
	proto, ok := f["protocol"].(map[string]int)
	if !ok || proto["min"] != 1 || proto["max"] != 1 || proto["preferred"] != 1 {
		t.Fatalf("worker_hello protocol = %v", f["protocol"])
	}
	if WorkerHello("hijack")["input_mode"] != "hijack" {
		t.Fatal("custom mode not honored")
	}
}

func TestNowTS(t *testing.T) {
	a := nowTS()
	time.Sleep(time.Millisecond)
	if nowTS() <= a {
		t.Fatal("nowTS not monotonic-ish")
	}
}
