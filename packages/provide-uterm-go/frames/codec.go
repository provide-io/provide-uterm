//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
)

// decodeForbid decodes an extra="forbid" model: unknown fields are rejected,
// mirroring the Pydantic _FrameBase default.
func decodeForbid[T any](data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	v := new(T)
	if err := dec.Decode(v); err != nil {
		return nil, fmt.Errorf("frames: decode %T: %w", v, err)
	}
	return v, nil
}

// decodeLoose decodes an extra="ignore" or extra="allow" model: unknown
// fields are dropped (ignore) or captured by the model's own UnmarshalJSON
// into Extra (allow).
func decodeLoose[T any](data []byte) (any, error) {
	v := new(T)
	if err := json.Unmarshal(data, v); err != nil {
		return nil, fmt.Errorf("frames: decode %T: %w", v, err)
	}
	return v, nil
}

// decoders maps the "type" discriminator to the decoder for the matching
// frame struct — the Go equivalent of the AnyFrame discriminated union.
var decoders = map[string]func([]byte) (any, error){
	TypeTerm:               decodeForbid[TermFrame],
	TypeInput:              decodeForbid[InputFrame],
	TypeSnapshotReq:        decodeForbid[SnapshotReqFrame],
	TypeSnapshot:           decodeForbid[SnapshotFrame],
	TypeControl:            decodeForbid[ControlFrame],
	TypeHijackState:        decodeForbid[HijackStateFrame],
	TypeHijackRequest:      decodeForbid[HijackRequestFrame],
	TypeHijackRelease:      decodeForbid[HijackReleaseFrame],
	TypeHijackStep:         decodeForbid[HijackStepFrame],
	TypeWorkerConnected:    decodeForbid[WorkerConnectedFrame],
	TypeWorkerDisconnected: decodeForbid[WorkerDisconnectedFrame],
	TypeWorkerHello:        decodeForbid[WorkerHelloFrame],
	TypeHeartbeat:          decodeForbid[HeartbeatFrame],
	TypeHeartbeatAck:       decodeForbid[HeartbeatAckFrame],
	TypePing:               decodeForbid[PingFrame],
	TypePong:               decodeForbid[PongFrame],
	TypeHello:              decodeLoose[HelloFrame], // extra="ignore"
	TypeResume:             decodeForbid[ResumeFrame],
	TypeIdentity:           decodeLoose[IdentityFrame], // extra="allow"
	TypeSessionToken:       decodeForbid[SessionTokenFrame],
	TypeResumeOk:           decodeForbid[ResumeOkFrame],
	TypeResumeFailed:       decodeForbid[ResumeFailedFrame],
	TypeLinkPatterns:       decodeForbid[LinkPatternsFrame],
	TypeAnalysis:           decodeForbid[AnalysisFrame],
	TypeError:              decodeForbid[ErrorFrame],
	TypeStatus:             decodeLoose[StatusFrame], // extra="allow"
	TypeInputModeChanged:   decodeForbid[InputModeChangedFrame],
	TypeApprovalPending:    decodeForbid[ApprovalPendingFrame],
	TypeApprovalResolved:   decodeForbid[ApprovalResolvedFrame],
	TypePresenceUpdate:     decodeLoose[PresenceUpdateFrame], // extra="allow"
	TypePresenceSync:       decodeLoose[PresenceSyncFrame],   // extra="allow"
	TypePresenceLeave:      decodeForbid[PresenceLeaveFrame],
	TypeControlTransfer:    decodeForbid[ControlTransferFrame],
}

// DecodeFrame parses a wire JSON frame, dispatching on the "type"
// discriminator exactly like the Python AnyFrame discriminated union. The
// result is a pointer to the matching frame struct (e.g. *TermFrame).
// An unknown or missing "type" is an error. The per-model extra-field policy
// (forbid/ignore/allow) is honored; see the package documentation.
//
// Deviation from Pydantic: required fields and enum values are not
// validated — a frame missing a required field decodes with that field left
// at its Go zero value.
func DecodeFrame(data []byte) (any, error) {
	var head struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(data, &head); err != nil {
		return nil, fmt.Errorf("frames: invalid frame JSON: %w", err)
	}
	decode, ok := decoders[head.Type]
	if !ok {
		return nil, fmt.Errorf("frames: unknown frame type %q", head.Type)
	}
	return decode(data)
}

// EncodeFrame serializes a frame struct (value or pointer) to wire JSON with
// model_dump(exclude_none=True) semantics. It validates that the struct's
// Type field carries its wire literal (as a Pydantic Literal["..."] field
// would enforce) before marshaling.
func EncodeFrame(v any) ([]byte, error) {
	f, ok := v.(Frame)
	if !ok {
		return nil, fmt.Errorf("frames: %T is not a frame struct", v)
	}
	got, err := currentType(v)
	if err != nil {
		return nil, err
	}
	if got != f.FrameType() {
		return nil, fmt.Errorf("frames: %T has type %q, want literal %q", v, got, f.FrameType())
	}
	return json.Marshal(v)
}

// currentType reads the Type field of a frame struct or struct pointer.
func currentType(v any) (string, error) {
	rv := reflect.ValueOf(v)
	if rv.Kind() == reflect.Pointer {
		if rv.IsNil() {
			return "", fmt.Errorf("frames: nil %T", v)
		}
		rv = rv.Elem()
	}
	return rv.FieldByName("Type").String(), nil
}
