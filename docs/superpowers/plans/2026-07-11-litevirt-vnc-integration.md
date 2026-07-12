# Litevirt VNC Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect `uterm` to `litevirt`'s `ProxyVNC` gRPC stream using the Dual-Stream Sidecar architecture.

**Architecture:**
- **Task 1 & 2:** Create the RFB protocol definitions and the Headless AI Client (Stream B) that negotiates a `Raw` VNC session with QEMU, populating the `FramebufferTracker` and handling `gui_click`/`gui_type` via `InjectPointer`/`InjectKey`.
- **Task 3:** Implement the Human Relay (Stream A) which bridges noVNC WebSockets to gRPC, actively sniffing for input packets to drop them if the user doesn't hold the `HijackLease`.

**Tech Stack:** Go, gRPC (litevirt v1 protos), WebSockets (`github.com/coder/websocket`).

---

## Task 1: Add Dependencies and RFB Protocol Definitions

We need the `litevirt` protobufs and basic RFB struct definitions.

**Files:**
- Modify: `packages/provide-uterm-go/go.mod`
- Create: `packages/provide-uterm-go/vnc/rfb.go`

- [ ] **Step 1: Update dependencies**

Run: `cd packages/provide-uterm-go && go get github.com/litevirt/litevirt` (if it fails, add a replace directive: `go mod edit -replace github.com/litevirt/litevirt=../../../../colonelpanik/litevirt` and retry).

- [ ] **Step 2: Define RFB constants and structs**

```go
package vnc

import (
	"encoding/binary"
	"io"
)

// RFB Message Types
const (
	ClientSetPixelFormat = 0
	ClientSetEncodings   = 2
	ClientFramebufferUpdateRequest = 3
	ClientKeyEvent       = 4
	ClientPointerEvent   = 5

	ServerFramebufferUpdate = 0
)

// EncodePointerEvent serializes an RFB PointerEvent to bytes.
func EncodePointerEvent(x, y int, buttonMask uint8) []byte {
	buf := make([]byte, 6)
	buf[0] = ClientPointerEvent
	buf[1] = buttonMask

	// Clamp x and y to uint16
	if x < 0 { x = 0 } else if x > 65535 { x = 65535 }
	if y < 0 { y = 0 } else if y > 65535 { y = 65535 }

	binary.BigEndian.PutUint16(buf[2:4], uint16(x))
	binary.BigEndian.PutUint16(buf[4:6], uint16(y))
	return buf
}

// EncodeKeyEvent serializes an RFB KeyEvent to bytes.
func EncodeKeyEvent(keySym uint32, down bool) []byte {
	buf := make([]byte, 8)
	buf[0] = ClientKeyEvent
	if down {
		buf[1] = 1
	} else {
		buf[1] = 0
	}
	// bytes 2-3 are padding (zero)
	binary.BigEndian.PutUint32(buf[4:8], keySym)
	return buf
}
```

- [ ] **Step 3: Commit**

```bash
cd packages/provide-uterm-go
go mod tidy
git add go.mod go.sum vnc/rfb.go
git commit -m "feat: add litevirt dependency and RFB protocol definitions"
```

---

## Task 2: Headless AI Client Skeleton & Injection

Implement the AI client that connects to `litevirt` and fulfills the `GraphicalSession` interface for input injection.

**Files:**
- Create: `packages/provide-uterm-go/vnc/litevirt_ai.go`

- [ ] **Step 1: Implement AI Client**

```go
package vnc

import (
	"context"
	"image"
	"sync"
	"io"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
)

// LitevirtAIClient is the Headless AI Client (Stream B) for litevirt.
type LitevirtAIClient struct {
	trackerMu sync.Mutex
	tracker   *FramebufferTracker

	streamMu sync.Mutex
	stream   grpc.BidiStreamingClient[pb.VNCData, pb.VNCData]
}

func NewLitevirtAIClient(ctx context.Context, cc grpc.ClientConnInterface, vmName string) (*LitevirtAIClient, error) {
	client := pb.NewLiteVirtClient(cc)
	outCtx := metadata.AppendToOutgoingContext(ctx, "x-vm-name", vmName)

	stream, err := client.ProxyVNC(outCtx)
	if err != nil {
		return nil, err
	}

	return &LitevirtAIClient{
		tracker: NewFramebufferTracker(1920, 1080), // Default bounds, will be resized on ServerInit
		stream:  stream,
	}, nil
}

func (c *LitevirtAIClient) Screenshot() (image.Image, error) {
	c.trackerMu.Lock()
	t := c.tracker
	c.trackerMu.Unlock()

	if t == nil {
		return nil, fmt.Errorf("framebuffer tracker not initialized")
	}
	return t.GetImage(), nil
}

func (c *LitevirtAIClient) InjectPointer(x, y int, buttonMask uint8) error {
	buf := EncodePointerEvent(x, y, buttonMask)

	c.streamMu.Lock()
	defer c.streamMu.Unlock()
	return c.stream.Send(&pb.VNCData{Data: buf})
}

func (c *LitevirtAIClient) InjectKey(keySym uint32, down bool) error {
	buf := EncodeKeyEvent(keySym, down)

	c.streamMu.Lock()
	defer c.streamMu.Unlock()
	return c.stream.Send(&pb.VNCData{Data: buf})
}
```

- [ ] **Step 2: Compile to verify syntax**

Run: `cd packages/provide-uterm-go && go build ./vnc`
Expected: Success

- [ ] **Step 3: Commit**

```bash
cd packages/provide-uterm-go
git add vnc/litevirt_ai.go
git commit -m "feat: implement headless AI client input injection"
```

---

## Task 3: RFB Handshake & Framebuffer Tracker Loop

Wire up the actual RFB protocol handshake and read loop on the `LitevirtAIClient`.

**Files:**
- Modify: `packages/provide-uterm-go/vnc/litevirt_ai.go`

- [ ] **Step 1: Add handshake and read loop methods**

```go
package vnc

import (
	"encoding/binary"
	"fmt"
	"io"
	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
)

// grpcReader adapts the gRPC Recv() stream to an io.Reader
type grpcReader struct {
	stream grpc.BidiStreamingClient[pb.VNCData, pb.VNCData]
	buf    []byte
}

func (r *grpcReader) Read(p []byte) (n int, err error) {
	for len(r.buf) == 0 {
		msg, err := r.stream.Recv()
		if err != nil {
			return 0, err
		}
		r.buf = msg.Data
	}
	n = copy(p, r.buf)
	r.buf = r.buf[n:]
	return n, nil
}

func (c *LitevirtAIClient) RunHandshakeAndLoop() error {
	r := &grpcReader{stream: c.stream}

	// 1. ProtocolVersion
	var ver [12]byte
	if _, err := io.ReadFull(r, ver[:]); err != nil { return err }

	c.streamMu.Lock()
	err := c.stream.Send(&pb.VNCData{Data: []byte("RFB 003.008\n")})
	c.streamMu.Unlock()
	if err != nil { return err }

	// 2. Security
	var numSecTypes [1]byte
	if _, err := io.ReadFull(r, numSecTypes[:]); err != nil { return err }
	if numSecTypes[0] == 0 { return fmt.Errorf("connection failed") }

	secTypes := make([]byte, numSecTypes[0])
	if _, err := io.ReadFull(r, secTypes); err != nil { return err }

	// Assume type 1 (None) is supported
	c.streamMu.Lock()
	err = c.stream.Send(&pb.VNCData{Data: []byte{1}})
	c.streamMu.Unlock()
	if err != nil { return err }

	// SecurityResult
	var secResult [4]byte
	if _, err := io.ReadFull(r, secResult[:]); err != nil { return err }
	if binary.BigEndian.Uint32(secResult[:]) != 0 { return fmt.Errorf("security failed") }

	// 3. ClientInit (shared=1)
	c.streamMu.Lock()
	err = c.stream.Send(&pb.VNCData{Data: []byte{1}})
	c.streamMu.Unlock()
	if err != nil { return err }

	// 4. ServerInit
	var serverInit [24]byte
	if _, err := io.ReadFull(r, serverInit[:]); err != nil { return err }
	width := binary.BigEndian.Uint16(serverInit[0:2])
	height := binary.BigEndian.Uint16(serverInit[2:4])

	// Skip name length and name
	nameLen := binary.BigEndian.Uint32(serverInit[20:24])
	if _, err := io.CopyN(io.Discard, r, int64(nameLen)); err != nil { return err }

	c.trackerMu.Lock()
	c.tracker = NewFramebufferTracker(int(width), int(height))
	c.trackerMu.Unlock()

	// 6. FramebufferUpdate loop
	for {
		var msgType [1]byte
		if _, err := io.ReadFull(r, msgType[:]); err != nil { return err }
		if msgType[0] != ServerFramebufferUpdate {
			return fmt.Errorf("unsupported server message type: %d", msgType[0])
		}

		var header [3]byte // padding + numRects
		if _, err := io.ReadFull(r, header[:]); err != nil { return err }
		numRects := binary.BigEndian.Uint16(header[1:3])

		for i := 0; i < int(numRects); i++ {
			var rect [12]byte // x, y, w, h, encoding
			if _, err := io.ReadFull(r, rect[:]); err != nil { return err }
			rx := int(binary.BigEndian.Uint16(rect[0:2]))
			ry := int(binary.BigEndian.Uint16(rect[2:4]))
			rw := int(binary.BigEndian.Uint16(rect[4:6]))
			rh := int(binary.BigEndian.Uint16(rect[6:8]))
			encoding := binary.BigEndian.Uint32(rect[8:12])

			if encoding != 0 {
				return fmt.Errorf("unsupported encoding: %d", encoding)
			}

			if rw > int(width) || rh > int(height) {
				return fmt.Errorf("rectangle too large")
			}

			pixels := make([]byte, rw*rh*4)
			if _, err := io.ReadFull(r, pixels); err != nil { return err }

			c.trackerMu.Lock()
			t := c.tracker
			c.trackerMu.Unlock()
			if t != nil {
				t.ApplyRawUpdate(rx, ry, rw, rh, pixels)
			}
		}
	}
}
```

- [ ] **Step 2: Commit**

```bash
cd packages/provide-uterm-go
git add vnc/litevirt_ai.go
git commit -m "feat: implement RFB handshake and tracker loop"
```

---

## Task 4: Human Relay (Stream A)

Implement the WebSocket to gRPC proxy that drops unauthorized input.

**Files:**
- Create: `packages/provide-uterm-go/vnc/litevirt_human.go`

- [ ] **Step 1: Implement the Relay**

```go
package vnc

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"net/http"

	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
)

// HijackLeaseManager is a placeholder interface for the access control lock.
type HijackLeaseManager interface {
	HasLease(sessionID string) bool
}

// wsReader adapts a WebSocket connection to an io.Reader
type wsReader struct {
	ctx  context.Context
	conn *websocket.Conn
	buf  []byte
}

func (r *wsReader) Read(p []byte) (n int, err error) {
	for len(r.buf) == 0 {
		_, msg, err := r.conn.Read(r.ctx)
		if err != nil {
			return 0, err
		}
		r.buf = msg
	}
	n = copy(p, r.buf)
	r.buf = r.buf[n:]
	return n, nil
}

// filterRFBInput reads RFB Client-to-Server messages from src.
// If a message is a KeyEvent (4) or PointerEvent (5), it checks the lease.
// Allowed messages are written to dst.
func filterRFBInput(dst io.Writer, src io.Reader, leaseMgr HijackLeaseManager, sessionID string) error {
	// 1. Handshake: Protocol Version (12 bytes)
	if _, err := io.CopyN(dst, src, 12); err != nil { return err }

	// 2. Handshake: Security (1 byte)
	var sec [1]byte
	if _, err := io.ReadFull(src, sec[:]); err != nil { return err }
	if sec[0] != 1 { return fmt.Errorf("unsupported security type %d", sec[0]) }
	if _, err := dst.Write(sec[:]); err != nil { return err }

	// 3. Handshake: ClientInit (1 byte)
	if _, err := io.CopyN(dst, src, 1); err != nil { return err }

	// Normal Phase
	var msgType [1]byte
	for {
		if _, err := io.ReadFull(src, msgType[:]); err != nil { return err }

		switch msgType[0] {
		case ClientSetPixelFormat: // 0 (20 bytes total)
			if _, err := dst.Write(msgType[:]); err != nil { return err }
			if _, err := io.CopyN(dst, src, 19); err != nil { return err }

		case ClientSetEncodings: // 2
			var header [3]byte // padding(1) + num(2)
			if _, err := io.ReadFull(src, header[:]); err != nil { return err }
			num := binary.BigEndian.Uint16(header[1:3])

			if _, err := dst.Write(msgType[:]); err != nil { return err }
			if _, err := dst.Write(header[:]); err != nil { return err }
			if num > 0 {
				if _, err := io.CopyN(dst, src, int64(num)*4); err != nil { return err }
			}

		case ClientFramebufferUpdateRequest: // 3 (10 bytes total)
			if _, err := dst.Write(msgType[:]); err != nil { return err }
			if _, err := io.CopyN(dst, src, 9); err != nil { return err }

		case ClientKeyEvent: // 4 (8 bytes total)
			payload := make([]byte, 7)
			if _, err := io.ReadFull(src, payload); err != nil { return err }

			if leaseMgr == nil || leaseMgr.HasLease(sessionID) {
				if _, err := dst.Write(msgType[:]); err != nil { return err }
				if _, err := dst.Write(payload); err != nil { return err }
			}

		case ClientPointerEvent: // 5 (6 bytes total)
			payload := make([]byte, 5)
			if _, err := io.ReadFull(src, payload); err != nil { return err }

			if leaseMgr == nil || leaseMgr.HasLease(sessionID) {
				if _, err := dst.Write(msgType[:]); err != nil { return err }
				if _, err := dst.Write(payload); err != nil { return err }
			}

		case 6: // ClientCutText
			var header [7]byte // padding(3) + length(4)
			if _, err := io.ReadFull(src, header[:]); err != nil { return err }
			length := binary.BigEndian.Uint32(header[3:7])

			if length > 1048576 {
				return fmt.Errorf("ClientCutText too large")
			}

			payload := make([]byte, int(length))
			if length > 0 {
				if _, err := io.ReadFull(src, payload); err != nil { return err }
			}

			if leaseMgr == nil || leaseMgr.HasLease(sessionID) {
				if _, err := dst.Write(msgType[:]); err != nil { return err }
				if _, err := dst.Write(header[:]); err != nil { return err }
				if length > 0 {
					if _, err := dst.Write(payload); err != nil { return err }
				}
			}

		default:
			// Unknown client message type, cannot safely parse length to skip.
			return fmt.Errorf("unknown RFB client message type: %d", msgType[0])
		}
	}
}

// grpcWriter adapts the gRPC Send() stream to an io.Writer
type grpcWriter struct {
	stream grpc.BidiStreamingClient[pb.VNCData, pb.VNCData]
}

func (w *grpcWriter) Write(p []byte) (n int, err error) {
	if err := w.stream.Send(&pb.VNCData{Data: p}); err != nil {
		return 0, err
	}
	return len(p), nil
}

// ServeHumanRelay proxies a WebSocket to litevirt ProxyVNC, dropping input if no lease is held.
func ServeHumanRelay(w http.ResponseWriter, r *http.Request, cc grpc.ClientConnInterface, vmName string, leaseMgr HijackLeaseManager, sessionID string) {
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		slog.Error("websocket accept failed", "error", err)
		return
	}
	// Force close on exit to prevent leaks
	defer c.Close(websocket.StatusInternalError, "handler exited")

	ctx, cancel := context.WithCancel(context.Background()) // Detach from request context for long-lived streams
	defer cancel()

	client := pb.NewLiteVirtClient(cc)
	outCtx := metadata.AppendToOutgoingContext(ctx, "x-vm-name", vmName)

	stream, err := client.ProxyVNC(outCtx)
	if err != nil {
		slog.Error("proxyvnc dial failed", "error", err)
		c.Close(websocket.StatusInternalError, "grpc dial failed")
		return
	}

	errCh := make(chan error, 2)

	// Server -> Client (Video)
	go func() {
		for {
			msg, err := stream.Recv()
			if err != nil {
				errCh <- err
				return
			}
			if err := c.Write(ctx, websocket.MessageBinary, msg.Data); err != nil {
				errCh <- err
				return
			}
		}
	}()

	// Client -> Server (Input Filter)
	go func() {
		src := &wsReader{ctx: ctx, conn: c}
		dst := &grpcWriter{stream: stream}
		errCh <- filterRFBInput(dst, src, leaseMgr, sessionID)
	}()

	err = <-errCh
	if err != nil && err != io.EOF {
		slog.Error("human relay error", "error", err)
		c.Close(websocket.StatusInternalError, err.Error())
	} else {
		c.Close(websocket.StatusNormalClosure, "eof")
	}
}
```

- [ ] **Step 2: Compile to verify syntax**

Run: `cd packages/provide-uterm-go && go build ./vnc`
Expected: Success

- [ ] **Step 3: Commit**

```bash
cd packages/provide-uterm-go
go mod tidy
git add go.mod go.sum vnc/litevirt_human.go
git commit -m "feat: add robust stateful RFB input filter and human websocket relay"
```
