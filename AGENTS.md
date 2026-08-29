# Agent Instructions

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## Reporting External State

**A state claim is only true as of the moment it was read.** The gap between
reading and saying is where the wrong answers live.

Before asserting anything about mutable external state — CI run results, a
browser's current page, what is on PyPI, whether a workflow fired — either read
it in the *same* turn as the assertion, or write the timestamp into the sentence
("as of 17:06, no run had fired"). A bare present-tense claim from a read taken
several tool calls ago is how a stale observation gets reported as current fact.

`scripts/state.sh` prints one timestamped snapshot of the state that is easiest
to get wrong: git position, the latest push *and* scheduled CI runs with their
failing jobs, the latest release run and its publish jobs, and the versions on
both package indexes. Run it before reporting status rather than assembling the
answer from memory.

Three specific traps it encodes, each of which produced a confident wrong answer:

- **Scheduled runs execute jobs no push runs.** A green push says nothing about
  them, so they are listed separately.
- **The jobs API pages at 30.** A run with 49 jobs hides everything past the
  thirtieth, which is how a failing job went unseen for six days. Always
  `--paginate` with `per_page=100`.
- **PyPI's JSON API serves stale data for minutes after an upload** while the
  simple index is already correct. Read the simple index.

Absence of evidence is not evidence of absence, and it is usually latency: a
workflow that has not fired after four minutes may fire after fifteen. Say what
was observed and when, not what is.
