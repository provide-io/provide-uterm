//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"bytes"
	"io"
	"os"
	"os/exec"
	"testing"
	"time"

	"github.com/creack/pty"
)

func TestSpawnedPtyEchoAndClose(t *testing.T) {
	sp, err := SpawnPTY([]string{"echo", "hi"})
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	// Read child output until "hi" arrives or the master EOFs.
	var acc bytes.Buffer
	buf := make([]byte, 256)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		_ = sp.master.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		n, rerr := sp.Read(buf)
		if n > 0 {
			acc.Write(buf[:n])
			if bytes.Contains(acc.Bytes(), []byte("hi")) {
				break
			}
		}
		if rerr != nil {
			break
		}
	}
	if !bytes.Contains(acc.Bytes(), []byte("hi")) {
		t.Fatalf("never saw echo output, got %q", acc.Bytes())
	}
	if sp.Closed() {
		t.Fatal("should not be closed yet")
	}
	if err := sp.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if !sp.Closed() {
		t.Fatal("should be closed")
	}
	// Idempotent + no-op resize after close.
	if err := sp.Close(); err != nil {
		t.Fatalf("double close: %v", err)
	}
	if err := sp.Resize(100, 40); err != nil {
		t.Fatalf("resize after close should be no-op, got %v", err)
	}
}

func TestSpawnedPtyResizeAndDefaultShell(t *testing.T) {
	trueBin, err := exec.LookPath("true")
	if err != nil {
		t.Skip("no `true` binary")
	}
	t.Setenv("SHELL", trueBin)
	sp, err := SpawnPTY(nil) // exercises the $SHELL default branch
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer func() { _ = sp.Close() }()
	if err := sp.Resize(120, 45); err != nil {
		t.Fatalf("resize: %v", err)
	}
	cols, rows := sp.TermSize()
	if cols != 120 || rows != 45 {
		t.Fatalf("term size after resize = %dx%d, want 120x45", cols, rows)
	}
	// drain until the child exits so Close reaps cleanly.
	go func() { _, _ = io.Copy(io.Discard, sp.master) }()
}

func TestSpawnedPtyWrite(t *testing.T) {
	// `cat` echoes stdin back on the master, exercising the Write path (the
	// ws→pty direction of the share bridge).
	sp, err := SpawnPTY([]string{"cat"})
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer func() { _ = sp.Close() }()
	if _, err := sp.Write([]byte("ping\n")); err != nil {
		t.Fatalf("write: %v", err)
	}
	var acc bytes.Buffer
	buf := make([]byte, 64)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && !bytes.Contains(acc.Bytes(), []byte("ping")) {
		_ = sp.master.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		n, _ := sp.Read(buf)
		acc.Write(buf[:n])
	}
	if !bytes.Contains(acc.Bytes(), []byte("ping")) {
		t.Fatalf("cat did not echo write, got %q", acc.Bytes())
	}
}

func TestSpawnPtyBadCommand(t *testing.T) {
	if _, err := SpawnPTY([]string{"/nonexistent/definitely-not-a-real-binary-xyz"}); err == nil {
		t.Fatal("expected spawn error for missing command")
	}
}

func TestTtyProxyDefaultsAndInactiveClose(t *testing.T) {
	p := NewTtyProxy()
	if p.Active() {
		t.Fatal("new proxy should be inactive")
	}
	if err := p.Close(); err != nil {
		t.Fatalf("inactive close should be nil, got %v", err)
	}
}

func TestTtyProxyStartRequiresTerminal(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	defer func() { _ = r.Close(); _ = w.Close() }()
	p := &TtyProxy{in: r, out: w}
	if _, _, err := p.Start(); err == nil {
		t.Fatal("Start on a non-terminal should error")
	}
	if cols, rows := p.TermSize(); cols != 80 || rows != 24 {
		t.Fatalf("non-terminal TermSize fallback = %dx%d, want 80x24", cols, rows)
	}
}

func TestTtyProxyReadWrite(t *testing.T) {
	inR, inW, _ := os.Pipe()
	outR, outW, _ := os.Pipe()
	defer func() { _ = inR.Close(); _ = inW.Close(); _ = outR.Close(); _ = outW.Close() }()
	p := &TtyProxy{in: inR, out: outW}

	go func() { _, _ = inW.Write([]byte("keys")); _ = inW.Close() }()
	buf := make([]byte, 16)
	n, err := p.Read(buf)
	if err != nil || string(buf[:n]) != "keys" {
		t.Fatalf("read = %q err=%v", buf[:n], err)
	}

	go func() { _, _ = p.Write([]byte("out")); _ = outW.Close() }()
	got, _ := io.ReadAll(outR)
	if string(got) != "out" {
		t.Fatalf("write echoed %q", got)
	}
}

func TestTtyProxyRawModeOnRealPty(t *testing.T) {
	ptmx, tty, err := pty.Open()
	if err != nil {
		t.Skipf("cannot open pty pair: %v", err)
	}
	defer func() { _ = ptmx.Close(); _ = tty.Close() }()
	_ = pty.Setsize(ptmx, &pty.Winsize{Rows: 30, Cols: 90})

	p := &TtyProxy{in: tty, out: tty}
	cols, rows, err := p.Start()
	if err != nil {
		t.Fatalf("start raw mode on real pty: %v", err)
	}
	if !p.Active() {
		t.Fatal("proxy should be active after Start")
	}
	if cols != 90 || rows != 30 {
		t.Fatalf("Start size = %dx%d, want 90x30", cols, rows)
	}
	if err := p.Close(); err != nil {
		t.Fatalf("restore: %v", err)
	}
	if p.Active() {
		t.Fatal("proxy should be inactive after Close")
	}
}
