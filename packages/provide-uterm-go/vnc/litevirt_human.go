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
