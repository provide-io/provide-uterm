# Mutmut survivor triage

**Snapshot:** 2026-05-23 after the protocol-handshake wave.

Four attack waves landed:

1. **Surgical-killer pass (2026-05-19).** Killed 161 mutants, dropping
   survivors from ~408 to 247. Kill rate 60.69% → 70.19%.
2. **High-ROI batch (2026-05-19, evening).** Targeted the 20 highest-ROI
   survivors identified by this document via
   `packages/provide-uterm/tests/test_high_roi_mutmut.py`. Killed 11/20.
   Kill rate 70.19% → 71.25%.
3. **Comprehensive-review hardening (2026-05-22).** Sweep against the
   review diff (`run_mutation_gate.py --changed-only --base-ref c2229bf`)
   surfaced 137 survivors + 197 no_tests across 16 changed files.
   Triage:
   * 1 substantive new survivor — `_check_json_depth` list-branch
     `depth + 1 → depth + 2` — killed by
     `test_decoder_list_depth_increment_is_one_not_two`.
   * 3 control_channel `_parse_frame_payload` survivors fall in the
     "internal log-format string renames" EQUIV category (existing
     policy: cosmetic, do not test).
   * 1 detector `__init__` `strict: bool = False → True` survivor is
     trampoline-masked (existing EQUIV category — outer wrapper
     captures the original default).
   * 130 detector survivors are pre-existing whole-file mutants
     unrelated to the strict-mode addition (`_detect_in_text`,
     `prompt_fingerprint`, `reload_patterns`, `_run_two_pass_detection`,
     `_compile_patterns` log/dict-key mutants). Carried over from the
     previous snapshot; tracked in the EQUIV / UNKNOWN buckets below.
4. **Protocol handshake + CF token hashing (2026-05-23).** Sweep against
   the diff `d58e62e..HEAD` (CF parity `89f3cd8` + handshake `91bdda7`).
   Result: 40 mutants found but **0 killed / 0 survived / 0 no_tests** —
   the gate's test discovery didn't bind to any covering test.

   Root cause: `[tool.mutmut].paths_to_mutate` in `pyproject.toml`
   currently lists only `src/provide/uterm/{pty/connector,control_channel,
   control_channel_builders,control_channel_patterns,auth,detection/
   detector,detection/engine,io,recording}.py`. None of the files
   touched in this wave are in that list — `bridge/contracts.py`,
   `bridge/frames.py`, `bridge/hub/connections.py`, `bridge/models.py`,
   `bridge/routes/websockets.py`, the connectors, the CF tunnel API,
   and the CF DO are all uncovered by the mutmut gate.

   **Status (2026-05-23):** `bridge/contracts.py` added to
   `paths_to_mutate`. The negotiate_protocol_version boundary is now
   covered by `test_protocol_negotiation.py` and verified to kill all
   13 mutants the gate produced. The other three recommended
   additions (server-side bridge/models, tunnel/token_hash,
   tunnel/intercept) were attempted but **blocked by a cross-package
   namespace collision**:

   - mutmut runs tests with `mutants/packages/<pkg>/src` prepended to
     PYTHONPATH, expecting them to win against the uv-installed
     editable links.
   - But `provide.uterm` is a namespace package across multiple
     workspace members. uv's editable install registers each
     package's src tree as a real path, and Python's namespace-package
     resolution merges them — the installed paths can resolve before
     the mutants/ paths even with PYTHONPATH prepended.
   - Result: server tests imported `provide.uterm.server.bridge.models`
     from the editable install (un-mutated), not from
     `mutants/packages/provide-uterm-server/src/...`. mutmut's
     coverage map registered 0 of the mutated lines as test-covered;
     all 624 mutations reported `no_tests`.

   Server-side mutation coverage would need either (a) a `src/`-style
   symlink trick per package to give mutmut a single canonical import
   path, or (b) a script-side mechanism to point Python at the mutants/
   tree exclusively (e.g. via a per-test virtualenv that doesn't have
   the editable installs). Both are non-trivial.

The bucket counts and category analysis below were computed against the
247-survivor snapshot from after step 1. Step 2 removed ~11 from the `TEST`
bucket (mostly boundary + numeric + b64 + slice categories). The
*structural* picture — what is and isn't killable — is unchanged, so the
recommended attack order and EQUIV analysis still apply.

The numbers below are from `/tmp/triage2.py` (in-repo script ad-hoc) run
against the post-step-1 mutmut state.

## Bucket counts

| Bucket | Count | Means |
|---|---|---|
| `TEST` | 125 | Testable behavior — a surgical assertion would kill it. ROI varies. |
| `UNKNOWN` | 82 | Pattern wasn't recognized by the classifier — needs human review. Most are dict-key / `pattern.get()` defaults whose observability depends on whether the dict key is asserted on. |
| `EQUIV` | 40 | Structurally unkillable — mutmut limitation. |
| `REMOVE` | 0 | None flagged automatically. The `UNKNOWN` bucket contains some real candidates (see below). |

## EQUIV — accept and document

40 mutants that no test can ever kill. **Do nothing**; the survivors are
mutmut artefacts:

- **14 trampoline-masked default args.** Mutating a default value in a
  `def x_make_identity__mutmut_N` mutant has no effect because the
  outer wrapper captures the *original* defaults and passes them
  positionally to the trampoline. Examples: `make_identity` defaults
  for `fingerprint`, `transport`; `__init__` numeric defaults like
  `max_control_payload_bytes = 1_048_576 → 1048577`. The wrapper
  pattern is in `mutants/src/.../*_builders.py`.
- **14 cosmetic error-message renames.** `raise ValueError("missing
  key payload")` → `raise ValueError("XXmissing key payloadXX")`.
  These would only be killable by tests that grep the exact message
  string — brittle and undesirable.
- **4 internal-state reset values.** `self._buffer = ""` → `self._buffer = None`. The reset value is never read externally
  (the next operation rebuilds the buffer), so nothing observes the
  difference.
- **4 codec-name case folds.** `decode("utf-8")` → `decode("UTF-8")`.
  Python's codec registry normalizes case; runtime-equivalent.
- **4 dict-key renames in internal diagnostic dicts** — the dicts are
  built and consumed inside one function (e.g. `failed_patterns`
  inside `_compile_patterns`); nothing outside reads
  `entry["regex"]` so renaming the key is invisible.

**Recommendation:** none. Document them as known mutmut artefacts.

## TEST — real testable gaps

125 mutants ranked by category:

| Category | Count | Strategy |
|---|---|---|
| Truthiness substitution (`True/False/None`) | 62 | Tests should assert on the specific branch the boolean drives. |
| Logger arg mutation | 23 | `caplog` assertions on the formatted message; tightens log contract. |
| Numeric literal change | 22 | Test the off-by-one edge — input exactly at the boundary. |
| Comparison boundary flip (`<` ↔ `<=`) | 6 | Boundary tests (input = N exactly). |
| `str.split` limit | 5 | Input with >`limit+1` whitespace-separated tokens. |
| `kwarg=None` substitution | 3 | Mock the called function and inspect kwarg value. |
| `b64decode validate=True` drop | 3 | Feed invalid-padding base64 and expect rejection. |
| `lstrip` vs `rstrip` | 1 | Input with whitespace at one end only. |

**ROI ranking:**

1. **High** — comparison-boundary + numeric-literal + b64decode-validate + str.split limit + lstrip/rstrip + and/or flip. These are concrete bugs waiting to happen if the test gap stays open. ~40 mutants. Worth surgical tests.
2. **Medium** — truthiness substitutions in production code paths. ~30 of the 62 land in `wait_for_prompt`, `_drain`, `process_screen` — paths with real downstream consequences. The other ~30 are in fallback branches.
3. **Low** — logger arg mutations. Worth doing for the high-fanout log messages but quickly becomes a maintenance tax on tests for low-value strings.

## UNKNOWN — human review needed

82 mutants the heuristic couldn't classify. Spot-check shows them clustering as:

- **`pattern.get("key", default)` mutations** (~50): the `default` value
  is mutated. Killable only if the test uses a pattern dict missing
  `key`. Most of these are *theoretically* killable but the existing
  test fixtures all supply complete pattern dicts.
- **Internal log-format string renames** (`"prompt_detection_..." → "XXprompt_detection_...XX"`) (~15): same category as
  cosmetic error renames above — could be tested but brittle.
- **`screen[-200:]` → `screen[+200:]`** (~5): negative-vs-positive
  slice — testable with input shorter/longer than the slice length.
  Real off-by-one.
- **`continue` → `break`** (2 in `_detect_in_text`): control-flow
  change. Real semantic difference; killable with a test that has
  multiple matching patterns and verifies all are inspected.
- **Trailing kwarg drops** (`f(a, b=val)` → `f(a, )`): the dropped
  kwarg's behavior at default matters. Most flag a `REMOVE`
  opportunity — the default arg is always being supplied, so the
  default is dead. Worth a one-line cleanup PR each.

**Recommendation:** spend an hour reviewing the 82 UNKNOWNs. Expected outcome:
~30 move to `TEST` (real boundary/control-flow bugs), ~10 to
`REMOVE` (genuine dead default), ~40 to `EQUIV`.

## Recommended attack order

If you want to push absolute score higher in a future session:

1. **`comparison-boundary + numeric-literal + b64decode-validate + str.split limit + and/or` (~40 mutants).** Highest ROI — every kill represents a real off-by-one or input-validation gap. Budget: ~2 hours, expect ~30+ kills.

2. **Review UNKNOWNs for `REMOVE` candidates.** ~10 of these are
   probably "default is always supplied" cases that can be removed
   from the source rather than tested. Net win: smaller surface +
   fewer mutants.

3. **`process_screen` / `wait_for_prompt` truthiness substitutions
   (~30 mutants).** These live in the hot paths and represent the
   most operationally important behavior. Budget: ~2 hours of
   careful async-aware test writing.

4. **Recording-tests harness fix.** Adding `test_recording_stores.py`
   + `test_session_logger.py` to `mutmut.tests_dir` would convert
   most of the 255 `no_tests` to killed, but the addition currently
   trips the forced-fail step with exit code 4 — appears related to
   pytest's coverage gate. Fixing the harness unlocks the single
   biggest absolute score jump available.

5. **Stop.** The remaining EQUIV + cosmetic survivors are not worth
   the engineering cost. Document the 70-75% absolute as the
   intentional ceiling.

## CI gate posture (unchanged)

The CI mutmut gate runs `--changed-only` and stays at 100% as long as no
PR adds a new mutant the PR's own tests don't kill. This document is
about the *absolute* score, which is informational only.
