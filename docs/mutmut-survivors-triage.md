# Mutmut survivor triage

> **Status (2026-06-03):** waves 1–6 below are a HISTORICAL log of *absolute*-score
> reduction on the **original core perimeter** (2026-05-19 … 2026-05-28). They are
> **superseded** by the server-perimeter enablement that followed — see **Wave 7**
> at the bottom. The absolute counts in the mid-document snapshot predate that work
> (the perimeter has since roughly tripled: `process_impl.py` alone is 692 mutants).
> The live source of truth is the `[tool.mutmut]` comments in `pyproject.toml`, the
> documented-equivalent allowlist `mutation_equivalents.toml`, and `MUTATION_PATTERNS.md`.

**Snapshot:** 2026-05-23 after the detector long-tail sweep.

Six attack waves landed (the first five are listed here; wave 6 is appended below):

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

   Root cause: `[tool.mutmut].source_paths` in `pyproject.toml`
   currently lists only `src/provide/uterm/{pty/connector,control_channel,
   control_channel_builders,control_channel_patterns,auth,detection/
   detector,detection/engine,io,recording}.py`. None of the files
   touched in this wave are in that list — `bridge/contracts.py`,
   `bridge/frames.py`, `bridge/hub/connections.py`, `bridge/models.py`,
   `bridge/routes/websockets.py`, the connectors, the CF tunnel API,
   and the CF DO are all uncovered by the mutmut gate.

   **Status (2026-05-23):** `bridge/contracts.py` added to
   `source_paths`. The negotiate_protocol_version boundary is now
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

   **RESOLVED (2026-05-28).** The earlier diagnosis above was only
   partly right. The actual root cause of the server-package
   `no_tests` is a **mutant-name vs `__module__` mismatch**, plus a
   stats-phase `-x` abort:

   1. *Name mismatch.* mutmut derives a mutant's name from the
      `source_paths` path: `str(path).replace(os.sep, ".")` after
      stripping a leading `src.` (`get_mutant_name` in
      `mutmut/__main__.py`). The coverage trampoline keys on the
      *imported* module's `__module__`. A `packages/...` path yields
      `packages.provide-uterm-server.src.provide.uterm.server.bridge.hub.limiter`,
      which never equals the import name
      `provide.uterm.server.bridge.hub.limiter` — so the recorded
      coverage key and the mutant key never join, and *every* server
      mutant reports `no_tests`. (Import resolution to the mutated copy
      was actually fine — verified by probing `lim.__file__` under the
      stats env.)
   2. *Stats-phase abort.* mutmut's stats phase runs the whole
      pytest selection under `-x`. `tests/server/test_config.py::
      test_loaded_max_sessions_is_enforced_by_app` (and other
      `TestClient` suites) need the autouse auth fixtures in the server
      test package's `conftest_part1.py`. That conftest was never
      copied into `mutants/`, so the `TestClient` sent no
      `X-Principal/X-Role` header in `header` auth mode → 401 ≠ 409 →
      the test failed → `-x` aborted stats binding before reaching the
      later suites.

   The fix (all in harness/config scope):

   - **`src/provide/uterm` is now a real directory of symlinks** (built
     by `scripts/build_mutation_src_tree.py`): the core package's
     children plus one symlink per cross-package namespace — `server`,
     `tunnel` (provide-uterm-server) and `pty`, `manager`
     (provide-uterm-platform). All server/platform `source_paths`
     entries were switched from `packages/.../src/...` to
     `src/provide/uterm/...`, so the derived name now equals
     `__module__`. `_resolve_to_mutmut_path` in
     `scripts/run_mutation_gate.py` already maps the changed real file
     back to the symlinked entry by inode, so `--changed-only` keeps
     working.
   - **The server test package's `conftest.py` + split parts are added
     to `also_copy`** (after the directory entries so the parent dir
     exists). `pytest_configure` in `conftest_part2.py` is guarded to
     no-op under `MUTANT_UNDER_TEST` (it eagerly built a server app,
     which instantiates the mutated `RateLimiter` and tripped the
     forced-fail trampoline during the stats phase).
   - **`--import-mode=importlib`** is added to the mutmut
     `pytest_add_cli_args`: the core and server test packages both
     expose a `tests.conftest` dotted name; prepend import-mode rejects
     the second ("Plugin already registered under a different name").
     The server `tests/__init__.py` is deliberately *not* copied so its
     conftest is keyed by file path under importlib.

   Result: `bridge/hub/limiter.py` went 36 `no_tests` → 36 `killed`
   (100%). The whole server perimeter now binds (e.g. token_hash +
   models + intercept = 119 killed where they were 0/`no_tests`
   before). Files whose unit suites are not yet enumerated in
   pytest selection (the other hub services — lease/router/registry/
   connection/presence/store/polling — and a few token_hash/intercept
   survivors) still report `no_tests`/`survived`; that is a *test
   completeness* gap, not a binding-mechanism gap, and is tracked
   separately. The `--changed-only` CI gate only fires on the perimeter
   files a PR actually touches, so it stays green for unrelated PRs.

   > **Update (2026-06-03):** this "test completeness gap" is closed. The
   > hub services and the rest of the deferred server perimeter were
   > subsequently wired + killed to 100% — `limiter`, `lease`, `connection`,
   > and `registry` are all proven `killed==100`; `router`/`presence`/
   > `store`/`polling_service` are enumerated with their kill-suites wired.
   > See Wave 7.
5. **Detector long-tail sweep (2026-05-23).** Targeted the ~130
   pre-existing detector.py survivors carried over since wave 1.
   Result: **90 killed**, dropping detector survivors 133 → 43 and the
   absolute gate from 211 → 121 (kill rate 73.18% → 78.35%).

   Kills landed via
   `packages/provide-uterm/tests/detection/test_detector_survivors.py`
   across these groups (numbers are mutmut ids on detector.py):

   * **compile_failures shape (~12 kills).** `regex`/`error`/`id` dict
     keys + get-call defaults in the `failed_patterns` entries that
     `PromptDetector.compile_failures` exposes. Killed via direct
     assertions on the returned tuple.
   * **strict-mode error summary (~6 kills).** The
     `DetectorPatternCompileError` summary string format and separator.
     Mutants `145/148/150/152/153` killed via exact exception-text
     assertions.
   * **Compile-time logger arg defaults (~14 kills).** The
     `pattern.get("id", "unknown")` default on the success-path debug
     log, failed-path error log, and KeyError-path log. Plus the
     `str(e) → None / str(None)` 4th-arg mutations. Killed by feeding
     patterns missing the `id` key and asserting on
     `pattern_id=unknown` / `missing_key=` in caplog.
   * **prompt_fingerprint observability (~9 kills).** `cursor` x/y
     defaults with non-`or 0`-collapsing values, trailing flag default,
     and the `tail_lines` kwarg pass-through. Killed via direct format
     inspection of the returned `"{hash}:{cae}:{trail}:{cx}:{cy}"`
     string.
   * **_detect_in_text cursor-miss PromptMatch defaults (~9 kills).**
     `pattern.get("input_type", "multi_key")` and
     `pattern.get("eol_pattern", r"[\r\n]+")` defaults flow into the
     fallback match. Killed by exercising the cursor-miss-fallback
     path with patterns that both supply explicit values AND omit them.
   * **detect_prompt_with_diagnostics snapshot/log payload (~10 kills).**
     `screen` non-empty fallback (`"XXXX"` mutants 9/10), `cursor_at_end`
     default (13/15/18), `has_trailing_space=True` default (26), region/no-match
     log args (46/47/85/96/112), kwarg-passthrough `regex_matched_but_failed`
     in success + fallback returns (76/93), and dict-key renames in the
     failures-log payload (102/103/106/107).
   * **_run_two_pass_detection match-success log args (~3 kills).** The
     matched-region (19) and matched-full (40/41) log arg substitutions.
   * **reload_patterns + add_pattern legacy attr (~5 kills).** The
     `expect_cursor_at_end` filter key/defaults (6/7/9/11/12) and the
     `self._compiled = self._compiled_all` legacy-attr restoration (13
     for both methods). Killed via direct attribute introspection on
     `_compiled_no_cursor_end_req` and `_compiled`.

   **The remaining 43 detector survivors are all EQUIV** per the
   categories enumerated in the EQUIV section below. Specifically:
   - 1 trampoline-default-arg (`strict=False → True`)
   - 17 XX-wrapped / case-folded log message strings
   - 6 `failed_patterns` regex / log-regex defaults unreachable on the
     re.error branch (key always present)
   - 7 `p.get("error", ...)` defaults unreachable in failure log and
     strict-summary (key always present in entries)
   - 8 `prompt_fingerprint` mutations equivalent under `or 0` /
     codec normalization
   - 2 `_run_two_pass_detection` region-pass `cursor_miss_candidates`
     kwarg unobservable (compiled_fast filter excludes the only
     patterns that would append to the list)
   - 2 `detect_prompt_with_diagnostics` `screen` `or ""` collapse
   - 2 `detect_prompt_with_diagnostics` `has_trailing_space` None →
     `bool(None)` is False, same as default False
   - 1 `detect_prompt_with_diagnostics` `bool(None)` unreachable
     in natural data flow (candidate list non-empty implies
     `cursor_at_end=False`)
   - 1 `detect_prompt_with_diagnostics` `match=None` kwarg drop on
     PromptDetectionDiagnostics (match defaults to None on the model)

   None of these are worth killing — they require either testing the
   exact log/error string verbatim (brittle), inventing pattern dicts
   that can't be produced by the real pipeline, or contorting the data
   flow to reach unreachable branches.

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
   + `test_session_logger.py` to the mutmut pytest selection would convert
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

## Absolute-score snapshot (post-wave-5)

> **Superseded (2026-06-03).** These counts are the 2026-05-23 core-perimeter
> picture. The perimeter has since grown by the entire server stack + manager
> (lease, connection, registry, config_schema, webhooks, routes, process_impl,
> …), each enforced at `killed==100` with documented equivalents excused. Do
> not treat the numbers below as current; see Wave 7.

- Total mutants: 1741
- Killed: 1364 (78.35%)
- Survived: 121
- No-tests: 255 (recording / session_logger harness gap — unchanged)
- Timeout: 1
- detector.py specifically: 43 survivors (all EQUIV per the analysis
  above); started at 133.

Remaining non-detector survivors (~78) live in `control_channel`,
`control_channel_builders`, `auth`, `engine`, and `io`. Most are in the
EQUIV / cosmetic-rename family per the bucket counts above; a small
number could still be killed with surgical assertions but are not
high-ROI.

## Wave 6 — recording.py binding + auth.py/recording.py kill sweep (2026-05-28)

**recording.py `no_tests` fix.** recording.py's real test files
(`tests/terminal/test_recording_stores.py` for the In-memory/Null stores,
plus a new `tests/terminal/test_recording_local.py` for
`LocalFileRecordingStore`) were never enumerated in
`[tool.mutmut].pytest_add_cli_args_test_selection`, so the gate bound zero tests and reported all
~249 recording mutants as `no_tests`. Both files are now in pytest selection
(`tests/` is already in `also_copy`). No exit-4/forced-fail recurred — the
server-package harness fixes (canonical `src/` symlink tree, `pytest_configure`
guard under `MUTANT_UNDER_TEST`, `--import-mode=importlib`) carry the
core-package binding too. Result: recording.py **249 `no_tests` → 0**;
255 mutants bound, 245 killed.

**auth.py + recording.py kill sweep.** Combined gate run
(`--changed-only`, both files): 436 mutants, started 24 survived, ended
**23 survived (94.72%)** after surgical kills. Killed in this wave:

* recording.py LocalFile per-session lock keying (`_get_lock(session_id)`
  → `_get_lock(None)`), cached-handle reuse (`f = self._files.get(...)`
  → `None` / `_files[sid] = None`), `log_stop`/`log_start` dict-key shapes,
  `setdefault(sid, [])` default (kills `setdefault(sid, None)`), and the
  In-memory `recording_meta` `len > 0` boundary + `+ 1`-per-event
  size_bytes numeric. ~19 kills.
* auth.py `_parse_options` `value.strip('"')` → `strip('XX"XX')`: killed by
  `test_parse_options_strips_only_double_quotes_not_arbitrary_chars`
  (value `"Xdatax"` must keep its leading/trailing `X`).

**The 23 residual survivors are all EQUIV** and documented here:

recording.py (7):
- `path.open(..., encoding="utf-8")` / `read_text(encoding=...)` codec
  case-folds (`"utf-8"` → `"UTF-8"`, `encoding=None`, dropped kwarg). All
  runtime-equivalent under the test platform's UTF-8 text I/O. The same
  source line also hosts *killable* siblings (`encoding="XXutf-8XX"` →
  LookupError, mode `"a"` → `"A"`/None), so `# pragma: no mutate` is
  disallowed by policy (would mask a killable mutant).

recording.py default-arg (pragma'd, no longer counted): the
`get_entries(..., limit: int = 200, ...)` default on `LocalFileRecordingStore`,
`InMemoryRecordingStore`, and `NullRecordingStore` is trampoline-masked
(the mutmut wrapper passes the original default positionally). The
signature line carries *only* that masked default, so it earns
`# pragma: no mutate — trampoline-masked default`.

auth.py (13):
- codec case-folds: `decode("ascii")`/`encode("ascii")` → `"ASCII"`,
  `read_text(encoding="utf-8")` → `"UTF-8"`/`encoding=None`. Same
  killable-sibling constraint as recording — no pragma.
- `fingerprint_from_openssh_blob` `b64.rstrip("=")` → `rstrip("XX=XX")`:
  *provably* EQUIV. A SHA-256 digest is 32 bytes → 43 base64 chars + one
  `=`; the 43rd char encodes only 2 bits so it is always one of
  `{A,E,I,M,Q,U,Y,c,g,k,o,s,w,0,4,8}` — never `X`. The mutated strip set
  `{X,=}` can therefore never strip a real character.
- `_coerce_to_binary_pubkey` `split(None, 2)` → `split(None)` /
  `split(None, 3)`: only `parts[1]` (the 2nd token) is read, identical
  under any maxsplit ≥ 1.
- `_parse_authorized_keys_line` `options_str = ""` → `None` (both falsy in
  the `if options_str` guard), `rest.lstrip()` → `rstrip()` (the line is
  already `.strip()`'d upstream and `split(None, …)` ignores leading
  whitespace, so parts are identical).
- `_find_first_token_end` / `_split_options` `in_quotes = False` → `None`
  (only ever used as `not in_quotes` / toggled — `None` and `False` are
  both falsy and toggle identically).
- cosmetic `ValueError` message `XX`-wraps (`"malformed OpenSSH public
  key line"`, `"missing key payload"`) — brittle to test verbatim, EQUIV
  per established policy.

These 23 do not fire the `--changed-only` CI gate for unrelated PRs; they
fire only on PRs that touch auth.py/recording.py, where they are the
intentional EQUIV ceiling.

## Wave 7 — full server/manager perimeter enablement (2026-05-28 … 2026-06-03)

The waves above chipped at *absolute* score on the original core perimeter.
Wave 7 was a different goal: take every **deferred** perimeter file to a strict
`killed==100` (every non-equivalent mutant killed) so the `--changed-only` gate
genuinely enforces those files when a PR touches them. The blocker that made
this possible — and the per-file playbook — are recorded in
`MUTATION_PATTERNS.md`; the per-file obstacle notes live in the
`[tool.mutmut]` comments in `pyproject.toml`. Highlights:

- **The mutmut `os.wait()` child-reaping crash was root-caused and fixed.** A
  pytest selection suite that spawned a real `subprocess.Popen` (`test_process.py`)
  leaked worker children into mutmut's fork-loop reaper → `KeyError` → every
  server mutant `not_checked`/score 0. Removing that one suite (it bound
  nothing — it covered only the 0-mutant `manager/process.py` shim) unblocked
  the whole server perimeter.
- **A documented-equivalent allowlist now exists** (`mutation_equivalents.toml`,
  193 entries). Genuinely-equivalent mutants (trampoline-masked default args,
  codec case-folds, `typing.cast` no-ops, dead-initial-value reassignments,
  subclass-redundant `suppress(...)`, …) are subtracted from the `killed==N`
  denominator instead of pinning a file below 100. This is what makes
  `auth.py`-class files enforceable.
- **Files taken to `killed==100`** (dates = enablement commit): `limiter` →
  `lease` (489/489) → `connection` → `config_schema` → `webhooks` (network-
  mocked kill suite) → `registry` (async/SSE bounded by per-step `wait_for`) →
  `routes/` (decorated handlers are mutmut-skipped; the real surface is sync
  helpers) → `manager/process_impl.py` (692 mutants — the biggest file in the
  repo; mocked spawn/kill conftest + zero-delay sleep). `manager/process.py` +
  `config.py` were found to be **0-mutant** (re-export shim + Pydantic model)
  and dropped as non-targets, not deferrals.
- **CI timeout class is handled honestly.** mutmut flags a wall-clock
  `timeout` purely on `(estimated_test_time + 1) × 15`s; on a loaded runner a
  *documented-equivalent* mutant can surface as `timeout` instead of
  `survived`. Since an allowlisted mutant is proven-unkillable, `timeout` is now
  excusable **only** for allowlisted mutants (a non-allowlisted timeout still
  fails). See `EXCUSABLE_STATES` in `scripts/run_mutation_gate.py`.

### Wave 7 tail — `lease.py` two-phase reserve + `models.py` (2026-06-03)

Landing the bridge code-review remediation (`bridge/hub/lease.py`
`try_acquire_rest` now sends the worker-pause frame outside the hub lock via a
`hijack_pending` reservation) re-ran the lease perimeter and added a dedicated
kill suite (`test_lease_kill_acquire_rest_pending.py`) pinning the lock-free
send + reservation rollback paths. Editing `models.py` to add the reservation
field pulled it into the changed-only gate, which surfaced `_safe_int` /
`_safe_float` as latently uncovered ("enumerated ≠ enforced" again) — a focused
`test_models_safe_numeric_kill.py` now covers them. Combined result:
`lease.py` + `models.py` **521/521 killed**. Two `async with` exit-arcs carry
`# pragma: no branch` for the coverage.py-on-3.11 arc-attribution quirk (both
arcs are tested and every mutant on them is killed).

**Net:** the original "test completeness gap" tracked in waves 4–6 is closed.
The deferred-file list in `[tool.mutmut]` is empty — every perimeter entry is
active. The remaining *documented-equivalent* survivors (per
`mutation_equivalents.toml`) are the intentional ceiling, and the
`--changed-only` gate enforces `killed==100` on whatever a PR touches.

### Wave 8 tail — `connector.py` codec-pragma removal (2026-06-06)

`pty/connector.py` had carried `# pragma: no mutate` on its 4 codec/decode/
truncation lines — which violated the "no pragma when the line hosts a killable
sibling" policy above (the `"utf-8"` decoder/encoder and `"replace"` handler each
host killable `"XXutf-8XX"`/`"XXreplaceXX"`→LookupError and `errors=None`→
UnicodeDecodeError siblings). The pragmas were removed; the killable siblings are
now killed by a fork-free suite (`test_connector_mutation_mocked_part2.py`:
invalid-UTF-8→U+FFFD; buffer cap at exactly 32769), and the 3 genuine residuals —
two `utf-8`→`UTF-8` codec case-folds and the `> 32768`→`>=` clamp no-op — are
documented in `mutation_equivalents.toml` with exact mutant IDs. Mutant set + IDs
were enumerated via mutmut's own AST machinery and kills/equivalences confirmed by
edit-test-revert (the connector gate is Linux-only — it stalls under the macOS
fork-loop — so CI is authoritative).

## Wave 9 — `routes/` de-decoration regression (2026-07-23 … closed 2026-08-10)

`routes/` reached `killed==100` on 2026-06-02 over 459 mutants and the perimeter
entry in `[tool.mutmut]` justified re-enabling it with "routes/ has NO async-hang
surface at all". That claim was load-bearing and is no longer true.

**What broke.** `9bc4dd0c` (2026-07-23, "bind FastAPI session routes from RouteDefs")
moved the session handlers out of `@router.*` decorators into undecorated
`*_capability_handlers` factories. mutmut skips *decorated* functions only, so
de-decorating took the handler bodies from skipped to mutable and put ~2600 mutants
live in one commit. Line coverage was, and stayed, 100% — the tests execute the
handlers without asserting enough to kill their mutants, so nothing else flagged it.
`mutation-full` caught it on 2026-08-02, but that job had already been red for nine
straight weeks, so a newly-red file was indistinguishable from the standing failure
and went unread for five days. `9ce12f09` added the tracking-issue reporter for
exactly this reason.

**The revived caveat.** The 2026-06-01 audit deferred `routes/` for "async
timeout/segfault by pattern". The 2026-06-02 re-enablement overrode that on the
grounds that every async handler — including `sse.py`'s `StreamingResponse`
`stream_events` — was decorated and therefore skipped. Those handlers are mutated
now, so the deferral is live again in principle. In practice it has not reproduced:
`sse.py` was driven back to 100% with its async handler mutable and did not hang.
The unproven surface is `sessions.py`'s async handler bodies.

**Coverage split.** Every suite builds routers with mocked app-state and calls
endpoints with a mocked `Request` (no TestClient / full-app lifespan), and all are
wired into `pytest_add_cli_args_test_selection` — without that, mutmut runs none of
them in the `mutants/` tree.

- `test_routes_mutation_killing.py` — the decorated-era surface: `_helpers.py`
  accessors, per-module `_registry`/`_authz`/`_principal` accessors, `create_*_router`
  bodies, and nested undecorated helpers such as health's
  `_posture_caller_is_privileged`. 390 killed + 69 documented equivalents (64
  `typing.cast` no-ops + 5 `pages` default-value no-ops).
- `test_routes_capability_mutation_killing.py` — the de-decorated factories.
  `sse.py` 65.38% → 100%; `sessions.py` ~5% → 46.12% (493/1069), table-driven with a
  completeness check so a new handler cannot join the factory unmeasured. Its 8
  equivalents are `typing.cast` no-ops and Starlette header-*name* case folds
  (Starlette lowercases header names into `raw_headers`, so name-case mutants emit
  identical bytes; header *values* stay case-sensitive and are killed).

- `test_routes_tunnels_{create,connect,tokens}_mutation_killing.py` (`1b03ea14`) —
  `tunnels.py` 4.98% → 100%. Split three ways for the 777-line cap; the perimeter
  note in `[tool.mutmut]` records why each slice exists.
- `test_routes_pam_events_mutation_killing.py` (`4bcd9cab`) — `pam_events.py`
  6.91% → 100%.

**Closed 2026-08-10.** Every `routes/` file is back at `killed==100`, over thirteen
suites. The order they fell, and what each cost:

| Closed | Files | Commit |
|---|---|---|
| 08-07 | `sse.py` 65.38% → 100; `sessions.py` ~5% → 46.12% | `4355d736` |
| 08-09 | `tunnels.py` 4.98% → 100 (split three ways for the 777-LOC cap) | `1b03ea14` |
| 08-09 | `pam_events.py` 6.91% → 100 | `4bcd9cab` |
| 08-10 | `sessions.py` 46.12% → 100 | `6d7edc8b` |
| 08-10 | `route_defs.py` 36.54% → 100; `profiles.py` 88.26% → 100; `webhooks.py` 89.32% → 100 | `d2004938` |

`route_defs.py` was the lowest-scoring file in the whole perimeter and is the one
worth remembering: nothing tested it directly. Every RouteDef family exercised the
binder and its guard, but only implicitly — and implicit coverage executes code
without discriminating anything. It is also where a mutation does the most damage,
since `_route_guard` is the 422 path-grammar check and the 403 role gate for the
entire shared API at once. Two of its mutants resisted the obvious test: binding
the guard with a null authorizer leaves the dependency attached so the route still
*looks* guarded from outside, and the 405 catch-all loop iterates a **set**, so
`continue` vs `break` is observable only for a template pair whose real iteration
order exposes it — a fixture assuming source order kills it by luck and flakes when
the hash seed changes.

### What this wave should change about how the perimeter is read

Reaching 100% is not a terminal state. Two files drifted off it here from ordinary
refactors, and neither announced itself:

- `routes/` — `9bc4dd0c` de-decorated the handlers. Line coverage never moved.
- `lease.py` — `61647de9`'s lifecycle-fencing rework took it to 77.71%; `06d2ef96`
  restored it. Most of its damage was 138 mutants in mutmut's "no tests" state:
  helpers that arrived with full line coverage and no bound test, so they never ran
  yet still counted against the score.

Both are invisible to coverage and visible only to the mutation gate — which had
itself been red for nine straight weeks when the `routes/` regression landed, so a
newly-red file was indistinguishable from the standing failure and went unread for
five days. `9ce12f09` (later `c59cd4ac`) exists because of that: a red
`mutation-full` run now names the failing paths rather than being one more red run.
Do not read a red mutation job as the standing failure.

## Wave 10 — the perimeter lists a shim where the router lives (2026-09-02)

**Not yet worked. Measured and recorded so the number is not guessed at.**

`source_paths` lists `bridge/hub/router.py`. That file is eleven lines:

```python
from provide.uterm.server.bridge.hub.router_impl import MessageRouter

__all__ = ["MessageRouter"]
```

A re-export shim — the same shape this document and CLAUDE.md exclude
elsewhere as a 0-mutant non-target (`manager/process.py`). The router was split
for the 777-LOC limit and the documented rule for that ("the extracted sibling
module is added to `source_paths` so its mutants stay enforced") was not
applied, so the router service's real code has never been mutation-tested:

| file | lines | in perimeter |
|---|---|---|
| `router.py` | 11 | yes — and it is the shim |
| `router_impl.py` | 613 | no |
| `router_broadcast.py` | 583 | no |
| `router_behavioral.py` | 118 | no |
| `router_redaction.py` | 88 | no |

`approvals.py` (192 lines) is the `approval_store` service and is not listed
either. So two of the nine refactor-#16 hub services that CLAUDE.md describes
as being on the perimeter are, in practice, unenforced.

### Measured cost

One file, measured rather than estimated — `router_broadcast.py` added to
`source_paths` and run through the gate, then reverted:

```
mutation gate failed: score=19.14 min_required=100.00
stats={"total": 491, "killed": 94, "survived": 348, "not_checked": 0, "timeout": 0}
```

**348 survivors in one of the four unlisted router files**, behind tests whose
line coverage is 100% — the same mechanism as Wave 9, where ~2600 mutants went
live behind fully-covered tests. Extrapolating the remaining 1011 lines of
router plus `approvals.py`, this is a Wave-sized program, not an edit.

### Why it is filed rather than fixed

Adding paths to `source_paths` is a mutation support-file change, which forces
a full-perimeter run by design. Doing that before the survivors are killed puts
the gate red and keeps it red for the duration — the exact state the 2026-08-12
cron decision exists to avoid. The order has to be: kill first on a temporary
widening, add the path last.

### How it was found

A `presence_sync` fix touched `router_broadcast.py` and the changed-only gate
selected nothing, which is what prompted checking whether the file was on the
perimeter at all. Worth generalising: **a perimeter entry that is a shim is
indistinguishable from coverage in the path list.** Anything checking the list
should compare each entry against where the code actually lives.

### Resolution (2026-09-02)

All five files are closed and on the perimeter at `killed==100`, each landed as
its own green push rather than as one long red wave:

| file | mutants | survived cold | equivalents | state |
|---|---|---|---|---|
| `router_redaction.py` | 106 | 15 | 0 | on the perimeter |
| `router_behavioral.py` | 112 | 27 | 0 | on the perimeter |
| `approvals.py` | 105 | 16 | 0 | on the perimeter |
| `router_broadcast.py` | 507 | 211 | 10 | on the perimeter |
| `router_impl.py` | 490 | 237 (+73 `no tests`) | 2 | on the perimeter |

**"Filed rather than fixed" was the wrong call, and the reason is worth
recording.** The premise above — that adding a path forces a red full-perimeter
run for the duration of the wave — is not what the gate does. A support-file
change prints `FULL_PERIMETER_REQUIRED_MARKER` and *returns 0*, dispatching the
advisory `mutation-full.yml` separately. The unit of work is therefore one
file, not one wave: measure it, kill its survivors, add its `source_paths`
entry, push green. Nothing is ever red in between.

**The 348/491 figure above was measured against the wrong test selection.** A
cold re-measure of the same file gives 211 survivors of 507. Two distinct ways
to get this wrong, both of which produce plausible-looking survivor counts:

- `--paths` expects the `src/...` form that `source_paths` uses. Passing the
  repo-relative `packages/...` path skips `scoped_test_selection` (it tests
  `set(paths) <= BRIDGE_HUB_SOURCE_PATHS`) and silently falls back to the broad
  `pyproject.toml` selection. That run reported 165 survivors where the scoped
  selection reports 21 — the number CI would actually enforce.
- A kill-suite added only to root `pyproject.toml` is *not* in the scoped
  selection. `BRIDGE_HUB_MUTATION_TESTS` in `scripts/mutation_gate_config.py`
  is the list a changed-only run consults; a suite missing from it contributes
  nothing and its mutants return as phantom survivors.

Always read the collected-item count: 556 items is the scoped selection, ~4300
is the fallback.

### What the 211 survivors in `router_broadcast.py` were

Not scattered — four clusters, each invisible to the shape of test that already
existed:

- **The whole failure path** (~60). A failed browser send is not an error any
  caller sees: `broadcast` returns `None` either way. Every log line, counter,
  socket removal and hijack-state republish is a side effect, and none was
  asserted. Compounded by telemetry filtering below INFO, so a `caplog`
  assertion on the DEBUG line passes whether or not the line is emitted — these
  are asserted against a `MagicMock` logger instead.
- **`broadcast_hijack_state`'s second pass** (51). Reached only when a send
  fails, which no test did. Five state re-reads and eight arguments, all
  unasserted. Note the trap in pinning it: the obvious scenario is the dead
  socket *being* the hijack owner, which produces a "nobody is driving" frame —
  exactly what reading any of those five as `None` also produces. Eleven
  mutants survived the first kill-suite for that reason; killing them needs a
  lease that **outlives** the removal.
- **The 0/1-vs-many split** (~10). `<= 1` sends sequentially, above it fans out
  through `gather(return_exceptions=True)`. The only observable difference is
  `BaseException` handling — the sequential arm catches `Exception` and lets a
  cancellation propagate, `gather` captures it as a value — so the boundary is
  pinned with a custom `BaseException` subclass (not `KeyboardInterrupt`, which
  pytest treats as a request to abandon the session).
- **The startup buffer's per-browser `continue`s** (~10). Three loop skips that
  each hand control to the next browser; as `break` they abandon everything
  behind the first. Indistinguishable from correct with one browser in the
  session, which is how every existing test drove it.

The 10 documented equivalents are `zip(..., strict=True)` where both sequences
are built from each other (×3), `cast()`'s type argument, which is a runtime
no-op (×4), an unused `router` parameter kept for the module's uniform
signature convention (×2), and a `worker_id` that is unreachable because the
call site sets `suppress_errors=True` (×1).

### `router_impl.py`, and the state that is not "survived"

The last of the five measured 490 mutants and killed 139 cold. The interesting
part is not the 237 survivors but the **73 reported as `no tests`** — mutants
mutmut generated and then never ran, because no test in the selection covered
the function at all:

    try_reclaim_hijack        try_reclaim_hijack_status    set_browser_role
    get_worker_browser_role   send_hijack_state_to         keystroke_timestamps

`no tests` is not in `BAD_MUTANT_STATES`, so the gate's `bad_total` ignores it
and the surviving-mutant list does not print it — but `score = killed / total`
counts it in the denominator. A file can therefore sit at 79% with an
actionable-looking list of 26 survivors while 73 mutants are missing from that
list entirely. **Read the state histogram, not just the survivor list**, and
when the two do not reconcile, `mutants/mutmut-stats.json` →
`tests_by_mangled_function_name` names every function and the tests that cover
it; a function absent from that map has none.

`try_reclaim_hijack_status` being in that set is the one worth noting on its
own: it is the only place a browser takes the terminal for itself, it decides
under a fence with five conjuncts, and it had no test.

### The rule is now enforced, and it found a second instance

`tests/scripts/test_mutation_perimeter_shims.py` parses every `source_paths`
entry, identifies the ones whose body is nothing but imports and `__all__`, and
requires the modules they re-export from to be on the perimeter too. It carries
a negative control that reconstructs the exact `router.py`-without-`router_impl.py`
configuration, so the guard cannot quietly stop detecting shims and still pass.

On the commit that introduced it, it failed — on a module nobody was looking
at. `server/app/factory.py` is eleven lines re-exporting `create_server_app`
from `factory_impl.py`; the shim is on the perimeter and the 610 lines are not.
Same cause as the router: split for the 777-LOC limit, documented rule not
applied. It is recorded as a named exemption in that test with the reasoning
inline, because closing it means driving `factory_impl.py` to `killed==100`
first — adding the path before that only turns the advisory full-perimeter run
red, which is the mistake the original Wave 10 entry made in the other
direction.
