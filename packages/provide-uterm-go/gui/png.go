// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package gui

import (
	"encoding/binary"
	"errors"
	"hash/crc32"
	"image"
)

// pngSignature is the eight bytes every PNG opens with.
var pngSignature = []byte{137, 80, 78, 71, 13, 10, 26, 10}

// ErrInvalidDimensions is returned for a zero or negative screen.
var ErrInvalidDimensions = errors.New("invalid PNG dimensions")

// EncodeRGBA encodes RGBA8888 pixels as a PNG, byte-identical to the Python,
// TypeScript and C# ports.
//
// Deliberately not image/png. The stdlib encoder picks a colour type from the
// image's contents (an opaque screen becomes RGB, dropping the alpha channel),
// chooses a filter per row, and compresses at its own level — three ways of
// disagreeing with the other ports, on a surface whose bytes are a wire format.
// This writes what they write: colour type 6, filter 0 on every row, and a
// zlib stream at level 9 with the run-length strategy (see zlibrle.go).
func EncodeRGBA(width, height int, pixels []byte) ([]byte, error) {
	if width <= 0 || height <= 0 {
		return nil, ErrInvalidDimensions
	}
	if need := width * height * 4; len(pixels) < need {
		return nil, errors.New("pixel buffer too short")
	}

	rowLen := width * 4
	raw := make([]byte, 0, height*(1+rowLen))
	for y := 0; y < height; y++ {
		raw = append(raw, 0) // filter None
		raw = append(raw, pixels[y*rowLen:(y+1)*rowLen]...)
	}

	out := make([]byte, 0, len(pngSignature)+25+len(raw)/2+12)
	out = append(out, pngSignature...)

	ihdr := make([]byte, 13)
	binary.BigEndian.PutUint32(ihdr[0:4], uint32(width))
	binary.BigEndian.PutUint32(ihdr[4:8], uint32(height))
	ihdr[8] = 8 // bit depth
	ihdr[9] = 6 // colour type: truecolour with alpha
	out = writeChunk(out, "IHDR", ihdr)
	out = writeChunk(out, "IDAT", zlibCompressRLE(raw))
	out = writeChunk(out, "IEND", nil)
	return out, nil
}

// EncodeImage encodes any image.Image, converting to RGBA first so the colour
// type never depends on whether the screen happened to be opaque.
func EncodeImage(img image.Image) ([]byte, error) {
	bounds := img.Bounds()
	rgba, ok := img.(*image.RGBA)
	if !ok || !rgba.Rect.Eq(bounds) || rgba.Stride != bounds.Dx()*4 {
		converted := image.NewRGBA(image.Rect(0, 0, bounds.Dx(), bounds.Dy()))
		for y := 0; y < bounds.Dy(); y++ {
			for x := 0; x < bounds.Dx(); x++ {
				converted.Set(x, y, img.At(bounds.Min.X+x, bounds.Min.Y+y))
			}
		}
		rgba = converted
	}
	return EncodeRGBA(bounds.Dx(), bounds.Dy(), rgba.Pix)
}

func writeChunk(out []byte, kind string, data []byte) []byte {
	var length [4]byte
	binary.BigEndian.PutUint32(length[:], uint32(len(data)))
	out = append(out, length[:]...)
	start := len(out)
	out = append(out, kind...)
	out = append(out, data...)
	var crc [4]byte
	binary.BigEndian.PutUint32(crc[:], crc32.ChecksumIEEE(out[start:]))
	return append(out, crc[:]...)
}

// zlibCompressRLE produces the complete zlib stream that is the IDAT payload:
// the 0x78 0x01 header the other ports emit at level 9 with the run-length
// strategy, the deflate blocks, then adler32 of the uncompressed data.
func zlibCompressRLE(raw []byte) []byte {
	w := &bitWriter{out: make([]byte, 0, len(raw)/2+16)}
	w.out = append(w.out, 0x78, 0x01)

	d := newDeflator(w)
	blockStart := 0
	i := 0
	for i < len(raw) {
		// zlib's deflate_rle: a match is a run of the immediately preceding
		// byte, so it only ever emits distance 1, and only when at least
		// minMatch bytes repeat.
		matchLen := 0
		if i > 0 {
			prev := raw[i-1]
			for matchLen < maxMatch && i+matchLen < len(raw) && raw[i+matchLen] == prev {
				matchLen++
			}
		}

		var full bool
		if matchLen >= minMatch {
			full = d.tally(1, matchLen-minMatch)
			i += matchLen
		} else {
			full = d.tally(0, int(raw[i]))
			i++
		}
		if full {
			d.flushBlock(raw[blockStart:i], false)
			blockStart = i
		}
	}
	d.flushBlock(raw[blockStart:], true)

	var adler [4]byte
	binary.BigEndian.PutUint32(adler[:], adler32(raw))
	return append(w.out, adler[:]...)
}

func adler32(data []byte) uint32 {
	const mod = 65521
	a, b := uint32(1), uint32(0)
	for _, c := range data {
		a = (a + uint32(c)) % mod
		b = (b + a) % mod
	}
	return b<<16 | a
}
