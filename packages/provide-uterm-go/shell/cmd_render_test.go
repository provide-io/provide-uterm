//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"bytes"
	"image"
	"image/color"
	"image/gif"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/render"
)

func makePNG(t *testing.T) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, 4, 4))
	for y := 0; y < 4; y++ {
		for x := 0; x < 4; x++ {
			img.Set(x, y, color.RGBA{R: 255, A: 255})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func makeGIF(t *testing.T, n int) []byte {
	t.Helper()
	pal := color.Palette{color.RGBA{A: 255}, color.RGBA{R: 255, G: 255, A: 255}, color.RGBA{G: 255, A: 255}}
	g := &gif.GIF{}
	for i := 0; i < n; i++ {
		p := image.NewPaletted(image.Rect(0, 0, 4, 4), pal)
		for j := range p.Pix {
			p.Pix[j] = uint8((i + j) % len(pal))
		}
		g.Image = append(g.Image, p)
		g.Delay = append(g.Delay, 10) // 10 → 100/10 = 10 fps
	}
	var buf bytes.Buffer
	if err := gif.EncodeAll(&buf, g); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func serveBytes(t *testing.T, data []byte) (*CommandDispatcher, string) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(data)
	}))
	t.Cleanup(srv.Close)
	d := newDispatcher(nil)
	d.client = srv.Client()
	return d, srv.URL
}

func TestRenderNoArgs(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render"); !strings.Contains(got, "usage:") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderHelp(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "help"); !strings.Contains(got, "render") {
		t.Fatalf("= %q", got)
	}
	if got := dispatchText(t, d, "help render"); !strings.Contains(got, "--mode") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderStaticPNG(t *testing.T) {
	d, url := serveBytes(t, makePNG(t))
	if got := dispatchText(t, d, "render "+url); !strings.Contains(got, "▄") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderMode256(t *testing.T) {
	d, url := serveBytes(t, makePNG(t))
	if got := dispatchText(t, d, "render --mode 256 "+url); !strings.Contains(got, "38;5;") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderMode16(t *testing.T) {
	d, url := serveBytes(t, makePNG(t))
	got := dispatchText(t, d, "render --mode 16 "+url)
	if strings.Contains(got, "38;2;") || strings.Contains(got, "38;5;") {
		t.Fatalf("mode 16 emitted extended color: %q", got)
	}
}

func TestRenderCustomColsRows(t *testing.T) {
	d, url := serveBytes(t, makePNG(t))
	if got := dispatchText(t, d, "render --cols 10 --rows 5 "+url); !strings.Contains(got, "▄") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderAnimatedGIF(t *testing.T) {
	d, url := serveBytes(t, makeGIF(t, 3))
	r := dispatchResult(t, d, "render "+url)
	if r.Animated == nil || len(r.Animated.Frames) != 3 || r.Animated.FPS <= 0 || r.Animated.Loop {
		t.Fatalf("= %+v", r)
	}
}

func TestRenderLoopFlag(t *testing.T) {
	d, url := serveBytes(t, makeGIF(t, 3))
	r := dispatchResult(t, d, "render --loop "+url)
	if r.Animated == nil || !r.Animated.Loop {
		t.Fatalf("= %+v", r)
	}
}

func TestRenderFPSOverride(t *testing.T) {
	d, url := serveBytes(t, makeGIF(t, 3))
	r := dispatchResult(t, d, "render --fps 5 "+url)
	if r.Animated == nil || r.Animated.FPS != 5.0 {
		t.Fatalf("= %+v", r)
	}
}

func TestRenderFileURL(t *testing.T) {
	p := filepath.Join(t.TempDir(), "test.png")
	if err := os.WriteFile(p, makePNG(t), 0o600); err != nil {
		t.Fatal(err)
	}
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render file://"+p); !strings.Contains(got, "▄") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderBadScheme(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render ftp://example.com/x.png"); !strings.Contains(got, "unsupported URL scheme") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderNetworkError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	client := srv.Client()
	srv.Close()
	d := newDispatcher(nil)
	d.client = client
	if got := dispatchText(t, d, "render "+url); !strings.Contains(got, "cannot fetch") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderInvalidImage(t *testing.T) {
	d, url := serveBytes(t, []byte("not an image at all"))
	if got := dispatchText(t, d, "render "+url); !strings.Contains(got, "cannot decode image") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderUnknownMode(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --mode rainbow http://x/y.png"); !strings.Contains(got, "unknown mode") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderFileNotFound(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render file:///nonexistent/path/image.png"); !strings.Contains(got, "file not found") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderUnknownFlag(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --bogus-flag http://x/y.png"); !strings.Contains(got, "unknown flag") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderFlagsOnlyNoURL(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --loop"); !strings.Contains(got, "usage:") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderBadColsValue(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --cols abc http://x/y.png"); !strings.Contains(got, "invalid --cols") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderBadRowsValue(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --rows abc http://x/y.png"); !strings.Contains(got, "invalid --rows") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderBadFPSValue(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --fps abc http://x/y.png"); !strings.Contains(got, "invalid --fps") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderEmptyImage(t *testing.T) {
	d, url := serveBytes(t, makePNG(t))
	// Inject a renderer that yields no frames to exercise the empty-image path.
	d.renderImage = func([]byte, int, int, render.ColorMode) ([]string, float64, error) {
		return nil, 0, nil
	}
	if got := dispatchText(t, d, "render "+url); !strings.Contains(got, "empty image") {
		t.Fatalf("= %q", got)
	}
}

func TestRenderModeLastTokenNoValue(t *testing.T) {
	// A value-taking flag as the final token falls through to "unknown flag"
	// (matches the Python guard i+1 < len).
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "render --mode"); !strings.Contains(got, "unknown flag") {
		t.Fatalf("= %q", got)
	}
}
