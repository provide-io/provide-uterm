// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

import {
  ControlFrameDecoder,
  DLE,
  encodeControlFrame,
  encodeTerminalData,
} from "./control-channel.ts";

interface BenchmarkArgs {
  frameCount: number;
  controlRatio: number;
  chunkSize: number;
  dataSize: number;
  controlSize: number;
  seed: number;
  passes: number;
}

interface Summary {
  backend: string;
  generated_bytes: number;
  frame_count: number;
  chunk_size: number;
  median_seconds: number;
  mean_seconds: number;
  min_seconds: number;
  events: number;
  mib_per_s: number;
}

interface ParsedEventCount {
  events: number;
  medianSeconds: number;
  meanSeconds: number;
  minSeconds: number;
}

class XorShift32 {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0;
    if (this.#state === 0) {
      this.#state = 0x9e3779b9;
    }
  }

  #nextUint32(): number {
    let x = this.#state;
    x ^= (x << 13) & 0xffffffff;
    x ^= x >>> 17;
    x ^= (x << 5) & 0xffffffff;
    this.#state = x >>> 0;
    return this.#state;
  }

  nextFloat(): number {
    return this.#nextUint32() / 0x1_0000_0000;
  }
}

function parseNumberArg(argv: string[], index: number): number {
  const key = argv[index];
  if (key === undefined) {
    throw new Error("missing argument name");
  }
  const value = argv[index + 1];
  if (value === undefined) {
    throw new Error(`missing value for ${key}`);
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`invalid value ${value} for ${key}`);
  }
  return parsed;
}

function parseArgs(argv: string[]): BenchmarkArgs {
  const args: BenchmarkArgs = {
    frameCount: 200_000,
    controlRatio: 0.25,
    chunkSize: 4096,
    dataSize: 256,
    controlSize: 128,
    seed: 1337,
    passes: 5,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === undefined) {
      break;
    }
    if (arg === "--help" || arg === "-h") {
      printUsage();
      process.exit(0);
    }

    switch (arg) {
      case "--frame-count":
        args.frameCount = Math.trunc(parseNumberArg(argv, i));
        break;
      case "--control-ratio":
        args.controlRatio = parseNumberArg(argv, i);
        break;
      case "--chunk-size":
        args.chunkSize = Math.trunc(parseNumberArg(argv, i));
        break;
      case "--data-size":
        args.dataSize = Math.trunc(parseNumberArg(argv, i));
        break;
      case "--control-size":
        args.controlSize = Math.trunc(parseNumberArg(argv, i));
        break;
      case "--seed":
        args.seed = Math.trunc(parseNumberArg(argv, i));
        break;
      case "--passes":
        args.passes = Math.trunc(parseNumberArg(argv, i));
        break;
      default:
        if (arg.startsWith("--")) {
          throw new Error(`unknown argument: ${arg}`);
        }
        continue;
    }

    i += 1;
  }

  if (args.frameCount < 1) {
    throw new Error("frame-count must be >= 1");
  }
  if (args.passes < 1) {
    throw new Error("passes must be >= 1");
  }
  if (args.chunkSize < 1) {
    throw new Error("chunk-size must be >= 1");
  }
  if (args.controlRatio <= 0 || args.controlRatio >= 1) {
    throw new Error("control-ratio must be > 0 and < 1");
  }
  if (args.dataSize < 0) {
    throw new Error("data-size must be >= 0");
  }
  if (args.controlSize < 0) {
    throw new Error("control-size must be >= 0");
  }

  return args;
}

function printUsage(): void {
  console.log(
    "Usage: npm run benchmark:control-channel -- --frame-count 200000 --control-ratio 0.25 --chunk-size 4096 --data-size 256 --passes 5",
  );
  console.log("  --frame-count N      Number of frames to generate");
  console.log("  --control-ratio F    Control frame ratio in stream (0 < F < 1)");
  console.log("  --chunk-size N       Chunk size in bytes");
  console.log("  --data-size N        Terminal payload size");
  console.log("  --control-size N     Control payload size");
  console.log("  --seed N             PRNG seed");
  console.log("  --passes N           Repeat benchmark passes");
}

function buildStream(args: BenchmarkArgs): string {
  const rng = new XorShift32(args.seed);
  const dataSegment = "x".repeat(Math.max(0, args.dataSize));
  const controlPayload = "x".repeat(Math.max(0, args.controlSize));
  let stream = "";

  for (let i = 0; i < args.frameCount; i += 1) {
    if (rng.nextFloat() < args.controlRatio) {
      const payload = {
        type: "bench",
        id: i,
        seed: args.seed,
        payload: controlPayload,
      };
      stream += encodeControlFrame(payload);
      continue;
    }

    let segment = dataSegment;
    if (segment.length > 0 && rng.nextFloat() < 0.01) {
      const midpoint = Math.min(64, Math.trunc(segment.length / 2));
      const escaped = `${segment.slice(0, midpoint)}${DLE}DLE_ESC${segment.slice(midpoint)}`;
      segment = escaped.length > segment.length ? escaped.slice(0, segment.length) : escaped;
    }
    stream += encodeTerminalData(segment);
  }
  return stream;
}

function chunkStream(stream: string, chunkSize: number): string[] {
  const bytes = Buffer.from(stream, "utf-8");
  const chunks: string[] = [];

  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    chunks.push(bytes.slice(offset, Math.min(offset + chunkSize, bytes.length)).toString("utf-8"));
  }

  return chunks;
}

function benchmarkStream(chunks: string[], passes: number): ParsedEventCount {
  const samples: number[] = [];
  let events = 0;

  for (let pass = 0; pass < passes; pass += 1) {
    const decoder = new ControlFrameDecoder();
    const start = performance.now();
    let count = 0;

    for (const chunk of chunks) {
      for (const event of decoder.feed(chunk)) {
        count += 1;
        void event;
      }
    }

    for (const event of decoder.finish()) {
      count += 1;
      void event;
    }

    const elapsed = (performance.now() - start) / 1000;
    samples.push(elapsed);
    events = count;
  }

  const sorted = [...samples].sort((a, b) => a - b);
  const median = sorted.length > 0 ? sorted[Math.trunc(sorted.length / 2)] ?? 0 : 0;
  const min = sorted[0] ?? 0;
  const mean = sorted.reduce((acc, value) => acc + value, 0) / sorted.length;

  return {
    medianSeconds: median,
    meanSeconds: mean,
    minSeconds: min,
    events,
  };
}

function medianBenchmark(args: BenchmarkArgs): Summary {
  const stream = buildStream(args);
  if (stream.length === 0) {
    throw new Error("generated stream is empty");
  }

  const chunks = chunkStream(stream, args.chunkSize);
  const result = benchmarkStream(chunks, args.passes);
  const payloadBytes = Buffer.byteLength(stream, "utf-8");
  const payloadMiB = payloadBytes / (1024 * 1024);
  const mibPerS = payloadMiB / result.medianSeconds;

  console.log(`Generated stream: ${payloadBytes} bytes, ${args.frameCount} frames, chunk size ${args.chunkSize}`);
  console.log(`After   (typescript): ${result.medianSeconds.toFixed(4)}s, ${result.medianSeconds.toFixed(4)}s median, ${mibPerS.toFixed(2)} MiB/s`);
  console.log(`Events emitted: ${result.events}`);

  return {
    backend: "typescript",
    generated_bytes: payloadBytes,
    frame_count: args.frameCount,
    chunk_size: args.chunkSize,
    median_seconds: result.medianSeconds,
    mean_seconds: result.meanSeconds,
    min_seconds: result.minSeconds,
    events: result.events,
    mib_per_s: mibPerS,
  };
}

try {
  const args = parseArgs(process.argv.slice(2));
  const summary = medianBenchmark(args);
  console.log(JSON.stringify(summary));
} catch (error) {
  if (error instanceof Error) {
    console.error(error.message);
  } else {
    console.error("Unexpected benchmark failure");
  }
  process.exit(1);
}
