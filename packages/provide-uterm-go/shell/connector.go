//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"fmt"
	"hash/fnv"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// defaultPollDelay is the idle poll back-off (matches _connector.py's 0.05s).
const defaultPollDelay = 50 * time.Millisecond

// ConnectorConfig configures a UshellConnector. It mirrors the Python
// constructor keyword arguments (display_name, _config, extra_ctx); _config is
// intentionally unused (reserved), as in the reference.
type ConnectorConfig struct {
	// DisplayName is a human-readable name (defaults to the session id).
	DisplayName string
	// ExtraCtx seeds the dispatcher context Values (Python extra_ctx). Ignored
	// when Context is set.
	ExtraCtx map[string]any
	// Context provides the typed CF bindings (Env / Storage / ListKVSessions).
	// When nil a context with Values=ExtraCtx is built.
	Context *Context
}

// UshellConnector is an interactive REPL connector that needs no external
// process. It provides the same method set as the Python UshellConnector
// (SessionConnector protocol): Start/Stop/IsConnected, PollMessages,
// HandleInput, HandleControl, GetSnapshot, GetAnalysis, Clear, SetMode.
//
// Unlike the single-threaded asyncio reference, this port is safe for
// concurrent use: all mutable state is guarded by mu, and animation streaming
// runs in a goroutine that appends frames under the same lock.
type UshellConnector struct {
	mu          sync.Mutex
	sessionID   string
	displayName string
	connected   bool
	welcomed    bool
	flowPaused  bool
	buf         *LineBuffer
	dispatcher  *CommandDispatcher
	ctxValues   map[string]any
	pending     []Frame
	animCancel  context.CancelFunc
	animDone    chan struct{}

	// pollSleep / pollDelay implement the idle back-off; both are overridable
	// in tests within the package.
	pollSleep func(time.Duration)
	pollDelay time.Duration
}

// NewUshellConnector builds a connector for the given session id.
func NewUshellConnector(sessionID string, cfg ConnectorConfig) *UshellConnector {
	dctx := cfg.Context
	if dctx == nil {
		dctx = &Context{Values: cfg.ExtraCtx}
	}
	display := cfg.DisplayName
	if display == "" {
		display = sessionID
	}
	return &UshellConnector{
		sessionID:   sessionID,
		displayName: display,
		buf:         NewLineBuffer(),
		dispatcher:  NewCommandDispatcher(dctx),
		ctxValues:   dctx.Values,
		pollSleep:   time.Sleep,
		pollDelay:   defaultPollDelay,
	}
}

// Start marks the connector connected.
func (c *UshellConnector) Start() {
	c.mu.Lock()
	c.connected = true
	c.mu.Unlock()
}

// Stop marks the connector disconnected and cancels any running animation.
func (c *UshellConnector) Stop() {
	c.mu.Lock()
	c.connected = false
	if c.animCancel != nil {
		c.animCancel()
	}
	c.mu.Unlock()
}

// IsConnected reports the connection state.
func (c *UshellConnector) IsConnected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connected
}

// PollMessages returns the welcome frames on the first call after connect, then
// any pending frames, else nil after an idle back-off. Port of poll_messages.
func (c *UshellConnector) PollMessages() []Frame {
	c.mu.Lock()
	if !c.connected || c.flowPaused {
		c.mu.Unlock()
		return nil
	}
	if !c.welcomed {
		c.welcomed = true
		c.mu.Unlock()
		return []Frame{WorkerHello("open"), Term(Banner + Prompt)}
	}
	if len(c.pending) > 0 {
		frames := c.pending
		c.pending = nil
		c.mu.Unlock()
		return frames
	}
	c.mu.Unlock()
	c.pollSleep(c.pollDelay)
	return nil
}

// HandleInput processes raw keystroke data and returns terminal frames. Port of
// handle_input.
func (c *UshellConnector) HandleInput(ctx context.Context, data string) []Frame {
	c.mu.Lock()
	c.buf.Feed(data)
	echo := c.buf.TakeEcho()
	completed := c.buf.TakeCompleted()
	c.mu.Unlock()

	var frames []Frame
	if echo != "" {
		frames = append(frames, Term(echo))
	}

	logger := ptel.GetLogger(ctx, "provide.uterm.shell")
	for _, line := range completed {
		logger.Debug("dispatch", "line", line)
		result := c.dispatcher.Dispatch(ctx, line)
		if result.Animated != nil {
			c.startAnimation(*result.Animated)
			continue
		}
		for _, s := range result.Text {
			frames = append(frames, Term(s))
		}
	}
	return frames
}

// startAnimation cancels any running animation and launches a new streaming
// goroutine. Port of the AnimatedResult branch of handle_input +
// _stream_animation.
func (c *UshellConnector) startAnimation(result AnimatedResult) {
	c.mu.Lock()
	if c.animCancel != nil {
		c.animCancel()
	}
	actx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	c.animCancel = cancel
	c.animDone = done
	c.mu.Unlock()

	go c.streamAnimation(actx, done, result)
}

// streamAnimation streams frames with per-frame delay, appending them to the
// pending queue, then appends the prompt (on completion or cancellation).
func (c *UshellConnector) streamAnimation(ctx context.Context, done chan struct{}, result AnimatedResult) {
	defer close(done)
	delay := 100 * time.Millisecond
	if result.FPS > 0 {
		delay = time.Duration(float64(time.Second) / result.FPS)
	}
	for {
		for _, frame := range result.Frames {
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				c.appendPending(Term(Prompt))
				return
			case <-timer.C:
			}
			c.appendPending(Term(frame))
		}
		if !result.Loop {
			break
		}
		select {
		case <-ctx.Done():
			c.appendPending(Term(Prompt))
			return
		default:
		}
	}
	c.appendPending(Term(Prompt))
}

// waitAnimDone returns the current animation's completion channel, or nil when
// no animation has run. The channel is closed when the streaming goroutine
// exits.
func (c *UshellConnector) waitAnimDone() <-chan struct{} {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.animDone
}

// appendPending queues a frame for the next PollMessages.
func (c *UshellConnector) appendPending(f Frame) {
	c.mu.Lock()
	c.pending = append(c.pending, f)
	c.mu.Unlock()
}

// HandleControl handles flow_pause / flow_resume backpressure and
// snapshot_request; hijack actions (pause/resume/step) are no-ops. Port of
// handle_control.
func (c *UshellConnector) HandleControl(action string) []Frame {
	switch action {
	case "flow_pause":
		c.mu.Lock()
		c.flowPaused = true
		c.mu.Unlock()
	case "flow_resume":
		c.mu.Lock()
		c.flowPaused = false
		c.mu.Unlock()
	case "snapshot_request":
		return []Frame{c.GetSnapshot()}
	}
	return nil
}

// GetSnapshot returns a fresh screen snapshot frame. Port of get_snapshot.
func (c *UshellConnector) GetSnapshot() Frame {
	c.mu.Lock()
	current := c.buf.CurrentLine()
	sid := c.sessionID
	c.mu.Unlock()

	screen := "ushell " + sid + "\r\n" + Prompt + current
	x := utf8.RuneCountInString(Prompt) + utf8.RuneCountInString(current)
	return Frame{
		"type":               "snapshot",
		"screen":             screen,
		"cursor":             map[string]int{"x": x, "y": 1},
		"cols":               80,
		"rows":               24,
		"screen_hash":        screenHash(screen),
		"cursor_at_end":      true,
		"has_trailing_space": false,
		"prompt_detected":    map[string]string{"prompt_id": "ushell_prompt"},
		"ts":                 nowTS(),
	}
}

// GetAnalysis returns a human-readable analysis string. Port of get_analysis.
//
// Deviation: the Python "sandbox_names" line becomes "context_names" (the
// Python-exec sandbox is not ported), and booleans / reprs follow Go
// conventions rather than Python's.
func (c *UshellConnector) GetAnalysis() string {
	c.mu.Lock()
	connected := c.connected
	current := c.buf.CurrentLine()
	names := make([]string, 0, len(c.ctxValues))
	for k := range c.ctxValues {
		if !strings.HasPrefix(k, "__") {
			names = append(names, k)
		}
	}
	sid := c.sessionID
	c.mu.Unlock()
	sort.Strings(names)

	lines := []string{
		fmt.Sprintf("[ushell analysis — session: %s]", sid),
		fmt.Sprintf("connected: %t", connected),
		fmt.Sprintf("current_line: %q", current),
		fmt.Sprintf("context_names: %v", names),
	}
	return strings.Join(lines, "\n")
}

// Clear discards the current line and returns a clear-screen frame. Port of
// clear.
func (c *UshellConnector) Clear() []Frame {
	c.mu.Lock()
	c.buf.Clear()
	c.mu.Unlock()
	return []Frame{Term(ClearScreen + Prompt)}
}

// SetMode accepts a mode change; ushell is always open, so it just re-advertises
// the mode. Port of set_mode.
func (c *UshellConnector) SetMode(mode string) []Frame {
	return []Frame{WorkerHello(mode)}
}

// screenHash derives a short, deterministic hash of screen. Deviation: the
// Python str(hash(screen))[:16] uses Python's process-local, non-portable
// hash; this uses FNV-1a. Both are session-local snapshot hints, not wire
// contracts.
func screenHash(screen string) string {
	h := fnv.New64a()
	_, _ = h.Write([]byte(screen))
	s := fmt.Sprintf("%d", h.Sum64())
	if len(s) > 16 {
		s = s[:16]
	}
	return s
}
