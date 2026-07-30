<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The differential fuzz corpus (Layer A, all four ports)

`control_channel_fuzz.json` is a seeded, deterministic corpus of hostile inputs
for the inline DLE/STX control-frame codec, together with what the **CPython
reference** did with each one. Every port — Python, Go, C#, TypeScript — replays
the same file offline. No Python is needed to replay it.

It exists because the codec was fuzzed in one language only. Python runs 200–300
Hypothesis examples per property against this codec; Go, C# and TypeScript saw
about 27 hand-typed vectors. "The decoder survives arbitrary input" was therefore
proven for one implementation and assumed for three — on the framing layer, where
a disagreement is a silent protocol desync between a worker and a hub written in
different languages.

A recorded golden proves agreement on inputs somebody thought of. This is for the
inputs nobody thought of.

| | |
|---|---|
| Corpus | `conformance/fuzz/control_channel_fuzz.json` |
| Generator | `conformance/fuzz/gen_control_channel_fuzz.py` |
| Seed | **20260729** (`CORPUS_SEED`), committed with the corpus |
| Cases | 523 |

## A second corpus: the ANSI layer and the emulator

`ansi_emulator_fuzz.json` (generator `gen_ansi_emulator_fuzz.py`, seed
`20260730`, 435 cases) covers the other large parsing surface, under the same
format rules as this document describes: seeded generation, base64 payloads,
ASCII document, grep-able ids, per-family counts.

Its families split into two kinds, and the difference decides how to read a
failure:

* `normalize`, `upgrade_256`, `upgrade_truecolor` are **pure string
  transforms** over generated SGR text. Nothing but the port's own code decides
  the answer, so a divergence is unambiguously a port bug.
* `emulator` drives a real terminal emulator, and the reference's is built on
  **pyte**. Parity here means reproducing pyte's semantics, which the ports
  already claim for the nineteen hand-written cases in `emulator_golden.json`. A
  divergence is a genuine finding, but it may be a disagreement about an obscure
  corner of a third-party implementation rather than a defect anybody chose.
  Read the case before assuming the port is wrong.

Each `emulator` case records two drives, `chunked` and `single`, exactly as the
decode family here does — and for the same reason, since an emulator holds a
partial escape sequence across a feed. Only **one** generated case currently
distinguishes them, which is worth knowing rather than treating as a gap: pyte
buffers partial sequences properly, so where a chunk boundary falls rarely
changes the screen. A port that buffered naively would fail that one case and
nothing else would notice.

The comparison is the snapshot a consumer actually reads — screen text, cursor,
geometry, the two prompt-detection flags — plus `ansi_screen`, which is where an
SGR disagreement surfaces even when the plain text agrees. Screen *hash* is
deliberately not compared: it is a function of the text, so it would only ever
restate a failure the text already reported.

Geometry is recorded in the corpus (20×6) and asserted by the replay. A port
replaying at a different size would disagree about wrapping for reasons that
have nothing to do with its parser.

**This corpus found three crash classes in the reference before it produced a
single case**, each of which killed the transport read loop that feeds a
session's own output — see `AEF-REG-*` and the emulator's own tests. That is the
argument for generating inputs rather than choosing them.

| Reference replay | `packages/provide-uterm/tests/terminal/test_control_channel_fuzz_corpus.py` |
| Drift check | `ci/check_fuzz_corpus.sh` (runs in `make quality-gate`) |
| Exploration | `conformance/fuzz/explore_control_channel_fuzz.py`, weekly via `.github/workflows/fuzz-explore.yml` |

Regenerate with:

```bash
uv run python conformance/fuzz/gen_control_channel_fuzz.py
```

The same seed always produces a byte-identical file. That is the contract: CI
regenerates from the committed seed and fails on any difference, so all four
ports are provably held to identical inputs.

---

## The encoding rules

These four rules are the whole reason the file is portable. Read them before
adding a field.

### 1. Every string of codec input or output is base64, never a JSON string

Any field named `*_b64` holds **standard base64 (RFC 4648, `+/` alphabet, with
`=` padding) of the UTF-8 encoding of the string**. Decode it to bytes, then
decode those bytes as UTF-8 into your language's string type:

| Language | Decode |
|---|---|
| Go | `b, _ := base64.StdEncoding.DecodeString(s); str := string(b)` |
| C# | `Encoding.UTF8.GetString(Convert.FromBase64String(s))` |
| TypeScript | `new TextDecoder().decode(Buffer.from(s, "base64"))` |
| Python | `base64.b64decode(s).decode("utf-8")` |

Why not just put the string in the JSON? Because JSON cannot carry the inputs
this corpus needs. A JSON string cannot hold a lone surrogate; `json.dumps`
writes `Infinity`/`NaN`, which `JSON.parse` rejects outright; and the four
runtimes do not agree on whether a raw `U+2028` may appear inside a JSON string
literal. base64 is bytes, and bytes are the same everywhere. The existing
`gen_control_channel_golden.py` docstring warns about this trap for strings —
this is the same trap for the byte-shaped inputs, closed the same way.

### 2. The whole file is ASCII

`ensure_ascii=True`, asserted at write time. Every non-ASCII character anywhere
in the document — including inside a recorded control payload — is a `\uXXXX`
escape. No reader has to agree with CPython about file encoding, byte-order
marks, or Unicode normalization.

### 3. No floats, anywhere

Every number in the four asserted families is an integer in `[-2^31, 2^31-1]`.
Not in a payload, not in a length, not anywhere — the sole exception is
`serializer_divergences`, whose whole purpose is to pin float rendering and which
no port asserts equality on. CPython is the only one of the four that keeps
`1.0` distinct from `1` through JSON, and `json.dumps` can emit `Infinity` and
`NaN`, which are not JSON at all. Integers in that range are exact in a Go
`float64`, a JS `number`, and a .NET `double`, so structural comparison of a
decoded payload cannot fail on numeric representation.

Floats are *not* untested — they are pinned in `serializer_divergences` (below).

### 4. Chunk boundaries are between code points, never inside one

All four ports expose a string-typed decoder (`Feed(string)`, `feed(chunk: string)`,
`Feed(string)`, `feed(chunk: str)`), so a chunk physically cannot end halfway
through a code point. The related hostile case — a *declared payload length* that
lands mid-code-point — is a different thing and is covered: see the
`invalid control payload length` rejection.

---

## Case identifiers

Every case carries an `id`. `CCF-DC-0137` is a literal substring of the file, so
a port that prints `case CCF-DC-0137 diverged` gives you `grep CCF-DC-0137
conformance/fuzz/control_channel_fuzz.json` and you are looking at the input.

| Prefix | Family | Stability |
|---|---|---|
| `CCF-ED-nnnn` | `encode_data` | Moves if the seed or that family's count changes |
| `CCF-EC-nnnn` | `encode_control` | Same |
| `CCF-PR-nnnn` | `is_control_frame` | Same |
| `CCF-DC-nnnn` | `decode` | Same |
| `CCF-REG-nnnn` | `regressions` | **Permanent** — hand-assigned, never renumbered |
| `CCF-SD-nnnn` | `serializer_divergences` | **Permanent** — hand-assigned |

Generated ids are stable as long as the seed and the counts are; the two
hand-written families are stable forever, which is why a divergence gets pinned
there rather than by bumping the seed.

---

## Top level

```jsonc
{
  "schema": "provide-uterm/control-channel-fuzz/1",
  "generator": "conformance/fuzz/gen_control_channel_fuzz.py",
  "reference": "CPython provide.uterm.control_channel",
  "seed": 20260729,
  "limits": {
    "header_bytes": 11,
    "max_control_payload_bytes": 1048576,
    "max_frame_depth": 32
  },
  "counts": { "encode_data": 96, "...": 0 },

  "encode_data":  [ /* 96  */ ],
  "encode_control": [ /* 96  */ ],
  "is_control_frame": [ /* 128 */ ],
  "decode": [ /* 192 */ ],
  "regressions": [ /* 5, hand-written, same shape as decode + "note" */ ],
  "serializer_divergences": [ /* 6, NOT asserted equal across ports */ ]
}
```

Refuse to run if `schema` is not `provide-uterm/control-channel-fuzz/1`. A future
incompatible format bumps the trailing integer.

---

## The four asserted families

### `encode_data` — `encodeTerminalData(string) -> string`

```json
{ "id": "CCF-ED-0002",
  "in_b64":  "EPCfnIEwZgIC5L2g8J+YgNCW8J+YgFsQEHobG2Jizqk=",
  "out_b64": "EBDwn5yBMGYCAuS9oPCfmIDQlvCfmIBbEBAQEHobG2Jizqk=" }
```

Decode `in_b64`, call your `encodeTerminalData`, and assert the result equals the
decoded `out_b64`. (This one begins with a DLE, `0x10`, which the encoder doubles
— hence the leading `EBD` instead of `EPC`.)

### `encode_control` — `encodeControlFrame(object) -> string`

```json
{ "id": "CCF-EC-0001",
  "payload": { "k0kiy": null, "k1718i8": null, "k2pqb7": "4@¢W", "k3mt": null },
  "out_b64": "EAIwMDAwMDAzYTp7Imswa2l5IjpudWxsLCJrMTcxOGk4IjpudWxsLCJrMnBxYjciOiI0QMKiVyIsImszbXQiOm51bGx9" }
```

`payload` is a real JSON object — parse it into your language's map type, encode
it, assert byte equality with `out_b64`. Decoded, that `out_b64` is:

```
\x10\x02 0000003a : {"k0kiy":null,"k1718i8":null,"k2pqb7":"4@¢W","k3mt":null}
```

`0x3a` = 58 = the UTF-8 **byte** length of the payload, which is 57 characters —
`¢` is two bytes. That difference is the point of the family.

Two properties of the generated payloads make this assertable in four languages:

- **Keys are emitted in ascending byte order** (`k0…`, `k1…`, `k2…`). Go marshals
  a `map[string]any` with sorted keys; CPython, .NET and ECMAScript preserve
  insertion order. Ascending keys make both rules agree, so key ordering can never
  be what fails your port.
- **String values are drawn from a verified-safe alphabet** — see "Serializer
  divergences".

### `is_control_frame` — `isControlFrame(string) -> bool`

```json
{ "id": "CCF-PR-0000",
  "in_b64": "EFwwMDAwMDAxZjp7ImswIjozMzg2NDcxMDEsImsxb3NzcSI6ZmFsc2V9",
  "out": false }
```

Decode `in_b64`, call the predicate, assert it returns `out`. (Here the byte after
the DLE is `\` rather than STX, so it is not a frame.) 29 of the 128 are `true`.
The predicate is *structural only*: a frame whose payload is not JSON is still
`true` here and still rejected by the decoder.

### `decode` — the incremental decoder, driven twice

This is the family that matters. Every case records **two** drives of the same
stream, and they are not required to agree with each other:

```json
{ "id": "CCF-REG-0001",
  "chunks_b64": ["YRA=", "EGI="],
  "finish": true,
  "chunked": {
    "events": [ { "kind": "data", "data_b64": "YQ==" },
                { "kind": "data", "data_b64": "EGI=" } ],
    "error": null,
    "on_error": []
  },
  "single": {
    "events": [ { "kind": "data", "data_b64": "YRBi" } ],
    "error": null,
    "on_error": []
  } }
```

The stream is `a\x10` + `\x10b`. Fed as two chunks, the decoder flushes `a`
before it has decided what the trailing DLE means, then emits `\x10b` — **two**
data events. Fed as one chunk it emits `a\x10b` — **one**. Same bytes, different
event boundaries. A port that buffers differently passes every single-shot test
and still desynchronises a live session; recording both drives is what catches it.
39 of the 192 generated cases differ between the two drives.

How to replay one case:

- **`chunked`**: create a fresh decoder with an error hook. Decode each element of
  `chunks_b64` and `feed()` it, appending the returned events. If `finish` is
  `true`, then call `finish()` and append its events too.
- **`single`**: create another fresh decoder, `feed()` the concatenation of all the
  chunks, and `finish()` if `finish` is `true`. Feed exactly one chunk even when
  the concatenation is empty — one case has an empty `chunks_b64`, and the single
  drive for it is `feed("")`, which must behave the same as feeding nothing.
- Stop at the first protocol error. Keep the events emitted *before* it.
- `finish` is `false` for 59 of the 192 generated cases. Those streams are left
  mid-flight on purpose: a decoder that eagerly rejects an incomplete frame
  instead of buffering it fails here and nowhere else.

Each drive record has exactly three fields:

| Field | Meaning |
|---|---|
| `events` | Ordered list of what the decoder emitted before it stopped |
| `error` | `null`, or the protocol-error message as a string |
| `on_error` | What the decoder's error hook was called with, in order |

An event is one of:

```json
{ "kind": "data",    "data_b64": "<base64 of the DataChunk string>" }
{ "kind": "control", "control":  { /* the decoded JSON object */ } }
```

`control` is compared **structurally** (deep equality of the parsed value), not by
re-serializing it — that is what rule 3 (no floats) buys you.

`error` is the exact message text. All four reference implementations already use
identical strings, and that is part of the contract. The complete set, and how
often each appears in the `decode` family's chunked drive:

| `error` | Cases |
|---|---|
| `null` (no error) | 58 |
| `invalid control json` | 38 |
| `invalid control header` | 22 |
| `invalid control prefix` | 17 |
| `truncated control frame` | 16 |
| `control payload too large` | 14 |
| `invalid control payload length` | 14 |
| `control payload nests deeper than 32` | 7 |
| `control payload must be an object` | 6 |

`on_error` is always either `[]` or `["control_frame_protocol_error"]` — the hook
fires exactly once per rejection, never twice.

### `regressions`

Same shape as a `decode` case, plus a `"note"` explaining what it catches. These
are hand-written and permanently numbered. **When a divergence is found — by the
weekly exploratory job, or by a port — add it here.** Append a tuple to
`_REGRESSIONS` in the generator, take the next `CCF-REG-nnnn`, regenerate, commit.
Never renumber an existing one.

`CCF-REG-0004` is what that loop looks like in practice: the exploratory job
found it on its first run. Terminal data that precedes a frame the decoder later
rejects is *delivered* when the feed is split — an earlier `feed()` already
returned it — and *discarded* when the whole stream arrives at once, because the
raise throws away the events built so far. Same bytes, same error, different
delivery. A port that got this backwards would lose a screen's worth of output
only on inputs that also happen to be malformed, which is exactly the bug nobody
finds by hand.

---

## `serializer_divergences` — recorded, not asserted

```json
{ "id": "CCF-SD-0001",
  "note": "CPython keeps int/float apart through JSON; Go, .NET and JS all write 0.",
  "payload": { "k0": 0.0 },
  "cpython_out_b64": "EAIwMDAwMDAwYTp7ImswIjowLjB9" }
```

These are inputs where the four runtimes' JSON **serializers** legitimately
disagree, so `encodeControlFrame` legitimately produces different bytes.

**Do not assert your port equals `cpython_out_b64`.** Pin your *own* output in
your own test, the way the TypeScript port already pins the float cases in
`control_channel.test.ts`. The value here is that the divergence is written down
and a change to it shows up in a diff.

The six, and who diverges:

| Id | Input | Divergence |
|---|---|---|
| `CCF-SD-0001` | `0.0` | CPython writes `0.0`; Go, .NET, JS write `0` |
| `CCF-SD-0002` | `[1.0, 1.5, 2]` | Same, mixed with a genuinely fractional value |
| `CCF-SD-0003` | `U+2028`, `U+2029` | Go and .NET escape them; CPython and JS emit raw |
| `CCF-SD-0004` | `U+007F` (DEL) | .NET escapes it; the other three emit raw |
| `CCF-SD-0005` | `U+001F` | .NET writes `\u001F` (upper-case hex); the others `\u001f` |
| `CCF-SD-0006` | `U+1D11E` (astral) | .NET writes the `\uD834\uDD1E` surrogate pair; the others emit raw UTF-8 |

This is why `encode_control`'s string values come from a restricted alphabet:
ASCII `0x20`–`0x7E`, the five short-escape controls `\b \t \n \f \r`, and a
curated set of BMP characters (`¡¢éñÿ ΩαЖה €─│☃ あ你好한`). Every code point in it
was checked against CPython 3.11, Go's `encoding/json` with
`SetEscapeHTML(false)`, .NET 10's `System.Text.Json` with
`UnsafeRelaxedJsonEscaping`, and Node's `JSON.stringify`, and all four produce
identical bytes.

Excluded from that alphabet, measured rather than guessed:

- `U+000B`, `U+000E`–`U+000F`, `U+001A`–`U+001F` — .NET writes `\u001F`, the other
  three `\u001f`. Only escapes containing a hex *letter* diverge, so `U+0000`
  would have been safe; the whole class is excluded rather than the awkward half.
- `U+007F`–`U+00A0` — .NET escapes these; CPython, Go and Node emit them raw.
- `U+2028`/`U+2029` — Go **and** .NET escape these; CPython and Node emit raw.
- Unicode space separators, unassigned code points, and the private-use area —
  .NET escapes them.
- Everything astral — .NET writes a `\uXXXX` surrogate pair where the other three
  emit raw UTF-8.

None of this restriction touches the other three families. `encode_data`,
`is_control_frame` and `decode` never re-serialize a payload, so they fuzz the
**full** range — lone DLEs, C0 and C1 controls, DEL, the whole latin-1 high range,
CJK, box drawing, and astral code points including `𝄞😀🜁`.

Two further classes are deliberately absent from every family because they are
parser divergences with no agreed answer, not codec behaviour:

- **Lone surrogate escapes** in a frame payload (`{"k":"\ud800"}`). CPython's
  `json.loads` yields a lone surrogate; Go substitutes `U+FFFD`.
- **Out-of-range numeric literals** (`1e400`). CPython yields `inf`; Go errors.

---

## What the corpus is sized for

523 cases: 96 + 96 + 128 + 192 generated, plus 5 regressions and 6 divergences.

- **Weighted toward `decode` (192).** It is the only *stateful* surface. A port can
  be right about every single input to the other three and still desynchronise,
  because the bug is in what it buffers across a chunk boundary. The other three
  are pure functions and saturate much faster.
- **Fast enough to run on every commit.** Each case is a handful of microseconds;
  the whole corpus replays in well under a second in every language, so it belongs
  in the normal test run, not a nightly.
- **Small enough to review.** ~170 KB, and a diff after a regeneration is
  readable. Ten thousand cases would replay just as fast but nobody would ever
  look at the diff, and an unreviewable corpus is a corpus that silently records
  a regression as the new truth.

Growing it is a matter of raising `COUNTS` in the generator — but prefer adding a
new *segment kind* or a new *split strategy* over adding more draws from the same
distribution. More of the same finds nothing new.

---

## CI

**Deterministic replay** — every commit:

- `ci/check_fuzz_corpus.sh` (inside `make quality-gate`) regenerates the corpus
  from the committed seed and fails if a single byte moved. This catches both a
  stale corpus (the reference changed and the recording did not) and a
  non-deterministic generator.
- `packages/provide-uterm/tests/terminal/test_control_channel_fuzz_corpus.py`
  replays all 523 cases against the live CPython reference, in the normal pytest
  run.
- Each port adds its own replay test. That is the point of the file.

**Exploration** — weekly, `.github/workflows/fuzz-explore.yml`:

Runs `conformance/fuzz/explore_control_channel_fuzz.py` with a **fresh random
seed** and checks the reference against itself — round-trips, the predicate/decoder
agreement, that arbitrary input only ever raises `ControlFrameProtocolError`, and
that a chunked feed and a single feed agree on the *logical* stream even when they
disagree on event boundaries. On failure it prints the seed and the base64 of every
chunk it fed, in feed order, so the case can be pasted straight into
`_REGRESSIONS`. The shape (the chunks shown are `CCF-REG-0004`'s; the message is
illustrative):

```
DIVERGENCE  seed=1731  iteration=8  check=check_chunked_matches_single
  chunked error 'invalid control json' != single error None
  offending chunks (base64 of UTF-8, in feed order):
    [0] YWIQAjAwMDAwMDBjOnsiayI=
    [1] OjF9eHh4eHg=
```

It is separate from the deterministic replay on purpose. The committed corpus must
never change under a port's feet; exploration is allowed to find something new, and
what it finds becomes a permanent case.
