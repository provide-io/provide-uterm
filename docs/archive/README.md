# Archive Policy

`docs/archive/` is for historical project records that should remain available but
should no longer guide implementation.

Move a document here when all of these are true:

- The implementation work it describes has landed on `main`.
- Current docs, tests, or release gates now describe the live behavior.
- Keeping the document in an active planning area would make the repo harder to
  scan.

Do not move active specs, roadmap proposals, runbooks, architecture references,
or deferred work here. Those documents stay in their topic directories until the
work is either implemented, superseded, or explicitly abandoned.

Archived documents may keep original dates, titles, and links. If a document is
superseded by a current reference, add a short note at the top pointing to the
current source of truth.
