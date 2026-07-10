//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Divergence detection for fan-out sessions. Compares outputs from N sessions
// that received identical input and flags any session whose output diverges
// from the majority consensus. Port of fanout/_divergence.py, including a
// faithful re-implementation of Python's difflib.SequenceMatcher.ratio() so the
// similarity scores (and therefore the majority selection and flags) match the
// Python controller byte-for-byte.

package fanout

// ComputeDivergence returns per-output divergence flags.
//
// It finds the majority output (highest average similarity to all others via
// the SequenceMatcher ratio), then flags each output whose ratio vs. the
// majority falls below threshold. True means divergent. Port of
// compute_divergence.
func ComputeDivergence(outputs []string, threshold float64) []bool {
	n := len(outputs)
	if n == 0 {
		return []bool{}
	}
	if n == 1 {
		return []bool{false}
	}

	// Average similarity of each output against all others.
	avgSim := make([]float64, n)
	for i := 0; i < n; i++ {
		var sum float64
		for j := 0; j < n; j++ {
			if j == i {
				continue
			}
			sum += ratio(outputs[i], outputs[j])
		}
		avgSim[i] = sum / float64(n-1)
	}

	// The majority is the output with the highest average similarity — Python's
	// list.index(max(...)) returns the FIRST such index, replicated here.
	majorityIdx := 0
	best := avgSim[0]
	for i := 1; i < n; i++ {
		if avgSim[i] > best {
			best = avgSim[i]
			majorityIdx = i
		}
	}
	majority := outputs[majorityIdx]

	simToMajority := make([]float64, n)
	for i := 0; i < n; i++ {
		simToMajority[i] = ratio(outputs[i], majority)
	}

	// The majority is flagged only when it has no supporters — i.e., no other
	// output is within threshold of it (no real consensus at all).
	hasSupporters := false
	for i := 0; i < n; i++ {
		if i != majorityIdx && simToMajority[i] >= threshold {
			hasSupporters = true
			break
		}
	}

	flags := make([]bool, n)
	for i := 0; i < n; i++ {
		if i == majorityIdx {
			flags[i] = !hasSupporters
		} else {
			flags[i] = simToMajority[i] < threshold
		}
	}
	return flags
}

// ratio ports difflib.SequenceMatcher(None, a, b).ratio(): 2*M/T where M is the
// total number of matching characters found by the recursive
// longest-match block decomposition, and T = len(a)+len(b). Strings are
// compared as sequences of Unicode code points (runes), matching Python.
func ratio(aStr, bStr string) float64 {
	a := []rune(aStr)
	b := []rune(bStr)
	total := len(a) + len(b)
	if total == 0 {
		return 1.0
	}
	sm := newSeqMatcher(a, b)
	matches := sm.totalMatches()
	return 2.0 * float64(matches) / float64(total)
}

// seqMatcher is a minimal port of difflib.SequenceMatcher configured with
// isjunk=None and autojunk=True (the default), sufficient to compute ratio().
type seqMatcher struct {
	a        []rune
	b        []rune
	b2j      map[rune][]int
	bjunk    map[rune]bool
	bpopular map[rune]bool
}

// newSeqMatcher builds the b2j index with the autojunk heuristic, mirroring
// SequenceMatcher.__chain_b.
func newSeqMatcher(a, b []rune) *seqMatcher {
	sm := &seqMatcher{a: a, b: b, b2j: map[rune][]int{}, bjunk: map[rune]bool{}, bpopular: map[rune]bool{}}
	for i, elt := range b {
		sm.b2j[elt] = append(sm.b2j[elt], i)
	}
	// autojunk: for b longer than 200, drop elements occurring more than 1%.
	n := len(b)
	if n >= 200 {
		ntest := n/100 + 1
		for elt, idxs := range sm.b2j {
			if len(idxs) > ntest {
				sm.bpopular[elt] = true
			}
		}
		for elt := range sm.bpopular {
			delete(sm.b2j, elt)
		}
	}
	return sm
}

// match is a triple (i, j, size): a[i:i+size] == b[j:j+size].
type match struct{ i, j, size int }

// findLongestMatch ports SequenceMatcher.find_longest_match over a[alo:ahi] and
// b[blo:bhi] with isjunk=None (so the junk-extension loops never fire).
func (sm *seqMatcher) findLongestMatch(alo, ahi, blo, bhi int) match {
	besti, bestj, bestsize := alo, blo, 0
	j2len := map[int]int{}
	for i := alo; i < ahi; i++ {
		newj2len := map[int]int{}
		for _, j := range sm.b2j[sm.a[i]] {
			if j < blo {
				continue
			}
			if j >= bhi {
				break
			}
			k := j2len[j-1] + 1
			newj2len[j] = k
			if k > bestsize {
				besti, bestj, bestsize = i-k+1, j-k+1, k
			}
		}
		j2len = newj2len
	}
	// Extend the match over non-junk equal elements on both sides. With
	// isjunk=None, bjunk is empty, so only this (non-junk) extension applies.
	for besti > alo && bestj > blo && !sm.bjunk[sm.b[bestj-1]] && sm.a[besti-1] == sm.b[bestj-1] {
		besti, bestj, bestsize = besti-1, bestj-1, bestsize+1
	}
	for besti+bestsize < ahi && bestj+bestsize < bhi &&
		!sm.bjunk[sm.b[bestj+bestsize]] && sm.a[besti+bestsize] == sm.b[bestj+bestsize] {
		bestsize++
	}
	return match{besti, bestj, bestsize}
}

// totalMatches sums the sizes of every matching block found by the recursive
// decomposition (get_matching_blocks). The block-merge step in difflib does not
// change the total, so ratio only needs the sum.
func (sm *seqMatcher) totalMatches() int {
	type span struct{ alo, ahi, blo, bhi int }
	queue := []span{{0, len(sm.a), 0, len(sm.b)}}
	total := 0
	for len(queue) > 0 {
		s := queue[len(queue)-1]
		queue = queue[:len(queue)-1]
		m := sm.findLongestMatch(s.alo, s.ahi, s.blo, s.bhi)
		if m.size > 0 {
			total += m.size
			if s.alo < m.i && s.blo < m.j {
				queue = append(queue, span{s.alo, m.i, s.blo, m.j})
			}
			if m.i+m.size < s.ahi && m.j+m.size < s.bhi {
				queue = append(queue, span{m.i + m.size, s.ahi, m.j + m.size, s.bhi})
			}
		}
	}
	return total
}
