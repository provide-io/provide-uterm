# provide-terminal Demo Recordings

Each subdirectory contains the recordings for one feature demo. Every demo produces a full browser recording (`browser.mp4`), a short highlight clip (`*_trim.mp4`), and where applicable an asciinema terminal cast (`terminal.cast`).

Run a single demo:

```bash
uv run python -m scripts.demos.record_pty
```

Run all demos and assemble the super reel:

```bash
uv run python -m scripts.demos.reel
```

______________________________________________________________________

## Demos

| Feature                                   | What it shows                                                  | Directory       |
| ----------------------------------------- | -------------------------------------------------------------- | --------------- |
| [PTY Sessions](pty/README.md)             | Local pseudo-terminal served live in the browser               | `pty/`          |
| [Shell Rendering](shell_render/README.md) | Any image URL rendered as ANSI truecolor art                   | `shell_render/` |
| [Session Recording](recording/README.md)  | Full I/O capture to JSONL with screen snapshots                | `recording/`    |
| [Session Replay](replay/README.md)        | Scrub through recorded sessions in the browser                 | `replay/`       |
| [Annotations](annotation/README.md)       | Label sessions with metadata; auto-detect 20 security patterns | `annotation/`   |
| [SSH Connector](ssh/README.md)            | Connect to remote hosts over SSH                               | `ssh/`          |
| [Telnet Connector](telnet/README.md)      | Legacy telnet sessions with full terminal emulation            | `telnet/`       |
| [Tunnel Sharing](tunnel/README.md)        | Share sessions via Cloudflare Worker tunnel URL                | `tunnel/`       |
| [HTTP Inspection](http_inspect/README.md) | Intercept and inspect HTTP traffic in real time                | `http_inspect/` |
| [MCP Integration](mcp/README.md)          | 21 AI agent tools via Model Context Protocol                   | `mcp/`          |
| [Session Hijack](hijack/README.md)        | Viewer watches while operator takes exclusive control          | `hijack/`       |
| [DeckMux Presence](deckmux/README.md)     | Multiple operators share a session with live cursors           | `deckmux/`      |
| [Fleet Management](fleet/README.md)       | Workers self-register; broadcast deploy to the whole fleet     | `fleet/`        |
| [Fan-out Broadcast](fanout/README.md)     | Send one command to many sessions, collect all responses       | `fanout/`       |
| [Terminal Grid](demo_grid/README.md)      | 9 live terminals in a 3x3 grid, all running simultaneously     | `demo_grid/`    |
