package vnc

import (
	"context"
	"image"
	"sync"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
)

// LitevirtAIClient is the Headless AI Client (Stream B) for litevirt.
type LitevirtAIClient struct {
	mu      sync.Mutex
	tracker *FramebufferTracker
	stream  grpc.BidiStreamingClient[pb.VNCData, pb.VNCData]
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
	return c.tracker.GetImage(), nil
}

func (c *LitevirtAIClient) InjectPointer(x, y int, buttonMask uint8) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	
	buf := EncodePointerEvent(x, y, buttonMask)
	return c.stream.Send(&pb.VNCData{Data: buf})
}

func (c *LitevirtAIClient) InjectKey(keySym uint32, down bool) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	
	buf := EncodeKeyEvent(keySym, down)
	return c.stream.Send(&pb.VNCData{Data: buf})
}
