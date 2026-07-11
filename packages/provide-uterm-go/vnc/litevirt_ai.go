package vnc

import (
	"context"
	"fmt"
	"image"
	"sync"

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
