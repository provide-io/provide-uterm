# MCP Integration

provide-uterm exposes 21 tools via the Model Context Protocol (MCP), the standard
interface for AI agent tool use. An AI agent can create and manage sessions, acquire hijack
leases, broadcast commands to fleets, annotate events, inspect recordings, and more — all
through a single MCP server that any compatible agent can connect to.

**What you'll see:** The MCP server starts and exposes its tool listing. The demo walks
through key tools: reading session state, sending input via hijack, triggering a fan-out
broadcast, and querying recording entries. Everything an agent needs to orchestrate terminals
is available as a structured tool call.

## Files

| File | Description |
|------|-------------|
| [browser.mp4](browser.mp4) | Full browser recording |
| [browser_trim.mp4](browser_trim.mp4) | Highlight clip |
| [terminal.cast](terminal.cast) | Terminal session (asciinema) |
