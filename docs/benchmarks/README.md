<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Benchmarks

How to produce a number from this repository that somebody else can reproduce.

Everything is driven from `Makefile.bench` at the repository root, which is the
single place the parameters are declared. Before it existed, the benchmark
scripts were run ad hoc with whatever frame count and pass count the person
typed, so two results could not be compared — with each other, or with a run
from the previous month.

```bash
make -f Makefile.bench bench-help    # every target
make -f Makefile.bench bench-local   # dev sweep, small dataset
make -f Makefile.bench bench-ci      # full dataset, enforced smoke
```

## The two profiles

| | `bench-local` | `bench-ci` |
|---|---|---|
| Control-frame parity dataset | 20,000 frames, 3 passes | 200,000 frames, 5 passes |
| ANSI throughput smoke | reported | **enforced** (fails past threshold) |
| Intended use | "did my change regress this machine?" | history, cross-language deltas |
| Goes in a table | no | yes |

CI gets the larger dataset because a shared runner is slower *and* noisier: more
frames per pass is what pulls the variance down far enough for a delta to mean
something. A local sweep trades that away for turnaround.

Never put a local number next to a CI number. They are different experiments.

## What makes a number comparable

1. **Same profile.** The parameters that matter — seed, chunk size, data and
   control payload sizes, control ratio, frame count, pass count — are pinned in
   `Makefile.bench`. Overriding one (`make -f Makefile.bench bench-ci
   CI_PASSES=9`) is fine for exploration and disqualifies the result from the
   history.
2. **Same machine.** Every sweep writes `results/env.json` first
   (`scripts/bench_env_fingerprint.py`): git revision and dirty flag, OS,
   architecture, CPU count, and the Python/Go/.NET/Node versions. A comparison
   spanning two fingerprints compares machines, not code.
3. **A clean tree.** `env.json` records `dirty: true`, and that invalidates a
   historical comparison — nobody can reconstruct what was measured.
4. **Deltas, not absolutes.** The ratio between two backends on one machine
   survives a hardware change. `ns/op` does not. Report "C# decodes 2.1× the
   Python baseline", not "C# decodes at 41 ns/op".

## Results

`make -f Makefile.bench bench-*` writes to `docs/benchmarks/results/`, which is
**gitignored**: a result is only meaningful with its fingerprint and its
profile, and a directory of stray local JSON accumulates neither.

To keep a result, attach it to the thing that explains it:

- **A CI run** — upload `docs/benchmarks/results/` as a workflow artifact. The
  optional `control-channel-parity-benchmark` job in `.github/workflows/ci.yml`
  already runs the cross-language sweep on dispatch and schedule.
- **A pull request that claims a performance change** — paste the `bench-ci`
  table plus the `env.json` `host` and `git.revision` fields into the PR.
- **A durable baseline** — commit the JSON under `docs/benchmarks/` with an
  explicit name and date (`control-channel-parity.2026-08-15.ci.json`) *and* its
  `env.json`. Do this sparingly: an unexplained committed baseline is worse than
  no baseline, because the next person will diff against it.

## What each benchmark measures

| Target | Script | Measures |
|---|---|---|
| `bench-parity-{local,ci}` | `scripts/benchmark_control_channel_parity.py` | Control-frame decode throughput in Python, C#, Go, and TypeScript from one synthesized stream (fixed seed), plus the Python optimized-vs-baseline delta |
| `bench-smoke{,-enforced}` | `scripts/run_performance_smoke.py` | `normalize_colors` and `strip_ansi` ns/op, median of 3 runs. The enforced form also runs in `ci/quality_checks.sh` |
| `bench-redaction` | `scripts/benchmark_redaction.py` | Stream-redaction rule throughput |
| `bench-backends` | `scripts/benchmark_backends_impl.py` | FastAPI vs Cloudflare Worker session workloads — handshakes, hijack cycles, broadcast lag, scale tiers. Boots a real server and a real workerd, so it is slow and belongs to neither profile |

The per-language decoder entry points the parity harness drives:

| Language | Entry point |
|---|---|
| Python | `scripts/benchmark_control_channel_memoryview.py` |
| Go | `packages/provide-uterm-go/benchmarks/controlchannel/main.go` |
| C# | `packages/provide-uterm-csharp/benchmarks/ControlChannelDecoderBench/` |
| TypeScript | `packages/provide-uterm-ts/src/control-channel/benchmark.ts` |

## Adding a benchmark

Add the script, then add a target to `Makefile.bench` — do not add a bare
`uv run python scripts/benchmark_*.py` line to a README or a workflow. A
benchmark reachable only by typing a command from memory is one whose parameters
nobody agrees on, which is the state this file exists to end.

If it is cross-language, it must take `--seed` and synthesize its input rather
than reading a fixture, so every port measures the same bytes.
