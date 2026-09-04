// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package vnc

import (
	"encoding/binary"
	"fmt"
	"image"
	"io"
	"net"
	"strings"
	"sync"
	"time"
)

// RFB security types. Only None is supported, matching every other port.
const (
	SecurityNone = 1

	// Encodings this client asks for, in preference order.
	EncodingRaw      = 0
	EncodingCopyRect = 1

	maxRects       = 4096
	maxDesktopName = 4096
	maxDimension   = 8192
)

// RFBClient is a live RFB (VNC) connection presented as a gui.GraphicalSession.
//
// Ported from the C# canonical (Vnc/RfbClient.cs) and the Python reference
// (server/rfb_session.py). This port already carried the two halves either side
// of the protocol — FramebufferTracker for the pixels, EncodePointerEvent and
// EncodeKeyEvent for the input — so what is added here is the handshake and the
// update loop that join them.
//
// Until this existed, gui/attach answered 501 for rfb, described in
// bridge_rest.go as "a documented gap mirroring C#'s 501 for litevirt". That
// mirror held while the two ports had one console protocol each. Python and
// TypeScript both wire rfb now, which left this port the only served backend
// that could register an rfb target and never open it.
type RFBClient struct {
	mu      sync.Mutex
	conn    net.Conn
	tracker *FramebufferTracker
	width   int
	height  int
	closed  bool
	done    chan struct{}
}

// DialRFB connects to host:port, completes the handshake, and starts tracking
// framebuffer updates in the background.
//
// The dialer is injectable so tests drive a scripted pipe rather than a socket;
// a nil dialer uses net.DialTimeout.
func DialRFB(host string, port int, timeout time.Duration, dial func(string, string, time.Duration) (net.Conn, error)) (*RFBClient, error) {
	if dial == nil {
		dial = net.DialTimeout
	}
	conn, err := dial("tcp", net.JoinHostPort(host, fmt.Sprint(port)), timeout)
	if err != nil {
		return nil, fmt.Errorf("rfb dial: %w", err)
	}
	client, err := newRFBClient(conn)
	if err != nil {
		_ = conn.Close()
		return nil, err
	}
	return client, nil
}

// newRFBClient runs the handshake on an established connection.
func newRFBClient(conn net.Conn) (*RFBClient, error) {
	clientVersion, err := negotiateVersion(conn)
	if err != nil {
		return nil, err
	}
	if err := negotiateSecurity(conn, clientVersion); err != nil {
		return nil, err
	}
	// ClientInit, shared = 1.
	if _, err := conn.Write([]byte{1}); err != nil {
		return nil, fmt.Errorf("rfb client init: %w", err)
	}
	width, height, err := readServerInit(conn)
	if err != nil {
		return nil, err
	}
	if _, err := conn.Write(encodeSetPixelFormat()); err != nil {
		return nil, fmt.Errorf("rfb set pixel format: %w", err)
	}
	if _, err := conn.Write(encodeSetEncodings()); err != nil {
		return nil, fmt.Errorf("rfb set encodings: %w", err)
	}
	if _, err := conn.Write(encodeUpdateRequest(width, height, false)); err != nil {
		return nil, fmt.Errorf("rfb update request: %w", err)
	}

	client := &RFBClient{
		conn:    conn,
		tracker: NewFramebufferTracker(width, height),
		width:   width,
		height:  height,
		done:    make(chan struct{}),
	}
	go client.readLoop()
	return client, nil
}

// negotiateVersion agrees a ProtocolVersion, preferring 3.8 when offered.
func negotiateVersion(conn net.Conn) (string, error) {
	buf := make([]byte, 12)
	if _, err := io.ReadFull(conn, buf); err != nil {
		return "", fmt.Errorf("rfb protocol version: %w", err)
	}
	serverVersion := string(buf)
	if !strings.HasPrefix(serverVersion, "RFB ") {
		return "", fmt.Errorf("not an RFB server: %q", serverVersion)
	}
	clientVersion := serverVersion
	if strings.Contains(serverVersion, "003.008") {
		clientVersion = "RFB 003.008\n"
	}
	if _, err := conn.Write([]byte(clientVersion)); err != nil {
		return "", fmt.Errorf("rfb protocol version write: %w", err)
	}
	return clientVersion, nil
}

// negotiateSecurity completes the handshake, which must land on type None.
func negotiateSecurity(conn net.Conn, clientVersion string) error {
	if strings.Contains(clientVersion, "003.007") || strings.Contains(clientVersion, "003.008") {
		count := make([]byte, 1)
		if _, err := io.ReadFull(conn, count); err != nil {
			return fmt.Errorf("rfb security count: %w", err)
		}
		if count[0] == 0 {
			return fmt.Errorf("RFB security handshake failed (server offered no types)")
		}
		offered := make([]byte, count[0])
		if _, err := io.ReadFull(conn, offered); err != nil {
			return fmt.Errorf("rfb security types: %w", err)
		}
		if !containsByte(offered, SecurityNone) {
			return fmt.Errorf("RFB server does not offer security type None (offered %s)", joinBytes(offered))
		}
		if _, err := conn.Write([]byte{SecurityNone}); err != nil {
			return fmt.Errorf("rfb security select: %w", err)
		}
		// SecurityResult is 3.8 only; reading it on 3.7 desynchronises.
		if strings.Contains(clientVersion, "003.008") {
			result := make([]byte, 4)
			if _, err := io.ReadFull(conn, result); err != nil {
				return fmt.Errorf("rfb security result: %w", err)
			}
			if binary.BigEndian.Uint32(result) != 0 {
				return fmt.Errorf("RFB security rejected")
			}
		}
		return nil
	}

	// 3.3: the server dictates a single type as a u32.
	raw := make([]byte, 4)
	if _, err := io.ReadFull(conn, raw); err != nil {
		return fmt.Errorf("rfb security type: %w", err)
	}
	if securityType := binary.BigEndian.Uint32(raw); securityType != SecurityNone {
		return fmt.Errorf("unsupported RFB security type %d", securityType)
	}
	return nil
}

// readServerInit reads ServerInit and returns validated dimensions.
func readServerInit(conn net.Conn) (int, int, error) {
	header := make([]byte, 24)
	if _, err := io.ReadFull(conn, header); err != nil {
		return 0, 0, fmt.Errorf("rfb server init: %w", err)
	}
	width := int(binary.BigEndian.Uint16(header[0:2]))
	height := int(binary.BigEndian.Uint16(header[2:4]))
	if width == 0 || height == 0 || width > maxDimension || height > maxDimension {
		// The same cap MemoryGraphicalSession enforces: a hostile ServerInit
		// must not make us allocate on the strength of a remote number.
		return 0, 0, fmt.Errorf("RFB framebuffer dimensions out of range: %dx%d", width, height)
	}
	nameLen := binary.BigEndian.Uint32(header[20:24])
	if nameLen > maxDesktopName {
		return 0, 0, fmt.Errorf("RFB desktop name too long")
	}
	if nameLen > 0 {
		if _, err := io.ReadFull(conn, make([]byte, nameLen)); err != nil {
			return 0, 0, fmt.Errorf("rfb desktop name: %w", err)
		}
	}
	return width, height, nil
}

func encodeSetPixelFormat() []byte {
	msg := make([]byte, 20)
	msg[0] = 0
	msg[4] = 32 // bits-per-pixel
	msg[5] = 24 // depth
	msg[6] = 0  // big-endian-flag
	msg[7] = 1  // true-colour-flag
	binary.BigEndian.PutUint16(msg[8:10], 255)
	binary.BigEndian.PutUint16(msg[10:12], 255)
	binary.BigEndian.PutUint16(msg[12:14], 255)
	msg[14] = 16 // red-shift
	msg[15] = 8  // green-shift
	msg[16] = 0  // blue-shift
	return msg
}

func encodeSetEncodings() []byte {
	msg := make([]byte, 12)
	msg[0] = 2
	binary.BigEndian.PutUint16(msg[2:4], 2)
	binary.BigEndian.PutUint32(msg[4:8], uint32(EncodingRaw))
	binary.BigEndian.PutUint32(msg[8:12], uint32(EncodingCopyRect))
	return msg
}

func encodeUpdateRequest(width, height int, incremental bool) []byte {
	msg := make([]byte, 10)
	msg[0] = 3
	if incremental {
		msg[1] = 1
	}
	binary.BigEndian.PutUint16(msg[6:8], uint16(width))
	binary.BigEndian.PutUint16(msg[8:10], uint16(height))
	return msg
}

// readLoop applies framebuffer updates until the peer closes or we do.
func (c *RFBClient) readLoop() {
	defer close(c.done)
	for {
		if c.isClosed() {
			return
		}
		header := make([]byte, 1)
		if _, err := io.ReadFull(c.conn, header); err != nil {
			return
		}
		if header[0] != 0 {
			// Bell and ServerCutText carry no length we can skip blindly, so
			// stopping beats guessing and desynchronising the stream.
			return
		}
		rest := make([]byte, 3)
		if _, err := io.ReadFull(c.conn, rest); err != nil {
			return
		}
		rectCount := int(binary.BigEndian.Uint16(rest[1:3]))
		if rectCount > maxRects {
			return
		}
		for i := 0; i < rectCount; i++ {
			if err := c.applyRect(); err != nil {
				return
			}
		}
		if c.isClosed() {
			return
		}
		if _, err := c.conn.Write(encodeUpdateRequest(c.width, c.height, true)); err != nil {
			return
		}
	}
}

// applyRect reads one rectangle header and its payload.
func (c *RFBClient) applyRect() error {
	header := make([]byte, 12)
	if _, err := io.ReadFull(c.conn, header); err != nil {
		return err
	}
	x := int(binary.BigEndian.Uint16(header[0:2]))
	y := int(binary.BigEndian.Uint16(header[2:4]))
	w := int(binary.BigEndian.Uint16(header[4:6]))
	h := int(binary.BigEndian.Uint16(header[6:8]))
	encoding := int32(binary.BigEndian.Uint32(header[8:12]))
	if w == 0 || h == 0 {
		return nil
	}
	if x+w > c.width || y+h > c.height {
		return fmt.Errorf("RFB rect out of bounds: %d,%d %dx%d", x, y, w, h)
	}
	switch int(encoding) {
	case EncodingRaw:
		pixels := make([]byte, w*h*4)
		if _, err := io.ReadFull(c.conn, pixels); err != nil {
			return err
		}
		// FramebufferTracker takes RGBA — it was written for the litevirt path,
		// which delivers that. The pixel format this client negotiates puts the
		// bytes on the wire as BGRA, so swap here rather than change a contract
		// the other caller depends on. Python and TypeScript swap in the same
		// place, for the same reason.
		swapBGRAToRGBA(pixels)
		c.mu.Lock()
		defer c.mu.Unlock()
		return c.tracker.ApplyRawUpdate(x, y, w, h, pixels)
	case EncodingCopyRect:
		// Consume the source coordinates; a missed copyrect costs staleness in
		// a region, not a desynchronised stream.
		_, err := io.ReadFull(c.conn, make([]byte, 4))
		return err
	default:
		return fmt.Errorf("RFB encoding not negotiated: %d", encoding)
	}
}

// swapBGRAToRGBA rewrites a raw rectangle in place, and sets alpha opaque: RFB
// carries no alpha channel, and a zero there would make every pixel invisible.
func swapBGRAToRGBA(pixels []byte) {
	for i := 0; i+3 < len(pixels); i += 4 {
		pixels[i], pixels[i+2] = pixels[i+2], pixels[i]
		pixels[i+3] = 255
	}
}

// Screenshot returns a detached copy of the framebuffer.
func (c *RFBClient) Screenshot() (image.Image, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tracker.GetImage(), nil
}

// InjectPointer sends a PointerEvent.
func (c *RFBClient) InjectPointer(x, y int, buttonMask uint8) error {
	return c.send(EncodePointerEvent(x, y, buttonMask))
}

// InjectKey sends a KeyEvent.
func (c *RFBClient) InjectKey(keySym uint32, down bool) error {
	return c.send(EncodeKeyEvent(keySym, down))
}

func (c *RFBClient) send(payload []byte) error {
	if c.isClosed() {
		return fmt.Errorf("RFB session is closed")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	_, err := c.conn.Write(payload)
	return err
}

func (c *RFBClient) isClosed() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.closed
}

// Close stops the reader and releases the connection.
func (c *RFBClient) Close() error {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil
	}
	c.closed = true
	conn := c.conn
	c.mu.Unlock()
	err := conn.Close()
	<-c.done
	return err
}

// Width and Height report the negotiated framebuffer size.
func (c *RFBClient) Width() int  { return c.width }
func (c *RFBClient) Height() int { return c.height }

func containsByte(haystack []byte, needle byte) bool {
	for _, b := range haystack {
		if b == needle {
			return true
		}
	}
	return false
}

// joinBytes renders the offered types in ascending order, so the refusal reads
// the same as the reference's ("2, 16", not "16, 2").
func joinBytes(values []byte) string {
	sorted := make([]byte, len(values))
	copy(sorted, values)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j] < sorted[j-1]; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	out := ""
	for i, v := range sorted {
		if i > 0 {
			out += ", "
		}
		out += fmt.Sprint(v)
	}
	return out
}
