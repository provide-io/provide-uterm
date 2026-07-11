package vnc

import (
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

// ServeHumanRelay proxies a WebSocket to litevirt ProxyVNC, dropping input if no lease is held.
func ServeHumanRelay(w http.ResponseWriter, r *http.Request, cc grpc.ClientConnInterface, vmName string, leaseMgr HijackLeaseManager, sessionID string) {
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer c.CloseNow()

	ctx := r.Context()
	client := pb.NewLiteVirtClient(cc)
	outCtx := metadata.AppendToOutgoingContext(ctx, "x-vm-name", vmName)
	
	stream, err := client.ProxyVNC(outCtx)
	if err != nil {
		c.Close(websocket.StatusInternalError, "grpc dial failed")
		return
	}

	// Server -> Client (Video)
	go func() {
		for {
			msg, err := stream.Recv()
			if err != nil {
				return
			}
			if err := c.Write(ctx, websocket.MessageBinary, msg.Data); err != nil {
				return
			}
		}
	}()

	// Client -> Server (Input)
	for {
		_, msg, err := c.Read(ctx)
		if err != nil {
			return
		}

		// Simple sniffing: if it's PointerEvent (5) or KeyEvent (4), check lease
		if len(msg) > 0 && (msg[0] == 4 || msg[0] == 5) {
			if leaseMgr != nil && !leaseMgr.HasLease(sessionID) {
				continue // Drop unauthorized input
			}
		}

		if err := stream.Send(&pb.VNCData{Data: msg}); err != nil {
			return
		}
	}
}
