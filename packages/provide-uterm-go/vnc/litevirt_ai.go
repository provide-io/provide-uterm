package vnc

import (
	"context"
	"encoding/binary"
	"fmt"
	"image"
	"io"
	"sync"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	pb "github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc/gen/litevirt/v1"
)

// MaxRFBDimension caps ServerInit framebuffer size (matches gui.MaxDimension / C#).
const MaxRFBDimension = 8192

// maxRFBNameLen bounds the ServerInit desktop-name field.
const maxRFBNameLen = 4096

// maxRFBCutText bounds ServerCutText / ClientCutText payloads.
const maxRFBCutText = 1 << 20 // 1 MiB

// LitevirtAIClient is the Headless AI Client (Stream B) for litevirt.
type LitevirtAIClient struct {
	trackerMu sync.Mutex
	tracker   *FramebufferTracker

	streamMu sync.Mutex
	stream   grpc.BidiStreamingClient[pb.VNCData, pb.VNCData]

	// ready is closed once RFB handshake + ServerInit complete successfully.
	ready     chan struct{}
	readyOnce sync.Once
	readyErr  error
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
		ready:   make(chan struct{}),
	}, nil
}

// WaitReady blocks until RFB handshake completes or the context is cancelled.
func (c *LitevirtAIClient) WaitReady(ctx context.Context) error {
	select {
	case <-c.ready:
		return c.readyErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (c *LitevirtAIClient) markReady(err error) {
	c.readyOnce.Do(func() {
		c.readyErr = err
		close(c.ready)
	})
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
	err := c.runHandshakeAndLoop()
	if err != nil {
		c.markReady(err)
	}
	return err
}

func (c *LitevirtAIClient) runHandshakeAndLoop() error {
	r := &grpcReader{stream: c.stream}

	// 1. ProtocolVersion
	var ver [12]byte
	if _, err := io.ReadFull(r, ver[:]); err != nil {
		return err
	}
	// Accept RFB 003.00x family only.
	if string(ver[:4]) != "RFB " {
		return fmt.Errorf("unsupported RFB version: %q", string(ver[:]))
	}

	c.streamMu.Lock()
	err := c.stream.Send(&pb.VNCData{Data: []byte("RFB 003.008\n")})
	c.streamMu.Unlock()
	if err != nil {
		return err
	}

	// 2. Security
	var numSecTypes [1]byte
	if _, err := io.ReadFull(r, numSecTypes[:]); err != nil {
		return err
	}
	if numSecTypes[0] == 0 {
		var reasonLen [4]byte
		if _, err := io.ReadFull(r, reasonLen[:]); err != nil {
			return fmt.Errorf("connection failed, could not read reason length")
		}
		l := binary.BigEndian.Uint32(reasonLen[:])
		if l > maxRFBNameLen {
			return fmt.Errorf("connection failed: reason too long")
		}
		reason := make([]byte, l)
		if _, err := io.ReadFull(r, reason); err != nil {
			return fmt.Errorf("connection failed, could not read reason string")
		}
		return fmt.Errorf("connection failed: %s", string(reason))
	}

	secTypes := make([]byte, numSecTypes[0])
	if _, err := io.ReadFull(r, secTypes); err != nil {
		return err
	}

	hasNone := false
	for _, st := range secTypes {
		if st == 1 {
			hasNone = true
			break
		}
	}
	if !hasNone {
		return fmt.Errorf("security type None not supported by server")
	}

	c.streamMu.Lock()
	err = c.stream.Send(&pb.VNCData{Data: []byte{1}})
	c.streamMu.Unlock()
	if err != nil {
		return err
	}

	// SecurityResult
	var secResult [4]byte
	if _, err := io.ReadFull(r, secResult[:]); err != nil {
		return err
	}
	if binary.BigEndian.Uint32(secResult[:]) != 0 {
		return fmt.Errorf("security failed")
	}

	// 3. ClientInit (shared=1)
	c.streamMu.Lock()
	err = c.stream.Send(&pb.VNCData{Data: []byte{1}})
	c.streamMu.Unlock()
	if err != nil {
		return err
	}

	// 4. ServerInit
	var serverInit [24]byte
	if _, err := io.ReadFull(r, serverInit[:]); err != nil {
		return err
	}
	width := binary.BigEndian.Uint16(serverInit[0:2])
	height := binary.BigEndian.Uint16(serverInit[2:4])
	if width == 0 || height == 0 || int(width) > MaxRFBDimension || int(height) > MaxRFBDimension {
		return fmt.Errorf("invalid framebuffer dimensions: %dx%d", width, height)
	}

	// Skip name length and name (bounded)
	nameLen := binary.BigEndian.Uint32(serverInit[20:24])
	if nameLen > maxRFBNameLen {
		return fmt.Errorf("desktop name too long: %d", nameLen)
	}
	if _, err := io.CopyN(io.Discard, r, int64(nameLen)); err != nil {
		return err
	}

	c.trackerMu.Lock()
	c.tracker = NewFramebufferTracker(int(width), int(height))
	c.trackerMu.Unlock()
	c.markReady(nil)

	// 5. SetPixelFormat (RGBA 32-bit) & SetEncodings (Raw = 0)
	pfMsg := make([]byte, 20)
	pfMsg[0] = 0                                  // ClientSetPixelFormat
	pfMsg[4] = 32                                 // bits-per-pixel
	pfMsg[5] = 24                                 // depth
	pfMsg[6] = 1                                  // big-endian flag
	pfMsg[7] = 1                                  // true-colour flag
	binary.BigEndian.PutUint16(pfMsg[8:10], 255)  // red-max
	binary.BigEndian.PutUint16(pfMsg[10:12], 255) // green-max
	binary.BigEndian.PutUint16(pfMsg[12:14], 255) // blue-max
	pfMsg[14] = 24                                // red-shift
	pfMsg[15] = 16                                // green-shift
	pfMsg[16] = 8                                 // blue-shift

	encMsg := make([]byte, 8)
	encMsg[0] = 2                              // ClientSetEncodings
	binary.BigEndian.PutUint16(encMsg[2:4], 1) // 1 encoding
	binary.BigEndian.PutUint32(encMsg[4:8], 0) // Raw = 0

	c.streamMu.Lock()
	err = c.stream.Send(&pb.VNCData{Data: append(pfMsg, encMsg...)})
	c.streamMu.Unlock()
	if err != nil {
		return err
	}

	// Request initial full update
	reqMsg := make([]byte, 10)
	reqMsg[0] = 3 // ClientFramebufferUpdateRequest
	reqMsg[1] = 0 // incremental=0
	binary.BigEndian.PutUint16(reqMsg[2:4], 0)
	binary.BigEndian.PutUint16(reqMsg[4:6], 0)
	binary.BigEndian.PutUint16(reqMsg[6:8], width)
	binary.BigEndian.PutUint16(reqMsg[8:10], height)

	c.streamMu.Lock()
	err = c.stream.Send(&pb.VNCData{Data: reqMsg})
	c.streamMu.Unlock()
	if err != nil {
		return err
	}

	// 6. FramebufferUpdate loop
	for {
		var msgType [1]byte
		if _, err := io.ReadFull(r, msgType[:]); err != nil {
			return err
		}

		switch msgType[0] {
		case ServerFramebufferUpdate: // 0
			var header [3]byte // padding + numRects
			if _, err := io.ReadFull(r, header[:]); err != nil {
				return err
			}
			numRects := binary.BigEndian.Uint16(header[1:3])

			for i := 0; i < int(numRects); i++ {
				var rect [12]byte // x, y, w, h, encoding
				if _, err := io.ReadFull(r, rect[:]); err != nil {
					return err
				}
				rx := int(binary.BigEndian.Uint16(rect[0:2]))
				ry := int(binary.BigEndian.Uint16(rect[2:4]))
				rw := int(binary.BigEndian.Uint16(rect[4:6]))
				rh := int(binary.BigEndian.Uint16(rect[6:8]))
				encoding := binary.BigEndian.Uint32(rect[8:12])

				if encoding != 0 {
					return fmt.Errorf("unsupported encoding: %d", encoding)
				}

				if rw == 0 || rh == 0 || rx+rw > int(width) || ry+rh > int(height) {
					return fmt.Errorf("invalid rectangle bounds")
				}
				if rw > MaxRFBDimension || rh > MaxRFBDimension {
					return fmt.Errorf("rectangle too large")
				}

				pixels := make([]byte, rw*rh*4)
				if _, err := io.ReadFull(r, pixels); err != nil {
					return err
				}

				c.trackerMu.Lock()
				t := c.tracker
				c.trackerMu.Unlock()
				if t != nil {
					if err := t.ApplyRawUpdate(rx, ry, rw, rh, pixels); err != nil {
						return err
					}
				}
			}

			// Request next incremental update
			reqMsg[1] = 1 // incremental=1
			c.streamMu.Lock()
			_ = c.stream.Send(&pb.VNCData{Data: reqMsg})
			c.streamMu.Unlock()

		case 1: // ServerSetColourMapEntries
			var header [5]byte // padding + first + number
			if _, err := io.ReadFull(r, header[:]); err != nil {
				return err
			}
			num := binary.BigEndian.Uint16(header[3:5])
			if _, err := io.CopyN(io.Discard, r, int64(num)*6); err != nil {
				return err
			}

		case 2: // ServerBell
			// skip

		case 3: // ServerServerCutText
			var header [7]byte // padding(3) + length(4)
			if _, err := io.ReadFull(r, header[:]); err != nil {
				return err
			}
			length := binary.BigEndian.Uint32(header[3:7])
			if length > maxRFBCutText {
				return fmt.Errorf("ServerCutText too large: %d", length)
			}
			if _, err := io.CopyN(io.Discard, r, int64(length)); err != nil {
				return err
			}

		default:
			return fmt.Errorf("unsupported server message type: %d", msgType[0])
		}
	}
}

func (c *LitevirtAIClient) Close() error {
	return nil
}
