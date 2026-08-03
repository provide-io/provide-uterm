# CM-10: Warning-Free C# Release Build

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 16 C# Release-build warnings and set
`TreatWarningsAsErrors=true`, so a new warning fails the build instead of
scrolling past.

**Architecture:** Fix by warning code, each group being one kind of defect, then
flip the flag. Flipping first would just make the build red for the duration.

**Tech Stack:** .NET 10, Roslyn analyzers, xUnit.

## Global Constraints

- **No global suppression.** The quality-evidence design says so directly:
  "Global warning suppression is not used." No `NoWarn` entry in
  `Directory.Build.props`, no `#pragma warning disable` at file scope.
- Platform-specific findings get a narrow, justified guard — `OperatingSystem.IsLinux()`,
  `[SupportedOSPlatform]` — not a suppression.
- Behavior does not change. Every fix here is a nullability annotation, a
  removed dead field, a widened cast, or a platform guard.
- `make quality-gate` must pass, coverage floor and Stryker mutation gate
  included.
- **Land this before CM-03.** That plan adds a large amount of control-plane
  code, and adding it to a build that tolerates warnings means new nullable
  warnings arrive unnoticed.

## Context

Measured 2026-08-03. `packages/provide-uterm-csharp/Directory.Build.props:10`:

```xml
<TreatWarningsAsErrors>false</TreatWarningsAsErrors>
```

`dotnet build -c Release` produces 16 warnings:

| Code | Count | What it means |
|---|---|---|
| `CA1416` | 6 | platform compatibility — API called without a platform guard |
| `CS8603` | 2 | possible null reference return |
| `CS8600` | 2 | converting null literal or possible null to non-nullable |
| `CS0675` | 2 | bitwise-or on a sign-extended operand |
| `CS0649` | 2 | field never assigned, always default |
| `CS0414` | 2 | field assigned but never used |

This maps onto the design's list exactly — "Signed bitwise operations, nullable
API-key and metrics flows, dead fields, and platform analyzer findings" — which
suggests the review that produced the design ran this same build.

`CS0675` is the one worth reading carefully. Bitwise-or on a sign-extended
operand produces wrong results for negative inputs, and it is a real defect
rather than a style complaint.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-10.

---

### Task 1: Signed bitwise operations (CS0675 ×2)

**Files:**
- Modify: files identified in Step 1.

- [ ] **Step 1: Locate them**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep "CS0675"
```

- [ ] **Step 2: Read each site and determine whether it is a real bug**

`CS0675` fires when a signed operand is sign-extended before a bitwise-or, so
`someInt | (someByte << 8)` produces `0xFFFFxxxx` rather than `0x0000xxxx` when
`someInt` is negative.

For each site, work out whether a negative value can actually reach it. If one
can, this is a live defect and the fix needs a regression test showing the wrong
value before and the right value after. If none can, the fix is an explicit
unsigned cast that makes the intent visible.

- [ ] **Step 3: Write a test if the value can be negative**

If Step 2 found a reachable negative input, write a test asserting the correct
result for it, run it, and confirm it fails before the fix.

- [ ] **Step 4: Fix**

Cast the operand explicitly:

```csharp
var packed = (int)((uint)low | ((uint)high << 8));
```

- [ ] **Step 5: Verify**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -c "CS0675"
dotnet test tests/Provide.Uterm.Tests
```

Expected: `0` warnings, tests pass.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-csharp/src/
git commit -m "fix(csharp): make sign extension explicit in bitwise packing

A signed operand is sign-extended before a bitwise-or, so a negative
input sets the high half rather than leaving it clear. Cast through
unsigned so the width is stated rather than inferred."
```

If Step 2 found the negative input unreachable, say so in the message instead of
implying a bug was fixed.

---

### Task 2: Nullability (CS8603 ×2, CS8600 ×2)

**Files:**
- Modify: files identified in Step 1.

- [ ] **Step 1: Locate them**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -E "CS8603|CS8600"
```

The design names "nullable API-key and metrics flows," so expect them there.

- [ ] **Step 2: Decide per site: is null legitimate?**

Two different fixes:

- Null is a legitimate result → change the return type to `T?` and fix the
  callers. This is the honest fix and usually the right one.
- Null cannot occur → establish that with a guard that throws, not with `!`.

`!` is a suppression wearing an operator's clothing. Use it only where the
invariant is genuinely inexpressible, and comment why.

- [ ] **Step 3: Fix and check the callers**

Changing a return type to `T?` will produce new warnings at call sites. That is
the annotation propagating correctly — fix those too rather than reverting.

- [ ] **Step 4: Verify**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -cE "CS8603|CS8600"
dotnet test tests/Provide.Uterm.Tests
```

Expected: `0`, tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/
git commit -m "fix(csharp): annotate nullable returns in the API-key and metrics flows

Four warnings about returning or assigning a possible null through a
non-nullable type. Where null is a real outcome the signature now says
so and the callers handle it; where it is not, a guard throws rather
than a null-forgiving operator asserting it.

The new call-site warnings are the annotation propagating, not
regressions."
```

---

### Task 3: Dead fields (CS0649 ×2, CS0414 ×2)

**Files:**
- Modify: files identified in Step 1.

- [ ] **Step 1: Locate them**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -E "CS0649|CS0414"
```

- [ ] **Step 2: Determine why each is dead**

Two distinct diagnostics with different implications:

- **CS0649** — never assigned, always default. If code reads it, that code is
  reading a default it did not intend. Check the readers before deleting.
- **CS0414** — assigned, never read. Either the write was meant to matter (and
  something is missing) or the field is genuinely vestigial.

A field that was *meant* to be wired up is a bug, not dead code. Read
`git log -S "<fieldName>"` for each before deciding.

- [ ] **Step 3: Delete or wire up**

Delete the vestigial ones. If one turns out to be an unfinished feature, that is
a finding — record it rather than deleting the evidence.

- [ ] **Step 4: Verify**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -cE "CS0649|CS0414"
dotnet test tests/Provide.Uterm.Tests
```

Expected: `0`, tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/
git commit -m "refactor(csharp): remove vestigial fields

Two never assigned and read as default, two assigned and never read.
Checked git log -S for each first: a field that was meant to be wired up
is an unfinished feature rather than dead code, and deleting it would
delete the evidence."
```

---

### Task 4: Platform compatibility (CA1416 ×6)

**Files:**
- Modify: files identified in Step 1.

- [ ] **Step 1: Locate them**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep "CA1416"
```

Expect Unix-only file-mode APIs — `File.SetUnixFileMode`, `File.GetUnixFileMode`
— called on paths reachable from Windows.

- [ ] **Step 2: Guard, do not suppress**

Two correct forms:

```csharp
if (OperatingSystem.IsWindows())
{
    return OpenAppendWindows(path, mode);
}
// analyzer now knows the rest is non-Windows
```

or, for a method that is genuinely Unix-only:

```csharp
[SupportedOSPlatform("linux")]
[SupportedOSPlatform("macos")]
private static void ApplyUnixMode(string path, UnixFileMode mode) { ... }
```

A `try { ... } catch (PlatformNotSupportedException) { }` is not a guard — it
silently does nothing on the platform where the guarantee was needed, which is
worse than failing.

Note: `FileIo.cs` already uses the catch pattern at
`EnsureOwnerOnlyDir`. CM-02 restructures that file, so coordinate — if CM-02 has
landed, the Windows path is already split out and these warnings may resolve
with it.

- [ ] **Step 3: Verify**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -c "CA1416"
dotnet test tests/Provide.Uterm.Tests
```

Expected: `0`, tests pass.

- [ ] **Step 4: Verify on Windows**

These are platform warnings, so the Windows job is the one that matters.

Run: push and watch `csharp-quality-windows`, or run the suite on a Windows
host if available.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/
git commit -m "fix(csharp): guard Unix-only file APIs by platform

Six analyzer findings for Unix file-mode APIs reachable from Windows.
Guarded with OperatingSystem checks and SupportedOSPlatform rather than
suppressed.

A catch of PlatformNotSupportedException is not a guard: it silently
does nothing on the platform where the guarantee was wanted, which is
worse than failing there."
```

---

### Task 5: Flip TreatWarningsAsErrors

**Files:**
- Modify: `packages/provide-uterm-csharp/Directory.Build.props:10`

- [ ] **Step 1: Confirm zero warnings**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release 2>&1 | grep -c "warning"
```

Expected: `0`.

Also check the Debug configuration, which the flag will affect too:

```bash
dotnet build -c Debug 2>&1 | grep -c "warning"
```

Expected: `0`. If Debug has warnings Release does not, fix them before flipping.

- [ ] **Step 2: Flip the flag**

```xml
    <!-- A warning that scrolls past is a warning nobody acts on: sixteen had
         accumulated by 2026-08-03, including a sign-extension bug. Suppressions
         are narrow and justified at their site; there is deliberately no
         project-wide NoWarn. -->
    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
```

- [ ] **Step 3: Build both configurations**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build -c Release
dotnet build -c Debug
```

Expected: both succeed.

- [ ] **Step 4: Prove the flag has teeth**

Add an unused private field to any source file.

Run: `dotnet build -c Release`

Expected: FAIL with `error CS0169` (was a warning).

Remove the field, rebuild, confirm success.

- [ ] **Step 5: Full gate**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests
make quality-gate
```

Expected: PASS, mutation gate included.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-csharp/Directory.Build.props
git commit -m "ci(csharp): treat warnings as errors

Sixteen warnings had accumulated, including a sign-extension defect and
six platform-compatibility findings on APIs reachable from Windows. A
warning that scrolls past is a warning nobody acts on.

No project-wide NoWarn: every suppression is narrow and justified at its
site. Verified the flag goes red against an unused field."
```

---

## Definition of done

Per the measurement spec, CM-10 closes when:

- `dotnet build -c Release` and `-c Debug` each produce zero warnings;
- `TreatWarningsAsErrors` is `true` and was observed failing the build against a
  reintroduced warning;
- no `NoWarn` property and no file-scope `#pragma warning disable` were added;
- `make quality-gate` passes and the Windows CI job is green.

Then update the CM-10 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Do this before CM-03. That plan adds a lot of control-plane code and a
  breaking API change; landing it into a build that tolerates warnings means its
  new nullable warnings arrive unnoticed, which is the situation this plan is
  ending.
- Task 4 overlaps CM-02, which restructures `FileIo.cs` and splits the Windows
  path out explicitly. Whichever lands second should re-run the build and
  confirm the count rather than assuming the other plan's fix covered it.
- The warning counts here are from a macOS host. Windows and Linux analyzers can
  differ, particularly for `CA1416`. Re-measure on CI rather than trusting the
  local count as complete.
