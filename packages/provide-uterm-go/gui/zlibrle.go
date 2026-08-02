// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package gui — zlib-compatible deflate, run-length strategy.
//
// A screenshot is a wire format, and every port must emit the same bytes for
// it. The other three reach that by asking their zlib for level 9 with the
// run-length strategy: CPython's `zlib.compressobj(9, DEFLATED, 15, 8, Z_RLE)`,
// node's `deflateSync(raw, {level: 9, strategy: Z_RLE})`, and .NET's
// `ZLibStream` with `ZLibCompressionStrategy.RunLengthEncoding`. Go's
// compress/flate exposes no strategy and picks its own trees, so it cannot be
// asked for the same stream — which is why this file exists.
//
// It is a port of the parts of zlib 1.3 that stream touches: `deflate_rle`
// from deflate.c, and `build_tree`, `gen_bitlen`, `gen_codes`, `scan_tree`,
// `send_tree`, `compress_block` and the block-type decision from trees.c. The
// naming follows zlib rather than Go convention on purpose, so the two can be
// read side by side.
//
// Byte-identity depends on details that look arbitrary and are not:
//   - the tie-break in `smaller` (equal frequencies resolve on `depth`), which
//     decides tree shape and therefore every code emitted;
//   - the bit-length overflow redistribution in `gen_bitlen`;
//   - the block-type decision, where a tie between dynamic and static goes to
//     static;
//   - flushing a block every 16383 symbols, which is `lit_bufsize - 1` for
//     memLevel 8 and is what puts block boundaries in the same places.
//
// Changing any of those changes the bytes, which is why GuiPngIdentityTest
// compares against the corpus the Python reference records.
package gui

const (
	minMatch = 3
	maxMatch = 258

	lengthCodes    = 29           // literal/length codes carrying an extra-bits length
	lCodes         = 256 + 1 + 29 // literals + END_BLOCK + length codes
	dCodes         = 30           // distance codes
	blCodes        = 19           // code-length alphabet
	heapSize       = 2*lCodes + 1 //nolint:gomnd // zlib HEAP_SIZE
	maxBits        = 15           // longest Huffman code
	maxBlBits      = 7            // longest code-length code
	endBlock       = 256          // end-of-block symbol
	repeat3To6     = 16           // repeat previous length 3-6 times
	repeatZ3To10   = 17           // repeat zero 3-10 times
	repeatZ11To138 = 18           // repeat zero 11-138 times

	// lit_bufsize is 1<<(memLevel+6) for memLevel 8; a block is flushed one
	// symbol short of it, which is where zlib puts its block boundaries.
	symbolsPerBlock = (1 << (8 + 6)) - 1
)

var (
	extraLbits  = [lengthCodes]int{0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0}
	extraDbits  = [dCodes]int{0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13}
	extraBlbits = [blCodes]int{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 3, 7}
	blOrder     = [blCodes]int{16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15}

	baseLength [lengthCodes]int
	baseDist   [dCodes]int
	lengthCode [maxMatch - minMatch + 1]uint8
	distCode   [512]uint8

	staticLtree [lCodes + 2]ctData
	staticDtree [dCodes]ctData
)

// ctData is one node of a Huffman tree. zlib overlays freq with code and dad
// with len in a union; they are kept apart here because nothing needs the
// aliasing and the overlap is the easiest way to get this subtly wrong.
type ctData struct {
	freq uint16
	code uint16
	dad  uint16
	blen uint16
}

type treeDesc struct {
	dynTree   []ctData
	statTree  []ctData
	extraBits []int
	extraBase int
	elems     int
	maxLength int
	maxCode   int
}

func init() {
	// Length codes: which code carries each match length, and its base.
	length := 0
	for code := 0; code < lengthCodes-1; code++ {
		baseLength[code] = length
		for n := 0; n < 1<<extraLbits[code]; n++ {
			lengthCode[length] = uint8(code)
			length++
		}
	}
	// Length 258 has its own code rather than sharing code 28's range.
	lengthCode[length-1] = lengthCodes - 1

	// Distance codes, split at 256 because the alphabet changes scale there.
	dist := 0
	for code := 0; code < 16; code++ {
		baseDist[code] = dist
		for n := 0; n < 1<<extraDbits[code]; n++ {
			distCode[dist] = uint8(code)
			dist++
		}
	}
	dist >>= 7
	for code := 16; code < dCodes; code++ {
		baseDist[code] = dist << 7
		for n := 0; n < 1<<(extraDbits[code]-7); n++ {
			distCode[256+dist] = uint8(code)
			dist++
		}
	}

	initStaticTrees()
}

func initStaticTrees() {
	var blCount [maxBits + 1]uint16
	n := 0
	for ; n < 144; n++ {
		staticLtree[n].blen = 8
		blCount[8]++
	}
	for ; n < 256; n++ {
		staticLtree[n].blen = 9
		blCount[9]++
	}
	for ; n < 280; n++ {
		staticLtree[n].blen = 7
		blCount[7]++
	}
	for ; n < 288; n++ {
		staticLtree[n].blen = 8
		blCount[8]++
	}
	genCodes(staticLtree[:], lCodes+1, &blCount)

	for i := 0; i < dCodes; i++ {
		staticDtree[i].blen = 5
		staticDtree[i].code = uint16(biReverse(uint(i), 5))
	}
}

// biReverse mirrors the low `length` bits, because deflate sends Huffman codes
// most-significant-bit first while the bit writer emits low bits first.
func biReverse(code uint, length int) uint {
	res := uint(0)
	for ; length > 0; length-- {
		res |= code & 1
		code >>= 1
		res <<= 1
	}
	return res >> 1
}

// genCodes assigns the canonical code for each length, pre-reversed.
func genCodes(tree []ctData, maxCode int, blCount *[maxBits + 1]uint16) {
	var nextCode [maxBits + 1]uint16
	code := uint16(0)
	for bits := 1; bits <= maxBits; bits++ {
		code = (code + blCount[bits-1]) << 1
		nextCode[bits] = code
	}
	for n := 0; n <= maxCode; n++ {
		l := int(tree[n].blen)
		if l == 0 {
			continue
		}
		tree[n].code = uint16(biReverse(uint(nextCode[l]), l))
		nextCode[l]++
	}
}

// bitWriter emits low-order bits first, as deflate requires.
type bitWriter struct {
	out    []byte
	bitBuf uint32
	bitCnt uint
}

func (w *bitWriter) sendBits(value uint32, length uint) {
	w.bitBuf |= value << w.bitCnt
	w.bitCnt += length
	for w.bitCnt >= 8 {
		w.out = append(w.out, byte(w.bitBuf))
		w.bitBuf >>= 8
		w.bitCnt -= 8
	}
}

func (w *bitWriter) sendCode(c int, tree []ctData) {
	w.sendBits(uint32(tree[c].code), uint(tree[c].blen))
}

// windup flushes the partial byte that a final block leaves behind.
func (w *bitWriter) windup() {
	if w.bitCnt > 0 {
		w.out = append(w.out, byte(w.bitBuf))
	}
	w.bitBuf = 0
	w.bitCnt = 0
}

// alignToByte pads to a byte boundary, which a stored block header requires.
func (w *bitWriter) alignToByte() {
	if w.bitCnt > 0 {
		w.out = append(w.out, byte(w.bitBuf))
		w.bitBuf = 0
		w.bitCnt = 0
	}
}
