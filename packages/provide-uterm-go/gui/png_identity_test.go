// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package gui

import (
	"bytes"
	"compress/zlib"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"testing"
)

type pngRecord struct {
	Name   string  `json:"name"`
	Width  int     `json:"width"`
	Height int     `json:"height"`
	Error  *string `json:"error"`
	Value  *struct {
		Length int    `json:"length"`
		Sha256 string `json:"sha256"`
		PNG    string `json:"png"`
	} `json:"value"`
}

func goldenPath(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		candidate := filepath.Join(dir, "packages", "provide-uterm-ts", "testdata", "guisession_golden.json")
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("guisession_golden.json not found above cwd")
		}
		dir = parent
	}
}

// TestEncoderMatchesReferenceCorpus holds this port to the bytes the Python
// reference records. A screenshot is a wire format: "a valid PNG of the same
// picture" is not the bar.
func TestEncoderMatchesReferenceCorpus(t *testing.T) {
	blob, err := os.ReadFile(goldenPath(t))
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		PNGs []pngRecord `json:"pngs"`
	}
	if err := json.Unmarshal(blob, &golden); err != nil {
		t.Fatal(err)
	}

	compared := 0
	for _, record := range golden.PNGs {
		if record.Error != nil || record.Value == nil {
			continue
		}
		expected, err := base64.StdEncoding.DecodeString(record.Value.PNG)
		if err != nil {
			t.Fatalf("%s: %v", record.Name, err)
		}
		pixels := pixelsOf(t, expected, record.Width, record.Height)
		actual, err := EncodeRGBA(record.Width, record.Height, pixels)
		if err != nil {
			t.Fatalf("%s: %v", record.Name, err)
		}
		if !bytes.Equal(actual, expected) {
			t.Errorf("%s: bytes differ\n got %s\nwant %s", record.Name,
				hex.EncodeToString(actual), hex.EncodeToString(expected))
			continue
		}
		if got := hex.EncodeToString(hashOf(actual)); got != record.Value.Sha256 {
			t.Errorf("%s: sha256 = %s, want %s", record.Name, got, record.Value.Sha256)
		}
		compared++
	}
	if compared == 0 {
		t.Fatal("no corpus PNGs were compared")
	}
}

func hashOf(b []byte) []byte {
	sum := sha256.Sum256(b)
	return sum[:]
}

func pixelsOf(t *testing.T, png []byte, width, height int) []byte {
	t.Helper()
	var raw []byte
	for offset := 8; offset < len(png); {
		length := int(binary.BigEndian.Uint32(png[offset : offset+4]))
		if string(png[offset+4:offset+8]) == "IDAT" {
			r, err := zlib.NewReader(bytes.NewReader(png[offset+8 : offset+8+length]))
			if err != nil {
				t.Fatal(err)
			}
			raw, err = io.ReadAll(r)
			if err != nil {
				t.Fatal(err)
			}
			break
		}
		offset += 12 + length
	}
	rowLen := width * 4
	pixels := make([]byte, rowLen*height)
	for y := 0; y < height; y++ {
		copy(pixels[y*rowLen:(y+1)*rowLen], raw[y*(rowLen+1)+1:(y+1)*(rowLen+1)])
	}
	return pixels
}

// TestEncoderMatchesReferenceDefaultScreen is the case the small corpus images
// cannot reach: 640x480 compresses to a dynamic Huffman block, where the tree
// construction, the code-length coding and the block-type decision all have to
// agree with zlib rather than merely produce a valid stream.
func TestEncoderMatchesReferenceDefaultScreen(t *testing.T) {
	blob, err := os.ReadFile(goldenPath(t))
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		DefaultSize [2]int `json:"default_size"`
		DefaultPNG  struct {
			Length int    `json:"length"`
			Sha256 string `json:"sha256"`
		} `json:"default_png"`
	}
	if err := json.Unmarshal(blob, &golden); err != nil {
		t.Fatal(err)
	}

	width, height := golden.DefaultSize[0], golden.DefaultSize[1]
	session := NewMemoryGraphicalSession(width, height)
	shot, err := session.Screenshot()
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := EncodeImage(shot)
	if err != nil {
		t.Fatal(err)
	}

	if len(encoded) != golden.DefaultPNG.Length {
		t.Errorf("length = %d, want %d", len(encoded), golden.DefaultPNG.Length)
	}
	if got := hex.EncodeToString(hashOf(encoded)); got != golden.DefaultPNG.Sha256 {
		t.Errorf("sha256 = %s, want %s", got, golden.DefaultPNG.Sha256)
	}
}
