# HANDOFF — uterm VNC web console demo (black / broken video)

**Date:** 2026-07-21  
**Branch:** `main` @ `f7c72c28` (pushed to `origin/main`)  
**Working tree:** clean except untracked `demo/vnc-lab/storyboard/` (intermediate frames; do not need to commit)

---

## Problem / request (user intent)

Ship a **first-party provide-uterm VNC web console** (terminal-parity product chrome) with proof media that shows:

1. Host Playwright (or human) opens first-party `vnc.html` → real noVNC RFB over uterm binary WS relay.
2. Lab container runs Chromium on **uterm text demos** (not `example.com`).
3. Nested browser shows a **live** provide-shell transcript via `/ws/browser/.../term`.
4. Video is **readable**, **not flashing**, **not letterboxed**, and **moves through demos quickly** (website-demo pacing).

**Current user complaint (latest):**  
`demo/vnc-lab/uterm-vnc-text-demos.webm` is **all black** when opened. Previous iterations were: stuck on “Initializing…”, static freeze-frame, letterbox margins, RFB connect/disconnect flash, and shredded glyph spacing.

---

## Goal doc / plan

- Spec / goal: `.provide/goals/uterm-vnc-web-console.md`
- Recorder: `scripts/record_uterm_vnc_demo_video.py`
- Lab image: `docker/vnc-lab/` (`Dockerfile`, `entrypoint.sh`)
- Console UI: `packages/provide-uterm-frontend/` → `vnc.html`, `src/vnc-page.ts`, `static/vnc-page.css`
- Nested terminal: `src/terminal-element.ts` + `terminal-page.ts` → `terminal.html?worker_id=…&role=browser`

---

## What already works (do not re-litigate)

| Surface | Status | Notes |
|--------|--------|--------|
| Binary WS `/worker/{id}/hijack/{hid}/gui/vnc?target_id=` | Shipped | RFB dial plain/TLS; human relay; unbuffered makefile; RFB filter for cut-text |
| First-party `vnc.html` + `@novnc/novnc` ^1.7 | Shipped | ESM; qualityLevel/compression set in `vnc-page.ts` |
| `graphical_targets` + factory seed | Shipped | Config TOML targets for lab RFB ports |
| Nested terminal paint (browser role) | Fixed in `15a93962` | `terminal-element` now handles control `term`/`snapshot`, `snapshot_req` on open, buffers writes until xterm ready |
| Host-side terminal.html (Playwright, no custom headers on CDN) | Works | Live `/say` fan-out updates buffer text |
| REST hijack drive of reference `shell` connector | Works | In-memory chat (`/say`, `/clear`, …) — **not a real PTY** |
| Lab boots Chromium on `terminal.html?worker_id=provide-shell&role=browser` | Works | `DEMO_URL=http://host.docker.internal:{port}/_terminal/...` |
| Storyboard PNGs (chap_01–04) | Content present | Readable terminal text when opened as stills (mean brightness ~33–38, not pure black) |

---

## What is broken right now

### 1. Demo **video** appears black to the user
- Path: `demo/vnc-lab/uterm-vnc-text-demos.webm` (~4.4s, ~117KB after last run).
- Built by **ffmpeg concat of storyboard PNGs** (not continuous Playwright capture).
- Local frame extract from webm has mean brightness ~33 (dark UI, not pure 0) — may still look “black” in some players, or encode may be wrong (libvpx + concat + yuv420p).
- **Still PNGs** under `demo/vnc-lab/screenshots/` and `demo/vnc-lab/storyboard/` show real UI; prefer those as ground truth while fixing video.

### 2. Live RFB paint path is flaky under Xvfb
- Nested Chromium **does** get WS updates (proven via host Playwright on same session).
- **noVNC canvas often does not change** during a continuous RFB session after first ServerInit paint.
- Root cause class: Chromium **canvas** under Xvfb does not generate reliable XDAMAGE; x11vnc incremental updates stall.
- Mitigations tried (partial / insufficient without reconnect):
  - `x11vnc -noxdamage -fs 1 -nonap -wait 5 -defer 5`
  - Soft Chromium flags (`LIBGL_ALWAYS_SOFTWARE=1`, no ANGLE)
  - No WM (fluxbox removed) + `xdotool` pin window to 1280×720+0+0
  - 1px resize “nudge” via docker exec xdotool
- **RFB disconnect/reconnect** *did* force new frames but user correctly rejected it (flashing “Connecting…”).

### 3. Continuous Playwright video of noVNC was either static or flashing
- Static: single connect, demos after attach never visible on RFB canvas.
- Flashing: reconnect every chapter → bad UX, also double-scale CSS once shredded text.

---

## Approaches tried (and outcomes)

| Approach | Result |
|----------|--------|
| Drive bash commands into `shell` connector | Wrong — connector is slash-chat; use `/say` etc. |
| Drive demos *before* lab attach only | Nested gets snapshot once; still need live RFB updates for motion |
| CSS force `canvas { width/height: 100% }` + `aspect-ratio: 16/9` stage | Filled panel but **double-scaled** glyphs (“E x c l u s i v e  h i j a c k”) |
| RFB reconnect every N beats | Canvas motion OK; **user rejected** (flash) |
| xdotool resize nudge without reconnect | Did **not** move canvas fingerprints in last continuous run |
| Storyboard: chapter → fresh VNC page → PNG → ffmpeg | Stills good; **user says final webm is all black** (encode/playback issue to fix first) |

---

## Key code / files

### Frontend
- `packages/provide-uterm-frontend/src/vnc-page.ts` — noVNC boot; `scaleViewport=true`, `clipViewport=false`; quality 9
- `packages/provide-uterm-frontend/static/vnc-page.css` — **do not** force canvas pixel size; panel chrome only
- `packages/provide-uterm-frontend/src/terminal-element.ts` — browser-role `snapshot`/`term` + pending write buffer
- Build publishes into `packages/provide-uterm-server/src/provide/uterm/server/frontend/` (**gitignored assets**; rebuild with `npm run build:frontend`)

### Lab
- `docker/vnc-lab/entrypoint.sh` — Xvfb, dual x11vnc, **no fluxbox**, Chromium pinned via xdotool
- `docker/vnc-lab/Dockerfile` — chromium, x11vnc, xdotool, x11-xserver-utils (fluxbox removed)
- Image name used by scripts: `uterm-test-vnc` (see `scripts/prove_vnc_lab.py`)

### Recorder
- `scripts/record_uterm_vnc_demo_video.py`
  - Starts `uterm server` with `UTERM_TEST_MODE=1`, header auth, sessions `provide-shell` + `demo-shell-2` + VNC lease shell
  - Lab `DEMO_URL` → host terminal.html browser role
  - **Current path:** storyboard PNGs + ffmpeg concat → `demo/vnc-lab/uterm-vnc-text-demos.webm`
  - Shell is **reference connector** (`connector_type = "shell"`), drive with `/say` chapters in `LIVE_DEMO_CHAPTERS`
- Helpers: `scripts/prove_vnc_lab.py`, `scripts/prove_uterm_vnc_console.py`

### Auth note
- `UTERM_TEST_MODE=1` mints WS principal `test-admin`; REST must use same `x-uterm-principal: test-admin` for lease ownership.
- Playwright **must not** set context-wide `extra_http_headers` for CDN loads (breaks xterm.js CORS). Use cookies + page headers for same-origin only.

---

## Recent commits (this effort)

```
f7c72c28 fix(vnc): stop RFB flash and restore readable terminal text
34295073 fix(vnc): snappy demo pacing and edge-to-edge remote desktop
6b28c0ab fix(vnc): clean console frame and record live nested-browser motion
15a93962 fix(terminal): paint browser-role snapshots and clear nested loading
4064dcc6 fix(vnc): restore terminal CSS, widen console margins, reset demo/vnc-lab
13ce2a47 fix(vnc-demo): boot lab Chromium on text terminal URL, not example.com
4629732b demo(vnc): video of first-party VNC console running nested text demos
```

---

## Confirmed root causes (with evidence)

1. **Nested loading forever (fixed):** `terminal-element` only wrote `type===data`; browser path sends control `snapshot`/`hello`. Also WS could fire before xterm existed.
2. **Static RFB after first paint:** Xvfb + Chromium canvas + x11vnc damage model; continuous noVNC session does not reliably show nested xterm updates without full session reset.
3. **Garbled text:** CSS canvas stretch + scaleViewport double transform.
4. **Flash:** intentional RFB reconnect in recorder.
5. **Black webm (open):** storyboard stills are *not* black; treat as **ffmpeg/player pipeline** until proven otherwise — validate by opening `storyboard/chap_0N.png` vs playing webm.

---

## Checklist for next session

### A. Unblock “black video” first (highest priority)
1. Open stills: `demo/vnc-lab/storyboard/chap_01.png` … `chap_04.png` and `screenshots/uterm-vnc-text-demos-full.png`. Confirm content.
2. Diagnose webm:
   ```bash
   ffprobe -hide_banner demo/vnc-lab/uterm-vnc-text-demos.webm
   ffmpeg -y -i demo/vnc-lab/uterm-vnc-text-demos.webm -vf "select=eq(n\,0)+eq(n\,1)+eq(n\,2)+eq(n\,3)" -vsync vfr /tmp/vnc_f%02d.png
   # compare /tmp/vnc_f*.png to storyboard PNGs
   ```
3. Fix encode if frames are black/corrupt after encode:
   - Prefer `libvpx-vp9` or `libx264` + `mp4` for reliability
   - Or build video from PNGs with explicit `-framerate 1` and re-check first frames
   - Assert in recorder: decoded frame mean brightness **or** SSIM vs source PNG > threshold (fail CI if black)

### B. Deliver progressive *readable* motion without flash
Preferred options (pick one; do not reintroduce reconnect flash):

**Option 1 — Storyboard (current architecture), fixed encode**  
- Keep chapter PNGs; fix ffmpeg; maybe 2s/frame; assert non-black.  
- Fast to ship.

**Option 2 — Continuous RFB once nested content is fully painted**  
- Drive all `LIVE_DEMO_CHAPTERS` **before** opening VNC (nested browser already has final screen).  
- Open VNC once; record 6–8s stable Connected view (cursor blink only).  
- Honest: little “live typing”, but no black/flash/garbage.

**Option 3 — Fix live damage for real continuous video**  
- Harder: get Chromium under Xvfb to damage X on every xterm write, **or** use a nested surface that is not a canvas (DOM terminal / full page refresh), **or** proxy a second display capture.  
- Do **not** use RFB reconnect as the demo mechanism.

### C. Quality bar before claiming done
- [ ] Video opens in QuickTime/Chrome and shows nested Chromium + readable shell text  
- [ ] Status chip stays **Connected** (no Connecting flash loop)  
- [ ] Glyphs not spaced-out  
- [ ] Progresses through demo lines in ≤ ~12s total  
- [ ] Margins acceptable (panel chrome OK; avoid huge letterbox)  
- [ ] `npm run build:frontend` then re-record; commit `demo/vnc-lab/` stills + video  
- [ ] No reconnect spam in recorder  

### D. Reproduce / re-record commands

```bash
# Frontend → server static (assets gitignored)
npm run build:frontend

# Full lab rebuild + record (storyboard path)
uv run python scripts/record_uterm_vnc_demo_video.py --seconds 0

# Skip docker image rebuild
uv run python scripts/record_uterm_vnc_demo_video.py --skip-build --seconds 0

# Artifacts
open demo/vnc-lab/uterm-vnc-text-demos.webm
open demo/vnc-lab/screenshots/uterm-vnc-text-demos-full.png
open demo/vnc-lab/storyboard/
```

### E. Important product facts
- Hosted `shell` connector = **interactive reference transcript**, not bash. Drive with `/say`, `/clear`, `/nick`, `/status`, `/shell`.
- `role=browser` WS path: `/ws/browser/{worker_id}/term`; control channel framing required.
- VNC lease worker in demo config is separate from `provide-shell` (lease shell vs demo shell).

---

## Suggested first message for the next LLM

> Read `.provide/HANDOFF.md` and `.provide/goals/uterm-vnc-web-console.md`.  
> User reports `demo/vnc-lab/uterm-vnc-text-demos.webm` is all black. Storyboard PNGs in `demo/vnc-lab/storyboard/` had readable content after `f7c72c28`.  
> 1) Prove whether the webm encode is black vs the PNGs.  
> 2) Fix the video pipeline so the user sees progressive nested-terminal demos without RFB reconnect flash and without CSS double-scale text.  
> 3) Re-record, verify by actually opening the video frames, commit, push.

---

## Out of scope / do not derail
- Re-opening noVNC package version debate (1.7 is fine).  
- Rewriting the RFB relay for this demo fix.  
- Cross-language parity (older HANDOFF content); this file **replaces** that as active handoff.
