//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// encodeFrameControl encodes a typed frame struct into an inline DLE/STX control
// frame string, routing through the frames package (single source of wire
// truth) then the controlchannel encoder. Mirrors the hub's frameToMap +
// EncodeControlFrame path.
func encodeFrameControl(v any) (string, error) {
	b, err := frames.EncodeFrame(v)
	if err != nil {
		return "", err
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		return "", err
	}
	return controlchannel.EncodeControlFrame(m)
}

// encodeControlMap control-frames a raw payload map.
func encodeControlMap(m map[string]any) (string, error) {
	return controlchannel.EncodeControlFrame(m)
}
