//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * CPython's `statistics` on the ECMAScript engine.
 *
 * `statistics.variance` is not the two-pass float formula. Since CPython 3.8
 * it converts every input to an exact rational, accumulates the sum and the
 * sum of squares exactly, and rounds once at the end:
 *
 * ```
 * ssd = (count * sxx - sx * sx) / count
 * variance = ssd / (count - 1)
 * ```
 *
 * CPython's own comment says that formula has poor numeric properties in
 * floating point and is used only because fractions make it exact. A float
 * implementation agrees on most inputs and differs by one ULP on others —
 * including, notably, giving a non-zero variance for a run of identical
 * values.
 */

/** Bits of mantissa in an IEEE 754 double, including the implicit one. */
const MANTISSA_BITS = 53;

/** Scratch buffer for reading a double's bit pattern. */
const BITS = new DataView(new ArrayBuffer(8));

/**
 * Decompose a finite double into an exact integer ratio.
 *
 * The counterpart of Python's `float.as_integer_ratio`. Every finite double
 * is an integer over a power of two, which is what makes the exact
 * accumulation below cheap: the denominators are all powers of two and never
 * need factoring.
 *
 * @throws {RangeError} For a value that is not finite.
 */
export function exactRatio(value: number): [bigint, bigint] {
  if (!Number.isFinite(value)) {
    throw new RangeError(`cannot convert ${value} to an exact ratio`);
  }
  if (value === 0) {
    return [0n, 1n];
  }
  BITS.setFloat64(0, value);
  const raw = BITS.getBigUint64(0);
  const negative = raw >> 63n === 1n;
  const biasedExponent = Number((raw >> 52n) & 0x7ffn);
  const fraction = raw & 0xf_ffff_ffff_ffffn;

  // Subnormals carry no implicit leading bit and sit at the smallest
  // exponent rather than one below it.
  const mantissa = biasedExponent === 0 ? fraction : fraction | (1n << 52n);
  const exponent = (biasedExponent === 0 ? 1 : biasedExponent) - 1075;

  let numerator = negative ? -mantissa : mantissa;
  let denominator = 1n;
  if (exponent >= 0) {
    numerator <<= BigInt(exponent);
  } else {
    denominator = 1n << BigInt(-exponent);
  }
  // Reduce so the denominator stays the smallest power of two that works.
  const shared = trailingZeros(numerator < 0n ? -numerator : numerator);
  const available = trailingZeros(denominator);
  const reduce = shared < available ? shared : available;
  return [numerator >> reduce, denominator >> reduce];
}

/**
 * How many low bits of `value` are zero.
 *
 * Isolating the lowest set bit and measuring it, rather than shifting in a
 * loop — which also means zero needs no special case.
 */
function trailingZeros(value: bigint): bigint {
  return BigInt(bitLength(value & -value) - 1);
}

/** Position of the highest set bit, counting from one. Non-negative input. */
function bitLength(value: bigint): number {
  return value.toString(2).length;
}

/**
 * Round an exact rational to the nearest double, ties to even.
 *
 * The division happens in exact integers rather than by converting the two
 * halves to `number` first: a numerator and denominator that both overflow to
 * `Infinity` would divide to `NaN`, and a ratio like 10^400 · 3 over 10^400 · 2
 * is exactly 1.5.
 *
 * Defined for ratios whose value lands in the *normal* double range. That
 * covers every caller here — a variance is a sum of squares over a count —
 * and going below it would need the scaling split up to avoid flushing an
 * intermediate power of two to zero.
 *
 * @throws {RangeError} For a zero denominator.
 */
export function ratioToNumber(numerator: bigint, denominator: bigint): number {
  if (denominator === 0n) {
    throw new RangeError("division by zero");
  }
  if (numerator === 0n) {
    return 0;
  }
  const negative = numerator < 0n !== denominator < 0n;
  let num = numerator < 0n ? -numerator : numerator;
  let den = denominator < 0n ? -denominator : denominator;

  // Line the quotient up to hold one more bit than the mantissa, so the
  // rounding decision has something to look at.
  let shift = bitLength(num) - bitLength(den) - MANTISSA_BITS;
  if (shift > 0) {
    den <<= BigInt(shift);
  } else {
    num <<= BigInt(-shift);
  }
  let quotient = num / den;
  let remainder = num % den;
  // The estimate can land one bit wide; drop the extra bit, keeping whether
  // anything was below it so the tie test stays honest.
  if (bitLength(quotient) > MANTISSA_BITS) {
    remainder = (quotient & 1n) * den + remainder;
    quotient >>= 1n;
    den <<= 1n;
    shift += 1;
  }

  const twiceRemainder = remainder * 2n;
  if (twiceRemainder > den || (twiceRemainder === den && (quotient & 1n) === 1n)) {
    quotient += 1n;
    if (bitLength(quotient) > MANTISSA_BITS) {
      quotient >>= 1n;
      shift += 1;
    }
  }

  const magnitude = Number(quotient) * 2 ** shift;
  return negative ? -magnitude : magnitude;
}

/**
 * CPython's `statistics.variance` — the *sample* variance.
 *
 * The `n - 1` denominator is not a detail: the population formula reports a
 * systematically smaller number and would shift every threshold configured
 * against it.
 *
 * @throws {RangeError} For fewer than two values. There is no sample variance
 *   of a single point, and returning zero would read as perfect regularity.
 */
export function pyVariance(data: readonly number[]): number {
  const count = data.length;
  if (count < 2) {
    throw new RangeError("variance requires at least two data points");
  }

  // Every denominator is a power of two, so one common denominator is the
  // largest of them and the sums stay exact integers over it.
  let shift = 0n;
  const ratios = data.map((value) => {
    const ratio = exactRatio(value);
    const bits = trailingZeros(ratio[1]);
    if (bits > shift) {
      shift = bits;
    }
    return ratio;
  });
  const common = 1n << shift;

  let sx = 0n;
  let sxx = 0n;
  for (const [numerator, denominator] of ratios) {
    const scaled = (numerator * common) / denominator;
    sx += scaled;
    sxx += scaled * scaled;
  }

  // ssd = (count * sxx - sx²) / count, over the squared common denominator.
  const bigCount = BigInt(count);
  const ssdNumerator = bigCount * sxx - sx * sx;
  const ssdDenominator = bigCount * common * common;
  return ratioToNumber(ssdNumerator, ssdDenominator * BigInt(count - 1));
}
