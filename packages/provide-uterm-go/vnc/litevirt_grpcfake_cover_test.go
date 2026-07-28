package vnc

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	pb "github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc/gen/litevirt/v1"
)

// fakeClientStream implements grpc.ClientStream over channels so ProxyVNC can be
// driven without a real transport. recv delivers server->client frames; sent
// captures client->server frames.
type fakeClientStream struct {
	ctx  context.Context
	recv chan []byte
	mu   sync.Mutex
	sent [][]byte
}

func (s *fakeClientStream) Header() (metadata.MD, error) { return nil, nil }
func (s *fakeClientStream) Trailer() metadata.MD         { return nil }
func (s *fakeClientStream) CloseSend() error             { return nil }
func (s *fakeClientStream) Context() context.Context     { return s.ctx }

func (s *fakeClientStream) SendMsg(m any) error {
	v, ok := m.(*pb.VNCData)
	if !ok {
		return errors.New("unexpected send type")
	}
	s.mu.Lock()
	s.sent = append(s.sent, append([]byte(nil), v.Data...))
	s.mu.Unlock()
	return nil
}

func (s *fakeClientStream) RecvMsg(m any) error {
	data, ok := <-s.recv
	if !ok {
		return io.EOF
	}
	m.(*pb.VNCData).Data = data
	return nil
}

// fakeClientConn implements grpc.ClientConnInterface, returning a canned stream.
type fakeClientConn struct {
	stream grpc.ClientStream
	newErr error
}

func (c *fakeClientConn) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	return nil
}

func (c *fakeClientConn) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	if c.newErr != nil {
		return nil, c.newErr
	}
	return c.stream, nil
}

func TestNewLitevirtAIClientSuccess(t *testing.T) {
	cc := &fakeClientConn{stream: &fakeClientStream{ctx: context.Background(), recv: make(chan []byte)}}
	c, err := NewLitevirtAIClient(context.Background(), cc, "vm1")
	if err != nil {
		t.Fatalf("NewLitevirtAIClient: %v", err)
	}
	if c == nil || c.stream == nil {
		t.Fatal("expected initialized client with stream")
	}
}

func TestNewLitevirtAIClientStreamError(t *testing.T) {
	cc := &fakeClientConn{newErr: errors.New("dial boom")}
	if _, err := NewLitevirtAIClient(context.Background(), cc, "vm1"); err == nil {
		t.Fatal("expected stream error")
	}
}

// TestServeHumanRelayEndToEnd drives ServeHumanRelay through a real WebSocket
// upgrade + the fake gRPC stream: it exercises wsReader.Read, filterRFBInput on
// the live path, grpcWriter.Send, the server->client video pump, and shutdown.
func TestServeHumanRelayEndToEnd(t *testing.T) {
	recv := make(chan []byte, 1)
	recv <- []byte("video-frame") // one server->client frame

	fcs := &fakeClientStream{ctx: context.Background(), recv: recv}
	cc := &fakeClientConn{stream: fcs}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ServeHumanRelay(w, r, cc, "vm1", allowPolicy{}, "s", "l", "p", "operator")
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	conn, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "done") }()

	// Read the video frame pushed by the server->client pump.
	typ, data, err := conn.Read(ctx)
	if err != nil {
		t.Fatalf("read video frame: %v", err)
	}
	if typ != websocket.MessageBinary || string(data) != "video-frame" {
		t.Fatalf("unexpected video frame: %q", data)
	}

	// Send a valid RFB handshake + a KeyEvent; filterRFBInput must forward them.
	in := append([]byte(nil), rfbHandshake()...)
	in = append(in, ClientKeyEvent)
	in = append(in, make([]byte, 7)...)
	if err := conn.Write(ctx, websocket.MessageBinary, in); err != nil {
		t.Fatalf("write input: %v", err)
	}

	// Wait for the input pump to forward the handshake upstream (runs
	// concurrently) before tearing the relay down.
	deadline := time.Now().Add(3 * time.Second)
	forwarded := false
	for time.Now().Before(deadline) {
		fcs.mu.Lock()
		n := len(fcs.sent)
		fcs.mu.Unlock()
		if n > 0 {
			forwarded = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !forwarded {
		t.Fatal("expected at least one forwarded upstream frame")
	}

	// End the server->client pump: Recv returns EOF, relay tears down.
	close(recv)

	// The relay closes the socket; the next read returns an error.
	_, _, _ = conn.Read(ctx)
}

// TestServeHumanRelayVideoWriteError covers the c.Write error branch of the
// server->client pump: the WebSocket client goes away while the server keeps
// pushing video frames, so c.Write eventually fails.
func TestServeHumanRelayVideoWriteError(t *testing.T) {
	recv := make(chan []byte, 64)
	// Preload many frames so the pump keeps writing after the client leaves.
	for i := 0; i < 64; i++ {
		recv <- bytes.Repeat([]byte("frame"), 64)
	}
	fcs := &fakeClientStream{ctx: context.Background(), recv: recv}
	cc := &fakeClientConn{stream: fcs}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ServeHumanRelay(w, r, cc, "vm1", allowPolicy{}, "s", "l", "p", "operator")
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	// Read one frame, then abruptly close so subsequent server writes fail.
	if _, _, err := conn.Read(ctx); err != nil {
		t.Fatalf("first read: %v", err)
	}
	_ = conn.Close(websocket.StatusGoingAway, "bye")
	// Give the relay time to observe the write failure and tear down.
	time.Sleep(200 * time.Millisecond)
}

// TestServeHumanRelayProxyVNCDialError covers the ProxyVNC-dial-failure branch:
// the WebSocket upgrades, but opening the upstream gRPC stream fails.
func TestServeHumanRelayProxyVNCDialError(t *testing.T) {
	cc := &fakeClientConn{newErr: errors.New("upstream down")}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ServeHumanRelay(w, r, cc, "vm1", allowPolicy{}, "s", "l", "p", "operator")
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "done") }()
	// The relay closes the socket immediately after the dial failure.
	if _, _, err := conn.Read(ctx); err == nil {
		t.Fatal("expected relay to close after ProxyVNC dial error")
	}
}

// TestServeHumanRelayNonEOFError covers the error-close branch: the client sends
// an invalid RFB message, so filterRFBInput returns a non-EOF error first.
func TestServeHumanRelayNonEOFError(t *testing.T) {
	recv := make(chan []byte) // never delivers; server->client blocks in Recv
	fcs := &fakeClientStream{ctx: context.Background(), recv: recv}
	cc := &fakeClientConn{stream: fcs}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ServeHumanRelay(w, r, cc, "vm1", allowPolicy{}, "s", "l", "p", "operator")
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close(websocket.StatusNormalClosure, "done") }()

	// A 12-byte version + a bad security type (2) makes filterRFBInput return a
	// non-EOF error, exercising the error-close teardown path.
	bad := append([]byte("RFB 003.008\n"), 2)
	if err := conn.Write(ctx, websocket.MessageBinary, bad); err != nil {
		t.Fatalf("write: %v", err)
	}
	if _, _, err := conn.Read(ctx); err == nil {
		t.Fatal("expected relay to close after filter error")
	}
	close(recv)
}
