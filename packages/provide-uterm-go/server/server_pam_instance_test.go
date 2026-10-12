//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// A PAM event names its login by the pid of the process that ran PAM, and pids
// are reused. Every id that carries a pid also carries that process's start
// time, so a later login drawing the same pid gets a different id. Port of the
// instance cases in test_pam_integration_part2.py.

// pinStartTicks makes pamProcessStartTicks answer from ticks; a pid left out
// reads as unreadable.
func pinStartTicks(t *testing.T, ticks map[int]int64) {
	t.Helper()
	saved := pamProcessStartTicks
	t.Cleanup(func() { pamProcessStartTicks = saved })
	pamProcessStartTicks = func(pid int) (int64, bool) {
		v, ok := ticks[pid]
		return v, ok
	}
}

func captureEvent(event string, pid int) pty.PamEvent {
	ev := pty.PamEvent{Event: event, Username: "suokki", TTY: "ssh", PID: pid, Mode: "capture"}
	if event == "open" {
		ev.CaptureSocket = "/run/uterm-cap-" + strconv.Itoa(pid) + ".sock"
	}
	return ev
}

func TestPamRecycledPidGetsADifferentCaptureID(t *testing.T) {
	ticks := map[int]int64{62738: 1000}
	pinStartTicks(t, ticks)
	first := pamSessionID(captureEvent("open", 62738))
	ticks[62738] = 98000 // the pid came round again, to a later process
	second := pamSessionID(captureEvent("open", 62738))
	if first != "pam-suokki-capture-62738-1000" || second != "pam-suokki-capture-62738-98000" {
		t.Fatalf("ids %q, %q", first, second)
	}
}

func TestPamUnreadableStartTimeFallsBackToARandomTag(t *testing.T) {
	pinStartTicks(t, map[int]int64{})
	first := pamSessionID(captureEvent("open", 7))
	second := pamSessionID(captureEvent("open", 7))
	if !regexp.MustCompile(`^pam-suokki-capture-7-r[0-9a-f]{8}$`).MatchString(first) || first == second {
		t.Fatalf("ids %q, %q: want two distinct random-tagged ids", first, second)
	}
}

func TestProcStartTicksReadsProcStat(t *testing.T) {
	raw, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		t.Skip("needs /proc")
	}
	text := string(raw)
	want, _ := strconv.ParseInt(strings.Fields(text[strings.LastIndex(text, ")")+1:])[19], 10, 64)
	if got, ok := procStartTicks(os.Getpid()); !ok || got != want {
		t.Fatalf("procStartTicks(self) = %d, %t; want %d", got, ok, want)
	}
	for _, pid := range []int{0, -1, 1<<22 + 12345} {
		if _, ok := procStartTicks(pid); ok {
			t.Fatalf("procStartTicks(%d) reported a start time", pid)
		}
	}
}

// The close must not depend on reading /proc: sshd may already have exited.
func TestPamCloseFindsTheIDOpenGaveAfterTheProcessIsGone(t *testing.T) {
	ticks := map[int]int64{4242: 31337}
	pinStartTicks(t, ticks)
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, reg)
	pi.onOpen(context.Background(), captureEvent("open", 4242))
	if len(reg.created) != 1 || reg.created[0]["session_id"] != "pam-suokki-capture-4242-31337" {
		t.Fatalf("created %v", reg.created)
	}
	delete(ticks, 4242) // the sshd process has exited by the time close is handled
	pi.onClose(context.Background(), captureEvent("close", 4242))
	if len(reg.deleted) != 1 || reg.deleted[0] != "pam-suokki-capture-4242-31337" {
		t.Fatalf("close deleted %v, want the id open created", reg.deleted)
	}
	if len(pi.instances) != 0 {
		t.Fatalf("instances left behind: %v", pi.instances)
	}
}

// A login that drew a pid whose earlier login never saw its close is still a
// new session: the stale id is not reused.
func TestPamOpenNeverReusesAStaleID(t *testing.T) {
	ticks := map[int]int64{9: 100}
	pinStartTicks(t, ticks)
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, reg)
	pi.onOpen(context.Background(), captureEvent("open", 9))
	ticks[9] = 200 // no close came; the pid is now a later login's
	pi.onOpen(context.Background(), captureEvent("open", 9))
	if len(reg.created) != 2 || reg.created[0]["session_id"] == reg.created[1]["session_id"] {
		t.Fatalf("created %v: want two distinct ids", reg.created)
	}
}

// The open's id is handed down, not looked up again: a close that lands while
// the open is still in flight (say, in a slow relay call) must not make the
// rest of the open mint another id.
func TestPamOpenPassesItsIDDownInsteadOfLookingItUpAgain(t *testing.T) {
	pinStartTicks(t, map[int]int64{}) // random tags: a second computation differs
	reg := newFakeRegistry()
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, reg)
	ev := captureEvent("open", 5)
	id := pi.beginLogin(ev)
	pi.endLogin(captureEvent("close", 5)) // the close races ahead
	pi.createCaptureSession(context.Background(), ev, id)
	if len(reg.created) != 1 || reg.created[0]["session_id"] != id {
		t.Fatalf("created %v, want the open's id %q", reg.created, id)
	}
}

// A login whose close never came is forgotten once its pid reports another
// start time; one that cannot be proven gone stays until the cap.
func TestPamOpenLoginMapIsBounded(t *testing.T) {
	ticks := map[int]int64{10: 1, 11: 2, 12: 3}
	pinStartTicks(t, ticks)
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, newFakeRegistry())
	pi.beginLogin(captureEvent("open", 10))
	pi.beginLogin(captureEvent("open", 11))
	ticks[10] = 5 // pid 10's sshd exited without a close; the pid is someone else's now
	pi.beginLogin(captureEvent("open", 12))
	if _, ok := pi.instances[pamLoginKey{"suokki", 10}]; ok || len(pi.instances) != 2 {
		t.Fatalf("instances = %v, want pid 10 forgotten", pi.instances)
	}

	saved := maxRememberedLogins
	t.Cleanup(func() { maxRememberedLogins = saved })
	maxRememberedLogins = 3
	pinStartTicks(t, map[int]int64{}) // random tags: nothing provably gone
	pi = newPamIntegration(serverconfig.PamConfig{Mode: "capture"}, newFakeRegistry())
	for pid := 20; pid < 25; pid++ {
		pi.beginLogin(captureEvent("open", pid))
	}
	if len(pi.instances) != 3 {
		t.Fatalf("instances = %v, want the 3 newest", pi.instances)
	}
	for pid := 22; pid < 25; pid++ {
		if _, ok := pi.instances[pamLoginKey{"suokki", pid}]; !ok {
			t.Fatalf("instances = %v, want pids 22-24 kept", pi.instances)
		}
	}
}
