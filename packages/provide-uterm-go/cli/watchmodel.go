//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"encoding/json"
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

// dleWatch/stxWatch are the control-channel magic bytes the watch parser scans
// for. The HTTP-inspection frames are ordinary control frames tagged
// _channel=="http" (mirrors _watch_app._DLE/_STX).
const (
	dleWatch = 0x10
	stxWatch = 0x02
)

// exchange is one HTTP request/response pair. Port of _watch_app.Exchange.
type exchange struct {
	reqID            string
	method           string
	url              string
	reqHeaders       map[string]string
	reqBodyB64       string
	reqBodySize      int
	reqBodyTruncated bool
	reqBodyBinary    bool
	status           *int
	statusText       string
	durationMs       *float64
	resHeaders       map[string]string
	resBodyB64       string
	resBodySize      int
	resBodyTruncated bool
	resBodyBinary    bool
}

// httpFrameMsg carries one decoded HTTP-channel frame to the model.
type httpFrameMsg map[string]any

// connStateMsg reports a change in the upstream WebSocket connection state.
type connStateMsg struct{ connected bool }

// watchModel is the bubbletea model for `uterm watch`.
type watchModel struct {
	tunnelID     string
	exchanges    []*exchange
	cursor       int
	layoutMode   string // horizontal | vertical | modal
	methodFilter string
	connected    bool
	showDetail   bool
	width        int
	height       int
	quitting     bool
}

var watchMethods = []string{"", "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

// newWatchModel builds the initial model.
func newWatchModel(tunnelID, layout string) watchModel {
	if layout != "horizontal" && layout != "vertical" && layout != "modal" {
		layout = "horizontal"
	}
	return watchModel{tunnelID: tunnelID, layoutMode: layout, width: 80, height: 24}
}

// Init implements tea.Model. The WS reader command is injected by runWatch; the
// pure model has nothing to start.
func (m watchModel) Init() tea.Cmd { return nil }

// Update implements tea.Model.
func (m watchModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch t := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = t.Width, t.Height
	case tea.KeyMsg:
		return m.handleKey(t)
	case connStateMsg:
		m.connected = t.connected
	case httpFrameMsg:
		m.handleFrame(t)
	}
	return m, nil
}

// handleKey processes a keypress. Bindings mirror the Textual app: q quit,
// l layout, f method filter, plus arrow/enter/esc navigation.
func (m watchModel) handleKey(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.String() {
	case "q", "ctrl+c":
		m.quitting = true
		return m, tea.Quit
	case "l":
		m.cycleLayout()
	case "f":
		m.cycleMethod()
	case "up", "k":
		if m.cursor > 0 {
			m.cursor--
		}
	case "down", "j":
		if n := len(m.filtered()); n > 0 && m.cursor < n-1 {
			m.cursor++
		}
	case "enter":
		if m.layoutMode == "modal" {
			m.showDetail = true
		}
	case "esc":
		m.showDetail = false
	}
	return m, nil
}

func (m *watchModel) cycleLayout() {
	modes := []string{"horizontal", "vertical", "modal"}
	for i, mode := range modes {
		if mode == m.layoutMode {
			m.layoutMode = modes[(i+1)%len(modes)]
			return
		}
	}
	m.layoutMode = "horizontal"
}

func (m *watchModel) cycleMethod() {
	idx := 0
	for i, meth := range watchMethods {
		if meth == m.methodFilter {
			idx = i
			break
		}
	}
	m.methodFilter = watchMethods[(idx+1)%len(watchMethods)]
	if m.cursor >= len(m.filtered()) {
		m.cursor = 0
	}
}

// filtered returns the exchanges passing the current method filter.
func (m watchModel) filtered() []*exchange {
	if m.methodFilter == "" {
		return m.exchanges
	}
	out := make([]*exchange, 0, len(m.exchanges))
	for _, ex := range m.exchanges {
		if ex.method == m.methodFilter {
			out = append(out, ex)
		}
	}
	return out
}

// handleFrame applies an http_req/http_res frame to the exchange list. Mirrors
// WatchApp._handle_frame.
func (m *watchModel) handleFrame(frame map[string]any) {
	switch frameStr(frame, "type") {
	case "http_req":
		m.exchanges = append(m.exchanges, &exchange{
			reqID:            frameStr(frame, "id"),
			method:           frameStr(frame, "method"),
			url:              frameStr(frame, "url"),
			reqHeaders:       frameHeaders(frame["headers"]),
			reqBodyB64:       frameStr(frame, "body_b64"),
			reqBodySize:      frameInt(frame, "body_size"),
			reqBodyTruncated: frameBool(frame, "body_truncated"),
			reqBodyBinary:    frameBool(frame, "body_binary"),
		})
	case "http_res":
		rid := frameStr(frame, "id")
		for i := len(m.exchanges) - 1; i >= 0; i-- {
			ex := m.exchanges[i]
			if ex.reqID != rid {
				continue
			}
			st := frameInt(frame, "status")
			dur := frameFloat(frame, "duration_ms")
			ex.status = &st
			ex.statusText = frameStr(frame, "status_text")
			ex.durationMs = &dur
			ex.resHeaders = frameHeaders(frame["headers"])
			ex.resBodyB64 = frameStr(frame, "body_b64")
			ex.resBodySize = frameInt(frame, "body_size")
			ex.resBodyTruncated = frameBool(frame, "body_truncated")
			ex.resBodyBinary = frameBool(frame, "body_binary")
			return
		}
	}
}

// parseHTTPFrames extracts HTTP-channel frames from a control-channel-encoded WS
// message. Port of _watch_app.parse_http_frames: a manual DLE/STX scan is used
// (not the streaming decoder) because it only needs whole self-contained frames
// tagged _channel=="http".
func parseHTTPFrames(raw string) []map[string]any {
	var frames []map[string]any
	pos := 0
	for pos < len(raw) {
		idx := strings.IndexByte(raw[pos:], dleWatch)
		if idx == -1 {
			break
		}
		idx += pos
		if idx+1 < len(raw) && raw[idx+1] == stxWatch {
			if f, next, ok := parseOneHTTPFrame(raw, idx); ok {
				if f != nil {
					frames = append(frames, f)
				}
				pos = next
				continue
			}
		}
		pos = idx + 1
	}
	return frames
}

// parseOneHTTPFrame decodes a single DLE/STX-framed payload at idx, returning
// the frame (nil when it is not an http-channel object), the next scan
// position, and ok=false when the header is malformed.
func parseOneHTTPFrame(raw string, idx int) (map[string]any, int, bool) {
	header := raw[idx+2 : min(idx+10, len(raw))]
	if len(header) != 8 || idx+10 >= len(raw) || raw[idx+10] != ':' {
		return nil, 0, false
	}
	length64, err := parseHex8(header)
	if err != nil {
		return nil, 0, false
	}
	length := int(length64)
	start := idx + 11
	end := min(start+length, len(raw))
	payload := raw[start:end]
	next := start + length
	var obj map[string]any
	if json.Unmarshal([]byte(payload), &obj) == nil && frameStr(obj, "_channel") == "http" {
		return obj, next, true
	}
	return nil, next, true
}

func parseHex8(s string) (uint64, error) {
	var v uint64
	for i := 0; i < len(s); i++ {
		c := s[i]
		var d uint64
		switch {
		case c >= '0' && c <= '9':
			d = uint64(c - '0')
		case c >= 'a' && c <= 'f':
			d = uint64(c-'a') + 10
		case c >= 'A' && c <= 'F':
			d = uint64(c-'A') + 10
		default:
			return 0, fmt.Errorf("invalid hex")
		}
		v = v<<4 | d
	}
	return v, nil
}

func frameStr(m map[string]any, key string) string {
	switch v := m[key].(type) {
	case string:
		return v
	case float64:
		return fmt.Sprintf("%v", v)
	case json.Number:
		return v.String()
	default:
		return ""
	}
}

func frameInt(m map[string]any, key string) int {
	switch v := m[key].(type) {
	case float64:
		return int(v)
	case json.Number:
		i, _ := v.Int64()
		return int(i)
	default:
		return 0
	}
}

func frameFloat(m map[string]any, key string) float64 {
	switch v := m[key].(type) {
	case float64:
		return v
	case json.Number:
		f, _ := v.Float64()
		return f
	default:
		return 0
	}
}

func frameBool(m map[string]any, key string) bool {
	b, _ := m[key].(bool)
	return b
}

func frameHeaders(v any) map[string]string {
	raw, ok := v.(map[string]any)
	if !ok {
		return map[string]string{}
	}
	out := make(map[string]string, len(raw))
	for k, val := range raw {
		if s, ok := val.(string); ok {
			out[k] = s
		}
	}
	return out
}
