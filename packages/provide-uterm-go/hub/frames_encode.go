//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"encoding/json"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// encodeBrowserFrame encodes a browser-bound message for the inline stream.
// Port of core_helpers._encode_browser_frame: a "term" frame ships as escaped
// terminal data; everything else is a DLE/STX control frame.
func encodeBrowserFrame(msg map[string]any) (string, error) {
	if str(msg["type"]) == "term" {
		return controlchannel.EncodeTerminalData(str(msg["data"])), nil
	}
	return controlchannel.EncodeControlFrame(msg)
}

// encodeWorkerFrame encodes a worker-bound message for the inline stream. Port
// of core_helpers._encode_worker_frame: an "input" frame ships as escaped
// terminal data; everything else is a control frame.
func encodeWorkerFrame(msg map[string]any) (string, error) {
	if str(msg["type"]) == "input" {
		return controlchannel.EncodeTerminalData(str(msg["data"])), nil
	}
	return controlchannel.EncodeControlFrame(msg)
}

// str coerces a message field to its string form, mirroring Python's
// “str(msg.get(k) or "")“ (a nil/absent value yields "").
func str(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return toStr(v)
}

// frameToMap converts a typed frame struct to the map[string]any the
// controlchannel encoder consumes, routing through the frames package as the
// single source of truth for the wire shape. Numbers become float64 (JSON), so
// the subsequent compact re-encode is byte-stable.
func frameToMap(v any) (map[string]any, error) {
	b, err := frames.EncodeFrame(v)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return m, nil
}

// monoToWall converts a monotonic timestamp to wall-clock for external
// consumers. Port of core_helpers._mono_to_wall using the injected clock:
// wall + (mono_ts - mono_now).
func monoToWall(clock Clock, monoTS *float64) *float64 {
	if monoTS == nil {
		return nil
	}
	v := clock.Wall() + (*monoTS - clock.Monotonic())
	return &v
}
