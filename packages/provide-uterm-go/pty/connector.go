//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"fmt"
	"io"
	"os"
	"sort"
	"sync"
	"syscall"
	"time"
)

// validModes is the set of accepted input modes. Port of connector.py _VALID_MODES.
var validModes = map[string]struct{}{"open": {}, "hijack": {}}

// validConfigKeys is the set of accepted connector_config keys. Port of
// connector.py _VALID_CONFIG_KEYS.
var validConfigKeys = map[string]struct{}{
	"command": {}, "args": {}, "username": {}, "password": {}, "run_as": {},
	"run_as_uid": {}, "run_as_gid": {}, "env": {}, "inject": {}, "cols": {},
	"rows": {}, "input_mode": {},
}

// Grace-window tuning for Stop: a child that outlives SIGHUP + PTY EOF is given
// stopGraceWindow to run its hangup path before being SIGKILLed. Mirrors
// connector.py's _STOP_GRACE_POLLS (20) * _STOP_GRACE_POLL_S (0.05s) ≈ 1s.
// A var (not const) so tests can shorten it to exercise the SIGKILL path fast.
var stopGraceWindow = time.Second

// bufferCap bounds the rendered buffer. Port of connector.py's 32768 cap.
const bufferCap = 32768

// PTYConnector is a local PTY session connector (connector_type="pty").
// Port of connector.PTYConnector.
//
// It optionally authenticates via PAM, resolves uid/gid via UidMap, spawns a
// child in a PTY as the resolved user, and supervises it. All config parameters
// are validated before any system call.
//
// Unlike the single-threaded asyncio reference, this port is safe for concurrent
// use: all mutable state is guarded by mu, and a background reader goroutine
// feeds the buffer (the push-vs-pull deviation is documented on readLoop).
type PTYConnector struct {
	sessionID   string
	displayName string
	command     string
	args        []string
	username    string
	password    string
	runAs       string
	runAsUID    *int
	runAsGID    *int
	extraEnv    map[string]string
	inject      bool
	cols        int
	rows        int
	inputMode   string

	uidMap *UidMap

	mu         sync.Mutex
	master     *os.File
	child      *spawnedChild
	connected  bool
	paused     bool
	flowPaused bool
	buffer     string
	dirty      bool
	decoder    *incrementalDecoder

	captureSocket *CaptureSocket
	captureTmpDir string
	pam           *PamSession

	waitDone chan struct{}
	readDone chan struct{}

	// geteuid is a test seam for the PAM root check.
	geteuid func() int
}

// NewPTYConnector validates config and builds a connector. Port of
// PTYConnector.__init__.
func NewPTYConnector(sessionID, displayName string, config map[string]any) (*PTYConnector, error) {
	if err := checkUnknownKeys("PTYConnector", config, validConfigKeys); err != nil {
		return nil, err
	}
	if _, ok := config["command"]; !ok {
		return nil, fmt.Errorf("PTYConnector requires 'command' in connector_config")
	}

	command, err := coerceString(config["command"])
	if err != nil {
		return nil, fmt.Errorf("command must be a string: %w", err)
	}
	if err := ValidateCommand(command); err != nil {
		return nil, err
	}

	username, _ := optString(config, "username")
	if username != "" {
		if err := ValidateUsername(username); err != nil {
			return nil, err
		}
	}

	extraEnv, err := coerceEnv(config["env"])
	if err != nil {
		return nil, err
	}
	if len(extraEnv) > 0 {
		if err := ValidateEnv(extraEnv); err != nil {
			return nil, err
		}
	}

	inputMode := "open"
	if v, ok := config["input_mode"]; ok && v != nil {
		inputMode = fmt.Sprintf("%v", v)
		if _, ok := validModes[inputMode]; !ok {
			return nil, fmt.Errorf("invalid input_mode %q: must be one of %v", inputMode, sortedModes())
		}
	}

	runAsUID, err := coerceIntPtr(config, "run_as_uid")
	if err != nil {
		return nil, err
	}
	runAsGID, err := coerceIntPtr(config, "run_as_gid")
	if err != nil {
		return nil, err
	}
	password, _ := optString(config, "password")
	runAs, _ := optString(config, "run_as")

	c := &PTYConnector{
		sessionID:   sessionID,
		displayName: displayName,
		command:     command,
		args:        coerceStringList(config["args"]),
		username:    username,
		password:    password,
		runAs:       runAs,
		runAsUID:    runAsUID,
		runAsGID:    runAsGID,
		extraEnv:    extraEnv,
		inject:      coerceBool(config["inject"]),
		cols:        coerceIntOr(config["cols"], 80),
		rows:        coerceIntOr(config["rows"], 24),
		inputMode:   inputMode,
		uidMap:      NewUidMap(nil, false),
		decoder:     &incrementalDecoder{},
		geteuid:     os.Geteuid,
	}
	return c, nil
}

// Start authenticates (optionally), resolves the user, sets up capture, and
// spawns the child in a PTY. Port of PTYConnector.start.
func (c *PTYConnector) Start(ctx context.Context) error {
	pamEnv := map[string]string{}
	if c.username != "" && c.password != "" {
		if c.geteuid() != 0 {
			return fmt.Errorf("user-switching via PAM requires the server to run as root")
		}
		pam, err := NewPamSession("provide-uterm")
		if err != nil {
			return err
		}
		c.pam = pam
		if err := c.pam.Authenticate(c.username, c.password); err != nil {
			return err
		}
		if err := c.pam.AcctMgmt(); err != nil {
			return err
		}
		if err := c.pam.OpenSession(); err != nil {
			return err
		}
		pamEnv = c.pam.Env()
	}

	var resolved *ResolvedUser
	if c.username != "" || c.runAs != "" || c.runAsUID != nil {
		r, err := c.uidMap.Resolve(c.username, ResolveOpts{
			RunAs: c.runAs, RunAsUID: c.runAsUID, RunAsGID: c.runAsGID,
		})
		if err != nil {
			return err
		}
		resolved = r
	}

	var capturePath string
	if c.inject {
		tmp, err := os.MkdirTemp("", "uterm-cap-")
		if err != nil {
			return err
		}
		c.captureTmpDir = tmp
		capturePath = tmp + "/cap.sock"
		cs, err := NewCaptureSocket(capturePath)
		if err != nil {
			return err
		}
		if err := cs.Start(); err != nil {
			return err
		}
		c.captureSocket = cs
	}

	env := c.buildEnv(pamEnv, resolved, capturePath)
	child, err := spawnPTY(c.command, c.args, env, resolved, c.cols, c.rows)
	if err != nil {
		return err
	}

	c.mu.Lock()
	c.master = child.master
	c.child = child
	c.connected = true
	c.waitDone = make(chan struct{})
	c.readDone = make(chan struct{})
	master := child.master
	waitDone := c.waitDone
	c.mu.Unlock()

	go func() {
		_ = child.cmd.Wait()
		close(waitDone)
	}()
	go c.readLoop(master)
	return nil
}

// buildEnv assembles the child environment. Port of the env-assembly block in
// PTYConnector.start (os.environ + pam_env + resolved defaults + extra_env +
// capture vars).
func (c *PTYConnector) buildEnv(pamEnv map[string]string, resolved *ResolvedUser, capturePath string) []string {
	env := map[string]string{}
	for _, kv := range os.Environ() {
		if k, v, ok := splitEnv(kv); ok {
			env[k] = v
		}
	}
	for k, v := range pamEnv {
		env[k] = v
	}
	if resolved != nil {
		setDefault(env, "HOME", resolved.Home)
		setDefault(env, "SHELL", resolved.Shell)
		setDefault(env, "USER", resolved.Name)
		setDefault(env, "LOGNAME", resolved.Name)
	}
	for k, v := range c.extraEnv {
		env[k] = v
	}
	if capturePath != "" {
		env["UTERM_CAPTURE_SOCKET"] = capturePath
		if lib := getCaptureLibPath(); lib != "" {
			// Platform-C injection library (stubbed: getCaptureLibPath returns "").
			if isDarwin() {
				env["DYLD_INSERT_LIBRARIES"] = lib
				env["DYLD_FORCE_FLAT_NAMESPACE"] = "1"
			} else {
				env["LD_PRELOAD"] = lib
			}
		}
	}
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	sort.Strings(out) // deterministic ordering (cosmetic; execve order-agnostic)
	return out
}

// readLoop continuously reads r and feeds decoded output into the buffer.
//
// DEVIATION FROM PYTHON: connector.py reads the master lazily inside
// poll_messages (pull). This port uses a background goroutine (push) reading
// blocking into the shared buffer, marking it dirty; PollMessages then returns
// a snapshot iff new data arrived since the last poll. The observable behaviour
// is equivalent (data surfaces on the next poll), and a blocking goroutine read
// avoids the O_NONBLOCK/SetReadDeadline PTY-pollability differences between
// Linux and macOS — and is unblocked deterministically when Stop closes master.
func (c *PTYConnector) readLoop(r io.Reader) {
	defer func() {
		c.mu.Lock()
		if c.readDone != nil {
			select {
			case <-c.readDone:
			default:
				close(c.readDone)
			}
		}
		c.mu.Unlock()
	}()
	buf := make([]byte, 4096)
	for {
		n, err := r.Read(buf)
		if n > 0 {
			c.feed(buf[:n])
		}
		if err != nil {
			// EOF/EIO on the PTY master: the child closed the slave (exited).
			c.mu.Lock()
			c.connected = false
			c.mu.Unlock()
			return
		}
	}
}

// feed incrementally decodes data into the buffer (capped) and marks it dirty.
func (c *PTYConnector) feed(data []byte) {
	c.mu.Lock()
	c.buffer += c.decoder.Decode(data)
	if len(c.buffer) > bufferCap {
		c.buffer = c.buffer[len(c.buffer)-bufferCap:]
	}
	c.dirty = true
	c.mu.Unlock()
}

// Stop terminates the child (SIGHUP → grace → SIGKILL), reaps it, and releases
// capture / PAM resources. Idempotent. Port of PTYConnector.stop.
func (c *PTYConnector) Stop(ctx context.Context) error {
	c.mu.Lock()
	child := c.child
	master := c.master
	waitDone := c.waitDone
	c.child = nil
	c.master = nil
	c.mu.Unlock()

	if child != nil {
		if child.cmd.Process != nil {
			_ = child.cmd.Process.Signal(syscall.SIGHUP)
		}
		if master != nil {
			_ = master.Close() // slave receives HUP; unblocks readLoop
		}
		c.reapChild(child, waitDone)
	} else if master != nil {
		_ = master.Close()
	}

	c.mu.Lock()
	cs := c.captureSocket
	tmp := c.captureTmpDir
	pam := c.pam
	c.captureSocket = nil
	c.captureTmpDir = ""
	c.pam = nil
	c.connected = false
	c.mu.Unlock()

	if cs != nil {
		_ = cs.Stop()
	}
	if tmp != "" {
		_ = os.RemoveAll(tmp)
	}
	if pam != nil {
		pam.CloseSession()
	}
	return nil
}

// reapChild waits up to the grace window for the child to exit, then escalates
// to SIGKILL and blocks until reaped — so no zombie is left behind. Port of the
// WNOHANG → grace-poll → SIGKILL escalation in PTYConnector.stop /
// _reap_within_grace.
func (c *PTYConnector) reapChild(child *spawnedChild, waitDone chan struct{}) {
	if waitDone == nil {
		return
	}
	select {
	case <-waitDone:
		return
	case <-time.After(stopGraceWindow):
	}
	if child.cmd.Process != nil {
		_ = child.cmd.Process.Kill() // SIGKILL
	}
	<-waitDone
}

// IsConnected reports whether the connector is connected and holds a master fd.
// Port of PTYConnector.is_connected.
func (c *PTYConnector) IsConnected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connected && c.master != nil
}
