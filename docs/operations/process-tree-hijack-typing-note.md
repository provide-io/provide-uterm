# Process Tree, Hijack Parity, and WS Typing Note

This change set hardens three internal areas without changing external HTTP, WS, CLI, or MCP contracts.

## Agent lifecycle

- Worker subprocesses now launch in their own process session/group.
- All manager teardown paths route through one shared stop helper.
- POSIX shutdown sends `SIGTERM` to the process group, waits, then escalates to `SIGKILL`.
- Windows shutdown keeps the graceful terminate path and escalates to `taskkill /T /F`.

Operational impact:

- An agent that spawns descendants is now torn down as a tree instead of only killing the direct parent PID.
- The heartbeat timeout, bust-respawn, prune, and explicit kill paths now use the same shutdown behavior.

## Hijack REST parity

- FastAPI and Cloudflare now share prompt-id extraction, prompt-regex validation, prompt/snapshot matching, and common hijack snapshot/events response builders.
- The shared regex limit is standardized at `200` characters.

Operational impact:

- Prompt-guard edge cases now behave the same across both backends for overlapping route contracts.
- Response payload field names stay aligned without introducing a shared transport runtime.

## FastAPI WS/control typing

- The FastAPI hijack backend now uses internal `TypedDict` aliases and helper constructors for the main control-frame shapes.
- Worker-originated snapshot, analysis, status, connection lifecycle, and term frames are normalized earlier in the WS path.

Operational impact:

- Static checks catch more frame-shape regressions during refactors.
- Wire format remains unchanged.

## Verification

- Targeted manager, hijack, websocket, and Cloudflare tests pass.
- Touched modules pass `mypy`.
- A POSIX integration test verifies that `kill_agent()` stops a parent process and its spawned child subtree.

## Known limitation

- Manual runtime verification was performed on Darwin/POSIX only.
- Windows remains covered by the unit path in this pass, not by live process-tree execution.
