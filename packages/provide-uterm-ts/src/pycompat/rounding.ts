//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython-compatible numeric rounding.
 *
 * The reference implementation is Python, so any port that reproduces a
 * quantiser has to reproduce Python's tie-breaking too. `Math.round` rounds
 * halves away from zero; CPython's `round()` rounds halves to even
 * ("banker's rounding") — the same rule Go spells `math.RoundToEven` and C#
 * spells `MidpointRounding.ToEven`.
 */

/**
 * Round `value` to the nearest integer, breaking exact ties towards the even
 * neighbour — the behaviour of CPython's single-argument `round()`.
 *
 * Returns `0` rather than `-0` for values that round to zero, matching
 * CPython's integer result.
 */
export function pyRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  let result: number;
  if (diff > 0.5) {
    result = floor + 1;
  } else if (diff < 0.5) {
    result = floor;
  } else {
    result = floor % 2 === 0 ? floor : floor + 1;
  }
  // `Math.floor(-0.4)` is -1 and `-1 + 1` is -0; CPython yields the integer 0.
  return result === 0 ? 0 : result;
}

/** Scratch space for reading a double's bits. */
const BITS = new DataView(new ArrayBuffer(8));

/**
 * A finite double as an exact `mantissa * 2 ** exponent`.
 *
 * Every double is a dyadic rational, so this is lossless — which is the whole
 * point: the tie test has to be asked of the stored value.
 */
function decompose(value: number): { mantissa: bigint; exponent: number } {
  BITS.setFloat64(0, value);
  const bits = BITS.getBigUint64(0);
  // Both masks are wider than they strictly need to be to change an answer.
  // The sign bit is always clear, because this is only ever called on a
  // magnitude; and bit 52 is the exponent's low bit, which a normal ORs back
  // in below and a subnormal has clear. They are written exactly rather than
  // loosely because a mask that happens not to matter still says which field
  // is being read.
  const rawExponent = Number((bits >> 52n) & 0x7ffn);
  const rawMantissa = bits & 0xf_ffff_ffff_ffffn;
  // Subnormals have no implicit leading one and sit one exponent higher than
  // the stored zero would suggest.
  return rawExponent === 0
    ? { mantissa: rawMantissa, exponent: -1074 }
    : { mantissa: rawMantissa | 0x10_0000_0000_0000n, exponent: rawExponent - 1075 };
}

/** `10 ** power` as an integer. */
function tenTo(power: number): bigint {
  return 10n ** BigInt(power);
}

/**
 * Round `value` to `ndigits` decimal places, as CPython's two-argument
 * `round()`.
 *
 * This is not `pyRound(value * 10 ** ndigits) / 10 ** ndigits`. Two things
 * separate them:
 *
 * - Ties go to the even neighbour, where `Math.round` and `toFixed` both go
 *   away from zero.
 * - The tie test is asked of the *stored* value rather than of the scaled
 *   one. Multiplying by a power of ten can move a value that was just off a
 *   half onto one, and vice versa, so the two disagree on the neighbours of
 *   an exact tie.
 *
 * So the arithmetic is done exactly, on the double's own mantissa and
 * exponent, and only the final quotient becomes a double again.
 *
 * Values with no decimal expansion — NaN and the infinities — pass straight
 * through, as they do in CPython.
 */
export function pyRoundTo(value: number, ndigits: number): number {
  if (!Number.isFinite(value)) {
    return value;
  }
  // Signs are handled by reflection: round-half-to-even is symmetric, and
  // working on the magnitude keeps the integer division from having to care
  // which way it truncates.
  const negative = value < 0 || Object.is(value, -0);
  const { mantissa, exponent } = decompose(Math.abs(value));
  const scale = tenTo(ndigits);

  let quotient: bigint;
  if (exponent >= 0) {
    // No fractional part at all, so there is nothing to decide.
    quotient = mantissa * scale * 2n ** BigInt(exponent);
  } else {
    const denominator = 2n ** BigInt(-exponent);
    const numerator = mantissa * scale;
    quotient = numerator / denominator;
    const remainder = numerator % denominator;
    const twice = remainder * 2n;
    // Strictly greater, then the tie. The two cannot be collapsed into a
    // single `>=`, which would round every tie up; they also cannot differ by
    // an off-by-one, since the denominator is a power of two and both sides
    // are therefore even.
    if (twice > denominator || (twice === denominator && quotient % 2n === 1n)) {
      quotient += 1n;
    }
  }

  // Read back as a decimal rather than divided by the scale as two doubles:
  // `10 ** 400` is already an infinity, so dividing by it loses the answer
  // entirely. Parsing a decimal literal is correctly rounded, which is what
  // CPython does with the same digits.
  const magnitude = Number(`${quotient}e-${ndigits}`);
  return negative ? -magnitude : magnitude;
}
