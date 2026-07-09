//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// maxRxBufBytes bounds the unconsumed telnet receive buffer. An upstream that
// sends IAC SB without IAC SE would otherwise grow the buffer without bound
// (memory-exhaustion DoS). 256 KiB is far above any legitimate subnegotiation.
// Mirrors Python's _MAX_RX_BUF_BYTES.
const maxRxBufBytes = 256 * 1024

// TelnetTransport is a full RFC 854 telnet client implementing
// ConnectionTransport. It is a port of the Python TelnetTransport.
//
// Deviation from Python: the Python client answers negotiation events with
// fire-and-forget asyncio tasks. This Go port instead answers negotiation and
// subnegotiation events synchronously inside Receive, which is deterministic and
// avoids background goroutines.
type TelnetTransport struct {
	mu     sync.Mutex
	conn   net.Conn
	rxBuf  []byte
	cols   int
	rows   int
	term   string
	peerIP string
	// Negotiation state sets provide send-once semantics.
	negWill map[byte]bool
	negWont map[byte]bool
	negDo   map[byte]bool
	negDont map[byte]bool
}

// NewTelnetTransport returns a TelnetTransport with the Python constructor
// defaults (cols=80, rows=25, term="ANSI").
func NewTelnetTransport() *TelnetTransport {
	t := &TelnetTransport{cols: DefaultCols, rows: DefaultRows, term: DefaultTerm}
	t.resetNegotiation()
	return t
}

func (t *TelnetTransport) resetNegotiation() {
	t.negWill = map[byte]bool{}
	t.negWont = map[byte]bool{}
	t.negDo = map[byte]bool{}
	t.negDont = map[byte]bool{}
}

// Connect opens a telnet connection to host:port and offers WILL BINARY /
// WILL SGA.
func (t *TelnetTransport) Connect(ctx context.Context, host string, port int, opts ConnectOptions) error {
	logger := ptel.GetLogger(ctx, "provide.uterm.transports.telnet")
	opts = opts.withDefaults()

	t.mu.Lock()
	if t.conn != nil {
		t.mu.Unlock()
		_ = t.Disconnect(ctx)
		t.mu.Lock()
	}
	t.mu.Unlock()

	addr := net.JoinHostPort(host, strconv.Itoa(port))
	dialer := net.Dialer{Timeout: opts.Timeout}
	conn, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("failed to connect to %s: %w", addr, err)
	}

	t.mu.Lock()
	t.conn = conn
	t.cols = opts.Cols
	t.rows = opts.Rows
	t.term = opts.Term
	t.rxBuf = t.rxBuf[:0]
	t.resetNegotiation()
	if tcpAddr, ok := conn.RemoteAddr().(*net.TCPAddr); ok {
		t.peerIP = tcpAddr.IP.String()
	}
	// Offer BINARY + SGA (Python sends WILL BINARY, WILL SGA on connect).
	t.sendWillLocked(optBIN)
	t.sendWillLocked(optSGA)
	t.mu.Unlock()

	logger.Debug("telnet_transport connected", "host", host, "port", port)
	return nil
}

// Disconnect closes the connection. Idempotent.
func (t *TelnetTransport) Disconnect(ctx context.Context) error {
	t.mu.Lock()
	conn := t.conn
	if conn == nil {
		t.mu.Unlock()
		return nil
	}
	t.conn = nil
	t.rxBuf = t.rxBuf[:0]
	t.resetNegotiation()
	t.mu.Unlock()

	_ = conn.Close()
	ptel.GetLogger(ctx, "provide.uterm.transports.telnet").Debug("telnet_transport disconnected")
	return nil
}

// Send transmits data after remapping DEL(0x7f)→BS(0x08) and IAC-escaping 0xFF.
func (t *TelnetTransport) Send(ctx context.Context, data []byte) error {
	t.mu.Lock()
	conn := t.conn
	if conn == nil {
		t.mu.Unlock()
		return fmt.Errorf("%w: telnet send", ErrNotConnected)
	}
	escaped := escapeOutgoing(data)
	_, err := conn.Write(escaped)
	t.mu.Unlock()
	if err != nil {
		_ = t.Disconnect(ctx)
		return fmt.Errorf("send failed: %w", err)
	}
	return nil
}

// escapeOutgoing remaps DEL→BS then doubles every 0xFF (IAC escaping). xterm.js
// sends DEL for Backspace but many BBS/telnet servers expect BS.
func escapeOutgoing(data []byte) []byte {
	out := make([]byte, 0, len(data)+8)
	for _, b := range data {
		switch b {
		case 0x7f:
			out = append(out, 0x08)
		case iacByte:
			out = append(out, iacByte, iacByte)
		default:
			out = append(out, b)
		}
	}
	return out
}

// Receive reads bytes, strips IAC sequences, and answers negotiation events
// synchronously. Returns an empty slice on timeout.
func (t *TelnetTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	t.mu.Lock()
	conn := t.conn
	t.mu.Unlock()
	if conn == nil {
		return nil, fmt.Errorf("%w: telnet receive", ErrNotConnected)
	}

	if err := conn.SetReadDeadline(time.Now().Add(timeout)); err != nil {
		return nil, fmt.Errorf("set read deadline: %w", err)
	}
	tmp := make([]byte, maxBytes)
	n, err := conn.Read(tmp)

	if err != nil {
		if errors.Is(err, os.ErrDeadlineExceeded) {
			return []byte{}, nil
		}
		// EOF or reset: flush the remaining buffer as final.
		return t.handleRemoteClose(ctx)
	}
	if n == 0 {
		return t.handleRemoteClose(ctx)
	}

	t.mu.Lock()
	t.rxBuf = append(t.rxBuf, tmp[:n]...)
	payload, events, consumed := parseTelnetBuffer(t.rxBuf, false)
	if consumed > 0 {
		t.rxBuf = t.rxBuf[consumed:]
	}
	if len(t.rxBuf) > maxRxBufBytes {
		t.rxBuf = t.rxBuf[:0]
		t.mu.Unlock()
		_ = t.Disconnect(ctx)
		return nil, fmt.Errorf("telnet receive buffer exceeded %d bytes (likely IAC SB without IAC SE)", maxRxBufBytes)
	}
	t.respondToEventsLocked(ctx, events)
	t.mu.Unlock()
	return payload, nil
}

// handleRemoteClose flushes the residual buffer (final=true) and disconnects.
func (t *TelnetTransport) handleRemoteClose(ctx context.Context) ([]byte, error) {
	t.mu.Lock()
	payload, _, consumed := parseTelnetBuffer(t.rxBuf, true)
	if consumed > 0 {
		t.rxBuf = t.rxBuf[consumed:]
	}
	t.mu.Unlock()
	_ = t.Disconnect(ctx)
	if len(payload) > 0 {
		return payload, nil
	}
	return nil, ErrConnectionClosed
}

// IsConnected reports whether a connection is active.
func (t *TelnetTransport) IsConnected() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.conn != nil
}

// PeerIP returns the connected peer's IP address, or "" if unavailable. Mirrors
// the Python peer_ip() used for post-connect egress validation.
func (t *TelnetTransport) PeerIP() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.conn == nil {
		return ""
	}
	return t.peerIP
}

// SetSize updates the terminal size and sends a NAWS subnegotiation.
func (t *TelnetTransport) SetSize(ctx context.Context, cols, rows int) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.conn == nil {
		return fmt.Errorf("%w: telnet set_size", ErrNotConnected)
	}
	t.cols = cols
	t.rows = rows
	t.sendNAWSLocked(cols, rows)
	return nil
}

// respondToEventsLocked answers negotiation/subnegotiation events. Caller holds
// t.mu. This is the synchronous-negotiation deviation from Python.
func (t *TelnetTransport) respondToEventsLocked(ctx context.Context, events []telnetEvent) {
	logger := ptel.GetLogger(ctx, "provide.uterm.transports.telnet")
	for _, ev := range events {
		if ev.kind == evSubneg {
			t.handleSubnegotiationLocked(ev.payload)
			continue
		}
		logger.Debug("telnet negotiation", "cmd", ev.cmd, "opt", ev.opt)
		t.negotiateLocked(ev.cmd, ev.opt)
	}
}

func (t *TelnetTransport) negotiateLocked(cmd, opt byte) {
	if t.conn == nil {
		return
	}
	t.trackNegotiationLocked(cmd, opt)
	switch cmd {
	case cmdDO:
		t.negotiateDoResponseLocked(opt)
	case cmdDONT:
		t.sendWontLocked(opt)
	case cmdWILL:
		t.negotiateWillResponseLocked(opt)
	case cmdWONT:
		t.sendDontLocked(opt)
	}
}

func (t *TelnetTransport) trackNegotiationLocked(cmd, opt byte) {
	switch cmd {
	case cmdDO:
		t.negDo[opt] = true
	case cmdDONT:
		t.negDont[opt] = true
	case cmdWILL:
		t.negWill[opt] = true
	case cmdWONT:
		t.negWont[opt] = true
	}
}

func (t *TelnetTransport) negotiateDoResponseLocked(opt byte) {
	switch opt {
	case optBIN, optSGA:
		t.sendWillLocked(opt)
	case optNAWS:
		t.sendWillLocked(opt)
		t.sendNAWSLocked(t.cols, t.rows)
	case optTTYPE:
		t.sendWillLocked(opt)
		t.sendTTYPELocked(t.term)
	default:
		t.sendWontLocked(opt)
	}
}

func (t *TelnetTransport) negotiateWillResponseLocked(opt byte) {
	switch opt {
	case optECHO, optSGA, optBIN:
		t.sendDoLocked(opt)
	default:
		t.sendDontLocked(opt)
	}
}

func (t *TelnetTransport) handleSubnegotiationLocked(sub []byte) {
	if len(sub) == 0 || t.conn == nil {
		return
	}
	if sub[0] == optTTYPE && len(sub) > 1 && sub[1] == 1 {
		t.sendTTYPELocked(t.term)
	}
}

// --- write helpers (caller holds t.mu) ---

func (t *TelnetTransport) writeLocked(b []byte) {
	if t.conn == nil {
		return
	}
	_, _ = t.conn.Write(b)
}

func (t *TelnetTransport) sendCmdLocked(cmd, opt byte) {
	t.writeLocked([]byte{iacByte, cmd, opt})
}

func (t *TelnetTransport) sendWillLocked(opt byte) {
	if !t.negWill[opt] {
		t.sendCmdLocked(cmdWILL, opt)
		t.negWill[opt] = true
	}
}

func (t *TelnetTransport) sendWontLocked(opt byte) {
	if !t.negWont[opt] {
		t.sendCmdLocked(cmdWONT, opt)
		t.negWont[opt] = true
	}
}

func (t *TelnetTransport) sendDoLocked(opt byte) {
	if !t.negDo[opt] {
		t.sendCmdLocked(cmdDO, opt)
		t.negDo[opt] = true
	}
}

func (t *TelnetTransport) sendDontLocked(opt byte) {
	if !t.negDont[opt] {
		t.sendCmdLocked(cmdDONT, opt)
		t.negDont[opt] = true
	}
}

// escapeIAC doubles every 0xFF byte inside a subnegotiation payload (RFC 855).
func escapeIAC(payload []byte) []byte {
	out := make([]byte, 0, len(payload))
	for _, b := range payload {
		if b == iacByte {
			out = append(out, iacByte, iacByte)
		} else {
			out = append(out, b)
		}
	}
	return out
}

func (t *TelnetTransport) sendNAWSLocked(cols, rows int) {
	if t.conn == nil {
		return
	}
	size := escapeIAC([]byte{
		byte((cols >> 8) & 0xFF), byte(cols & 0xFF),
		byte((rows >> 8) & 0xFF), byte(rows & 0xFF),
	})
	frame := append([]byte{iacByte, cmdSB, optNAWS}, size...)
	frame = append(frame, iacByte, cmdSE)
	t.writeLocked(frame)
}

func (t *TelnetTransport) sendTTYPELocked(term string) {
	payload := append([]byte{optTTYPE, ttypeIS}, []byte(term)...)
	t.sendSubnegotiationLocked(payload)
}

func (t *TelnetTransport) sendSubnegotiationLocked(payload []byte) {
	if t.conn == nil {
		return
	}
	frame := append([]byte{iacByte, cmdSB}, escapeIAC(payload)...)
	frame = append(frame, iacByte, cmdSE)
	t.writeLocked(frame)
}

// Compile-time assertion that TelnetTransport implements ConnectionTransport.
var _ ConnectionTransport = (*TelnetTransport)(nil)
