// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package gui

// Tree construction and block emission, ported from zlib's trees.c. See
// zlibrle.go for why this is a port rather than a call into compress/flate. // codespell:ignore flate

type symbol struct {
	dist   uint16 // 0 for a literal
	lenLit uint16 // the literal byte, or matchLength-minMatch
}

type deflator struct {
	w *bitWriter

	dynLtree [heapSize]ctData
	dynDtree [2*dCodes + 1]ctData
	blTree   [2*blCodes + 1]ctData

	lDesc  treeDesc
	dDesc  treeDesc
	blDesc treeDesc

	blCount [maxBits + 1]uint16
	heap    [heapSize]int
	heapLen int
	heapMax int
	depth   [heapSize]uint8

	syms      []symbol
	optLen    uint64
	staticLen uint64
	matches   int
}

func newDeflator(w *bitWriter) *deflator {
	d := &deflator{w: w, syms: make([]symbol, 0, symbolsPerBlock)}
	d.initBlock()
	return d
}

func (d *deflator) initBlock() {
	for i := range d.dynLtree {
		d.dynLtree[i] = ctData{}
	}
	for i := range d.dynDtree {
		d.dynDtree[i] = ctData{}
	}
	for i := range d.blTree {
		d.blTree[i] = ctData{}
	}
	d.dynLtree[endBlock].freq = 1
	d.optLen, d.staticLen, d.matches = 0, 0, 0
	d.syms = d.syms[:0]
}

// tally records one symbol and reports whether the block is full. zlib flushes
// at lit_bufsize-1 symbols; matching that is what puts block boundaries in the
// same places, and block boundaries are visible in the output.
func (d *deflator) tally(dist, lc int) bool {
	if dist == 0 {
		d.syms = append(d.syms, symbol{dist: 0, lenLit: uint16(lc)})
		d.dynLtree[lc].freq++
	} else {
		d.matches++
		d.syms = append(d.syms, symbol{dist: uint16(dist), lenLit: uint16(lc)})
		d.dynLtree[int(lengthCode[lc])+256+1].freq++
		// zlib's _tr_tally_dist decrements the distance before looking up its
		// code, so distance 1 is code 0. Counting code 1 here while
		// compressBlock emits code 0 builds a tree that does not describe the
		// symbols it is sent with, and the stream will not inflate.
		d.dynDtree[dCode(dist-1)].freq++
	}
	return len(d.syms) == symbolsPerBlock
}

func dCode(dist int) int {
	if dist < 256 {
		return int(distCode[dist])
	}
	return int(distCode[256+(dist>>7)])
}

// smaller is zlib's heap ordering. The tie-break on depth is not incidental:
// it decides the shape of the tree when frequencies match, and therefore every
// code the block emits.
func (d *deflator) smaller(tree []ctData, n, m int) bool {
	return tree[n].freq < tree[m].freq ||
		(tree[n].freq == tree[m].freq && d.depth[n] <= d.depth[m])
}

func (d *deflator) pqdownheap(tree []ctData, k int) {
	v := d.heap[k]
	j := k << 1
	for j <= d.heapLen {
		if j < d.heapLen && d.smaller(tree, d.heap[j+1], d.heap[j]) {
			j++
		}
		if d.smaller(tree, v, d.heap[j]) {
			break
		}
		d.heap[k] = d.heap[j]
		k = j
		j <<= 1
	}
	d.heap[k] = v
}

// genBitlen walks the built tree to lengths, then redistributes any code
// longer than maxBits — the overflow loop is load-bearing for identity.
func (d *deflator) genBitlen(desc *treeDesc) {
	tree := desc.dynTree
	stree := desc.statTree
	maxCode := desc.maxCode
	overflow := 0

	for bits := 0; bits <= maxBits; bits++ {
		d.blCount[bits] = 0
	}
	tree[d.heap[d.heapMax]].blen = 0

	for h := d.heapMax + 1; h < heapSize; h++ {
		n := d.heap[h]
		bits := int(tree[int(tree[n].dad)].blen) + 1
		if bits > desc.maxLength {
			bits = desc.maxLength
			overflow++
		}
		tree[n].blen = uint16(bits)
		if n > maxCode {
			continue
		}
		d.blCount[bits]++
		xbits := 0
		if n >= desc.extraBase {
			xbits = desc.extraBits[n-desc.extraBase]
		}
		f := uint64(tree[n].freq)
		d.optLen += f * uint64(bits+xbits)
		if stree != nil {
			d.staticLen += f * uint64(int(stree[n].blen)+xbits)
		}
	}
	if overflow == 0 {
		return
	}

	for {
		bits := desc.maxLength - 1
		for d.blCount[bits] == 0 {
			bits--
		}
		d.blCount[bits]--
		d.blCount[bits+1] += 2
		d.blCount[desc.maxLength]--
		overflow -= 2
		if overflow <= 0 {
			break
		}
	}

	h := heapSize
	for bits := desc.maxLength; bits != 0; bits-- {
		n := int(d.blCount[bits])
		for n != 0 {
			h--
			m := d.heap[h]
			if m > maxCode {
				continue
			}
			if int(tree[m].blen) != bits {
				d.optLen += (uint64(bits) - uint64(tree[m].blen)) * uint64(tree[m].freq)
				tree[m].blen = uint16(bits)
			}
			n--
		}
	}
}

func (d *deflator) buildTree(desc *treeDesc) {
	tree := desc.dynTree
	elems := desc.elems
	maxCode := -1

	d.heapLen = 0
	d.heapMax = heapSize
	for n := 0; n < elems; n++ {
		if tree[n].freq != 0 {
			d.heapLen++
			d.heap[d.heapLen] = n
			maxCode = n
			d.depth[n] = 0
		} else {
			tree[n].blen = 0
		}
	}

	// A tree needs two codes even when the data used fewer, so that a decoder
	// always has something to read.
	for d.heapLen < 2 {
		d.heapLen++
		node := 0
		if maxCode < 2 {
			maxCode++
			node = maxCode
		}
		d.heap[d.heapLen] = node
		tree[node].freq = 1
		d.depth[node] = 0
		d.optLen--
		if desc.statTree != nil {
			d.staticLen -= uint64(desc.statTree[node].blen)
		}
	}
	desc.maxCode = maxCode

	for n := d.heapLen / 2; n >= 1; n-- {
		d.pqdownheap(tree, n)
	}

	node := elems
	for {
		n := d.heap[1]
		d.heap[1] = d.heap[d.heapLen]
		d.heapLen--
		d.pqdownheap(tree, 1)
		m := d.heap[1]

		d.heapMax--
		d.heap[d.heapMax] = n
		d.heapMax--
		d.heap[d.heapMax] = m

		tree[node].freq = tree[n].freq + tree[m].freq
		dn, dm := d.depth[n], d.depth[m]
		if dm > dn {
			dn = dm
		}
		d.depth[node] = dn + 1
		tree[n].dad = uint16(node)
		tree[m].dad = uint16(node)

		d.heap[1] = node
		node++
		d.pqdownheap(tree, 1)
		if d.heapLen < 2 {
			break
		}
	}
	d.heapMax--
	d.heap[d.heapMax] = d.heap[1]

	d.genBitlen(desc)
	genCodes(tree, desc.maxCode, &d.blCount)
}

// scanTree counts the code-length symbols a tree will need, using deflate's
// run-length codes for repeats.
func (d *deflator) scanTree(tree []ctData, maxCode int) {
	prevlen := -1
	nextlen := int(tree[0].blen)
	count := 0
	maxCount, minCount := 7, 4
	if nextlen == 0 {
		maxCount, minCount = 138, 3
	}
	tree[maxCode+1].blen = 0xffff

	for n := 0; n <= maxCode; n++ {
		curlen := nextlen
		nextlen = int(tree[n+1].blen)
		count++
		if count < maxCount && curlen == nextlen {
			continue
		}
		switch {
		case count < minCount:
			d.blTree[curlen].freq += uint16(count)
		case curlen != 0:
			if curlen != prevlen {
				d.blTree[curlen].freq++
			}
			d.blTree[repeat3To6].freq++
		case count <= 10:
			d.blTree[repeatZ3To10].freq++
		default:
			d.blTree[repeatZ11To138].freq++
		}
		count = 0
		prevlen = curlen
		switch {
		case nextlen == 0:
			maxCount, minCount = 138, 3
		case curlen == nextlen:
			maxCount, minCount = 6, 3
		default:
			maxCount, minCount = 7, 4
		}
	}
}

func (d *deflator) sendTree(tree []ctData, maxCode int) {
	prevlen := -1
	nextlen := int(tree[0].blen)
	count := 0
	maxCount, minCount := 7, 4
	if nextlen == 0 {
		maxCount, minCount = 138, 3
	}

	for n := 0; n <= maxCode; n++ {
		curlen := nextlen
		nextlen = int(tree[n+1].blen)
		count++
		if count < maxCount && curlen == nextlen {
			continue
		}
		switch {
		case count < minCount:
			for ; count != 0; count-- {
				d.w.sendCode(curlen, d.blTree[:])
			}
		case curlen != 0:
			if curlen != prevlen {
				d.w.sendCode(curlen, d.blTree[:])
				count--
			}
			d.w.sendCode(repeat3To6, d.blTree[:])
			d.w.sendBits(uint32(count-3), 2)
		case count <= 10:
			d.w.sendCode(repeatZ3To10, d.blTree[:])
			d.w.sendBits(uint32(count-3), 3)
		default:
			d.w.sendCode(repeatZ11To138, d.blTree[:])
			d.w.sendBits(uint32(count-11), 7)
		}
		count = 0
		prevlen = curlen
		switch {
		case nextlen == 0:
			maxCount, minCount = 138, 3
		case curlen == nextlen:
			maxCount, minCount = 6, 3
		default:
			maxCount, minCount = 7, 4
		}
	}
}

func (d *deflator) buildBlTree() int {
	d.scanTree(d.dynLtree[:], d.lDesc.maxCode)
	d.scanTree(d.dynDtree[:], d.dDesc.maxCode)
	d.buildTree(&d.blDesc)

	maxBlindex := blCodes - 1
	for ; maxBlindex >= 3; maxBlindex-- {
		if d.blTree[blOrder[maxBlindex]].blen != 0 {
			break
		}
	}
	d.optLen += 3*(uint64(maxBlindex)+1) + 5 + 5 + 4
	return maxBlindex
}

func (d *deflator) sendAllTrees(lcodes, dcodes, blcodes int) {
	d.w.sendBits(uint32(lcodes-257), 5)
	d.w.sendBits(uint32(dcodes-1), 5)
	d.w.sendBits(uint32(blcodes-4), 4)
	for rank := 0; rank < blcodes; rank++ {
		d.w.sendBits(uint32(d.blTree[blOrder[rank]].blen), 3)
	}
	d.sendTree(d.dynLtree[:], lcodes-1)
	d.sendTree(d.dynDtree[:], dcodes-1)
}

func (d *deflator) compressBlock(ltree, dtree []ctData) {
	for _, s := range d.syms {
		if s.dist == 0 {
			d.w.sendCode(int(s.lenLit), ltree)
			continue
		}
		lc := int(s.lenLit)
		code := int(lengthCode[lc])
		d.w.sendCode(code+256+1, ltree)
		if extra := extraLbits[code]; extra != 0 {
			d.w.sendBits(uint32(lc-baseLength[code]), uint(extra))
		}
		dist := int(s.dist) - 1
		code = dCode(dist)
		d.w.sendCode(code, dtree)
		if extra := extraDbits[code]; extra != 0 {
			d.w.sendBits(uint32(dist-baseDist[code]), uint(extra))
		}
	}
	d.w.sendCode(endBlock, ltree)
}

// flushBlock emits one deflate block, choosing stored, static or dynamic the
// way zlib does. The tie going to static is deliberate: `static_lenb <=
// opt_lenb` collapses first, so an exact tie emits the static tree.
func (d *deflator) flushBlock(raw []byte, last bool) {
	d.lDesc = treeDesc{dynTree: d.dynLtree[:], statTree: staticLtree[:], extraBits: extraLbits[:], extraBase: 257, elems: lCodes, maxLength: maxBits}
	d.dDesc = treeDesc{dynTree: d.dynDtree[:], statTree: staticDtree[:], extraBits: extraDbits[:], extraBase: 0, elems: dCodes, maxLength: maxBits}
	d.blDesc = treeDesc{dynTree: d.blTree[:], statTree: nil, extraBits: extraBlbits[:], extraBase: 0, elems: blCodes, maxLength: maxBlBits}

	d.buildTree(&d.lDesc)
	d.buildTree(&d.dDesc)
	maxBlindex := d.buildBlTree()

	optLenb := (d.optLen + 3 + 7) >> 3
	staticLenb := (d.staticLen + 3 + 7) >> 3
	if staticLenb <= optLenb {
		optLenb = staticLenb
	}

	lastBit := uint32(0)
	if last {
		lastBit = 1
	}

	switch {
	case uint64(len(raw))+4 <= optLenb && raw != nil:
		d.w.sendBits(lastBit, 3) // BTYPE 00 with BFINAL in bit 0
		d.w.alignToByte()
		n := len(raw)
		d.w.out = append(d.w.out, byte(n), byte(n>>8), byte(^uint16(n)), byte(^uint16(n)>>8))
		d.w.out = append(d.w.out, raw...)
	case staticLenb == optLenb:
		d.w.sendBits(lastBit|(1<<1), 3)
		d.compressBlock(staticLtree[:], staticDtree[:])
	default:
		d.w.sendBits(lastBit|(2<<1), 3)
		d.sendAllTrees(d.lDesc.maxCode+1, d.dDesc.maxCode+1, maxBlindex+1)
		d.compressBlock(d.dynLtree[:], d.dynDtree[:])
	}

	d.initBlock()
	if last {
		d.w.windup()
	}
}
