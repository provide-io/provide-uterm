//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Divergence detection across a fan-out group.
 *
 * Port of the Python module
 * `provide.uterm.server.bridge.fanout._divergence`.
 *
 * N sessions received the same input; this flags the ones whose output went
 * its own way. Both mistakes cost the operator — a missed flag hides a host
 * that failed, a spurious one buries the real signal — so the similarity is
 * CPython's `difflib` ratio exactly, not an approximation of it.
 */

import { sequenceRatio } from "../pycompat/index.ts";

/**
 * Flag the outputs that diverge from the group's consensus.
 *
 * The consensus is whichever output is on average most similar to all the
 * others — a cheap stand-in for a majority that needs no clustering. Every
 * other output is divergent when its similarity to that one falls below
 * `threshold`.
 *
 * The consensus itself is flagged only when *nothing* supports it: with no
 * output within the threshold there is no agreement to be the majority of,
 * and reporting one arbitrary session as correct would be worse than
 * reporting the whole group as divergent. That is why two disagreeing
 * sessions both come back flagged.
 *
 * A single session is never divergent — there is nothing to diverge from.
 */
export function computeDivergence(outputs: readonly string[], threshold: number): boolean[] {
  const count = outputs.length;
  if (count === 0) {
    return [];
  }
  if (count === 1) {
    return [false];
  }

  const averageSimilarity = outputs.map((candidate, index) => {
    let total = 0;
    for (let other = 0; other < count; other += 1) {
      if (other !== index) {
        total += sequenceRatio(candidate, outputs[other] as string);
      }
    }
    return total / (count - 1);
  });

  const best = Math.max(...averageSimilarity);
  const majorityIndex = averageSimilarity.indexOf(best);
  const majority = outputs[majorityIndex] as string;
  const similarityToMajority = outputs.map((output) => sequenceRatio(output, majority));
  const hasSupporters = similarityToMajority.some(
    (similarity, index) => index !== majorityIndex && similarity >= threshold,
  );

  return outputs.map((_output, index) =>
    index === majorityIndex ? !hasSupporters : (similarityToMajority[index] as number) < threshold,
  );
}
