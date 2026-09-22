//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"fmt"
	"strconv"
	"strings"
)

// CloseInitiator says which side ended a connection. Port of the Python
// provide.uterm.transport_close.CloseInitiator.
type CloseInitiator string

// Close initiators. The string values are the wire/summary spelling shared
// with the Python, TypeScript and C# ports.
const (
	// CloseLocal means this side ended the connection.
	CloseLocal CloseInitiator = "local"
	// CloseRemote means the peer ended the connection.
	CloseRemote CloseInitiator = "remote"
	// CloseUnknown means the transport cannot tell which side ended it.
	CloseUnknown CloseInitiator = "unknown"
)

// TransportClose describes why a transport connection ended. Port of the
// Python TransportClose dataclass.
type TransportClose struct {
	// Initiator is the side that ended the connection.
	Initiator CloseInitiator
	// Code is the protocol close code, when the protocol has one (WebSocket).
	// Nil means no code, which is distinct from a code of zero.
	Code *int
	// Reason is the protocol close reason, when one was given.
	Reason string
	// Detail is a transport-specific description, such as the underlying error.
	Detail string
}

// Summary renders the close on one line: the initiator, then the code and
// reason, then any detail in parentheses. Byte-identical to Python's
// TransportClose.summary(), e.g. "local close 1011 keepalive ping timeout",
// "remote close", "unknown close (*net.OpError: reset)".
func (c TransportClose) Summary() string {
	parts := []string{string(c.Initiator) + " close"}
	if c.Code != nil {
		parts = append(parts, strconv.Itoa(*c.Code))
	}
	if c.Reason != "" {
		parts = append(parts, c.Reason)
	}
	text := strings.Join(parts, " ")
	if c.Detail != "" {
		return text + " (" + c.Detail + ")"
	}
	return text
}

// CloseFromError describes a connection that ended with err, attributing it to
// initiator. The detail is "<error type>: <message>", the Go spelling of
// Python's close_from_exception.
func CloseFromError(err error, initiator CloseInitiator) TransportClose {
	return TransportClose{Initiator: initiator, Detail: fmt.Sprintf("%T: %v", err, err)}
}

// TransportClosedError is returned by a transport when its connection has
// ended. It carries the observed Close. errors.Is(err, ErrConnectionClosed)
// matches it, so callers written against the sentinel keep working, and
// errors.As exposes the close. Port of Python's TransportClosedError.
type TransportClosedError struct {
	// Message is the transport's description of the failure, without the close.
	Message string
	// Close is how the connection ended.
	Close TransportClose
	// Err is the underlying cause, if any; Unwrap returns it.
	Err error
}

// Error returns "<message> (<close summary>)", mirroring the Python message.
func (e *TransportClosedError) Error() string {
	return e.Message + " (" + e.Close.Summary() + ")"
}

// Is reports that a TransportClosedError is an ErrConnectionClosed.
func (e *TransportClosedError) Is(target error) bool {
	return target == ErrConnectionClosed
}

// Unwrap returns the underlying cause, so errors.Is/As still reach it.
func (e *TransportClosedError) Unwrap() error {
	return e.Err
}

// closedError builds a *TransportClosedError. A short constructor keeps the
// transports' return sites on one line.
func closedError(message string, tc TransportClose, cause error) error {
	return &TransportClosedError{Message: message, Close: tc, Err: cause}
}
