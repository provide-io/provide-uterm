# Tunnel Sharing

Share a terminal session via a secure URL backed by a Cloudflare Worker. Anyone with the link can open it in their browser without installing anything — the session is proxied through the Worker so no inbound port needs to be exposed on your machine.

**What you'll see:** A session is started and a tunnel URL is generated via a local Cloudflare Worker (wrangler dev --local). The browser navigates to the tunneled URL and connects to the live session, proving the Cloudflare path is fully operational end to end.

## Files

| File                                 | Description                  |
| ------------------------------------ | ---------------------------- |
| [browser.mp4](browser.mp4)           | Full browser recording       |
| [browser_trim.mp4](browser_trim.mp4) | Highlight clip               |
| [terminal.cast](terminal.cast)       | Terminal session (asciinema) |
