// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package gui

import (
	"bytes"
	"compress/zlib"
	"errors"
	"image"
	"image/color"
	"io"
	"testing"
)

func TestEncodeRGBARejectsUnusableInput(t *testing.T) {
	for _, tc := range []struct {
		name          string
		width, height int
		pixels        []byte
	}{
		{"zero width", 0, 1, make([]byte, 4)},
		{"zero height", 1, 0, make([]byte, 4)},
		{"negative width", -1, 1, make([]byte, 4)},
		{"short buffer", 2, 2, make([]byte, 4)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := EncodeRGBA(tc.width, tc.height, tc.pixels); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
	if _, err := EncodeRGBA(0, 1, nil); !errors.Is(err, ErrInvalidDimensions) {
		t.Fatalf("want ErrInvalidDimensions, got %v", err)
	}
}

// TestEncodeImageConvertsForeignImages covers the path that matters for
// identity: whatever the session hands back, the colour type written is 6.
// image/png would have chosen 2 for an opaque screen.
func TestEncodeImageConvertsForeignImages(t *testing.T) {
	nrgba := image.NewNRGBA(image.Rect(0, 0, 2, 2))
	nrgba.Set(0, 0, color.NRGBA{R: 10, G: 20, B: 30, A: 255})
	nrgba.Set(1, 1, color.NRGBA{R: 40, G: 50, B: 60, A: 255})

	encoded, err := EncodeImage(nrgba)
	if err != nil {
		t.Fatal(err)
	}
	if got := encoded[25]; got != 6 {
		t.Fatalf("colour type = %d, want 6 (truecolour with alpha)", got)
	}

	// An image whose bounds do not start at the origin still encodes its own
	// pixels rather than a shifted window of them.
	offset := image.NewRGBA(image.Rect(5, 7, 7, 9))
	offset.Set(5, 7, color.RGBA{R: 10, G: 20, B: 30, A: 255})
	if _, err := EncodeImage(offset); err != nil {
		t.Fatal(err)
	}
}

func TestDCodeSpansBothHalvesOfTheAlphabet(t *testing.T) {
	// Distances below 256 index the table directly; above it they are folded
	// by 7 bits. The RLE matcher only ever emits distance 1, so this is the
	// only place the upper half is exercised.
	for _, tc := range []struct{ dist, want int }{
		{0, 0}, {1, 1}, {4, 4}, {255, int(distCode[255])},
		{256, int(distCode[256+(256>>7)])},
		{5000, int(distCode[256+(5000>>7)])},
	} {
		if got := dCode(tc.dist); got != tc.want {
			t.Errorf("dCode(%d) = %d, want %d", tc.dist, got, tc.want)
		}
	}
}

// TestBlockRoundTripsWithLongDistances drives the emission paths the run-length
// matcher cannot reach — a distance beyond 1, and length and distance codes
// that carry extra bits — by tallying symbols directly.
func TestBlockRoundTripsWithLongDistances(t *testing.T) {
	original := []byte("the quick brown fox jumps over the lazy dog, and then does it again: ")
	raw := append([]byte{}, original...)
	raw = append(raw, original...)

	w := &bitWriter{out: []byte{0x78, 0x01}}
	d := newDeflator(w)
	for _, c := range original {
		d.tally(0, int(c))
	}
	// Repeat the whole first copy as one long match at a large distance.
	d.tally(len(original), len(original)-minMatch)
	d.flushBlock(raw, true)

	stream := append(w.out, 0, 0, 0, 0)
	adler := adler32(raw)
	stream[len(stream)-4] = byte(adler >> 24)
	stream[len(stream)-3] = byte(adler >> 16)
	stream[len(stream)-2] = byte(adler >> 8)
	stream[len(stream)-1] = byte(adler)

	r, err := zlib.NewReader(bytes.NewReader(stream))
	if err != nil {
		t.Fatalf("stream rejected: %v", err)
	}
	back, err := io.ReadAll(r)
	if err != nil {
		t.Fatalf("inflate failed: %v", err)
	}
	if !bytes.Equal(back, raw) {
		t.Fatalf("round-trip mismatch:\n got %q\nwant %q", back, raw)
	}
}
