package vnc

import (
	"bytes"
	"io"
	"net/http/httptest"
	"testing"

	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
	"google.golang.org/grpc"
)

type mockStream struct {
	grpc.BidiStreamingClient[pb.VNCData, pb.VNCData]
	recvData [][]byte
	sendData [][]byte
	recvErr  error
	sendErr  error
}

func (m *mockStream) Recv() (*pb.VNCData, error) {
	if len(m.recvData) > 0 {
		data := m.recvData[0]
		m.recvData = m.recvData[1:]
		return &pb.VNCData{Data: data}, nil
	}
	if m.recvErr != nil {
		return nil, m.recvErr
	}
	return nil, io.EOF
}

func (m *mockStream) Send(data *pb.VNCData) error {
	if m.sendErr != nil {
		return m.sendErr
	}
	m.sendData = append(m.sendData, data.Data)
	return nil
}

func TestLitevirtAIClient_Basic(t *testing.T) {
	stream := &mockStream{}
	client := &LitevirtAIClient{
		stream:  stream,
		tracker: NewFramebufferTracker(100, 100),
	}

	_, err := client.Screenshot()
	if err != nil {
		t.Errorf("expected no error from Screenshot")
	}

	_ = client.InjectPointer(10, 10, 1)
	_ = client.InjectKey(0x20, true)

	if len(stream.sendData) != 2 {
		t.Errorf("expected 2 injected events")
	}

	r := &grpcReader{stream: &mockStream{
		recvData: [][]byte{
			[]byte("hello"),
		},
	}}

	buf := make([]byte, 5)
	n, err := r.Read(buf)
	if string(buf[:n]) != "hello" || err != nil {
		t.Errorf("expected hello")
	}
}

func TestFilterRFBInput_Basic(t *testing.T) {
	in := bytes.NewBuffer([]byte("RFB 003.008\n")) // Handshake version
	out := &bytes.Buffer{}

	_ = filterRFBInput(out, in, nil, "s1", "l1", "p1", "admin")

	w := &grpcWriter{stream: &mockStream{}}
	_, _ = w.Write([]byte("test"))
}

func TestLitevirtAIClient_ErrorPaths(t *testing.T) {
	// this panics inside grpc, catch it or skip
	// _, err := NewLitevirtAIClient(context.Background(), nil, "vm1")

	client := &LitevirtAIClient{stream: &mockStream{recvErr: io.EOF}, ready: make(chan struct{})}
	_ = client.RunHandshakeAndLoop()
}

func TestServeHumanRelay_Errors(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET", "/", nil)
	ServeHumanRelay(w, r, nil, "vm1", nil, "s1", "l1", "p1", "admin")
}

func TestFilterRFBInput_More(t *testing.T) {
	// Test various message types
	for _, msgType := range []byte{0, 2, 3, 4, 5, 6, 99} {
		in := bytes.NewBuffer(append([]byte("RFB 003.008\n"), 1, 1, 0))

		// Add specific payload based on msgType to not EOF early
		payload := []byte{msgType}
		switch msgType {
		case 0: // ClientSetPixelFormat
			payload = append(payload, make([]byte, 19)...)
		case 2: // ClientSetEncodings
			payload = append(payload, 0, 0, 0)
		case 3: // ClientFramebufferUpdateRequest
			payload = append(payload, make([]byte, 9)...)
		case 4: // ClientKeyEvent
			payload = append(payload, make([]byte, 7)...)
		case 5: // ClientPointerEvent
			payload = append(payload, make([]byte, 5)...)
		case 6: // ClientCutText
			payload = append(payload, 0, 0, 0, 0, 0, 0, 0) // length 0
		}
		in.Write(payload)

		out := &bytes.Buffer{}
		_ = filterRFBInput(out, in, nil, "s1", "l1", "p1", "admin")
	}
}
