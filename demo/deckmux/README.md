# DeckMux Presence

A 9-person production incident response played out in a single shared terminal.
An on-call engineer gets paged, starts investigating DB connection timeouts, and
the team assembles over the next few minutes. Each person scrolls through the logs
independently — their position shows as a colored bar on the edge minimap, so you
can see at a glance who's looking at what. Control is handed off from the first
responder to the DBA to run the fix, then to the SRE to verify. Toast notifications
announce each handoff. Typing indicators pulse. People leave as the incident wraps up.

**Cast:** Tim (principal, operator), Kal (SRE), Chris (backend dev), Brandon (DBA),
Kyle (security), Logan (devops), Heidi (eng manager, viewer), Heather (QA),
Sentinel (monitoring bot, viewer).

**What you'll see:**
- Presence bar filling from 1 to 9 avatars as the team joins
- Edge indicator bars spreading across the minimap as people scroll to different
  parts of the scrollback
- Control transfer toasts ("Control transferred to Brandon") with owner glow moving
  between avatars
- Typing indicators pulsing when the active operator types
- Avatars disappearing as people disconnect during resolution

## Files

| File | Description |
|------|-------------|
| [composite.mp4](composite.mp4) | 3-column split: Tim \| Brandon \| Heidi |
| [composite_trim.mp4](composite_trim.mp4) | Highlight clip (handoff moment) |
| [operator.mp4](operator.mp4) | Operator perspective (operator, hero) |
| [brandon.mp4](brandon.mp4) | Brandon's perspective (DBA, receives handoff) |
| [heidi.mp4](heidi.mp4) | Heidi's perspective (eng manager, viewer) |
| [kal.mp4](kal.mp4) | Kal's perspective (SRE, verifies fix) |
| [chris.mp4](chris.mp4) | Chris's perspective (backend dev) |
| [kyle.mp4](kyle.mp4) | Kyle's perspective (security) |
| [logan.mp4](logan.mp4) | Logan's perspective (devops) |
| [heather.mp4](heather.mp4) | Heather's perspective (QA) |
| [sentinel.mp4](sentinel.mp4) | Sentinel's perspective (monitoring bot) |
| [terminal.cast](terminal.cast) | Terminal session (asciinema) |
