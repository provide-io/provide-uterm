#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

# VNC lab demos

Proof assets for the first-party provide-uterm VNC console and `uterm-test-vnc` lab.

Generate (do not hand-edit media):

```bash
# Functional dual-port RFB lab
uv run python scripts/prove_vnc_lab.py

# First-party VNC console (plain ×2 + TLS + denied)
uv run python scripts/prove_uterm_vnc_console.py --runs 2

# Nested: VNC chrome → lab Chromium → text terminal demo
uv run python scripts/record_uterm_vnc_demo_video.py --seconds 16
```

Lab image: `docker/vnc-lab/`. Console UI: `packages/provide-uterm-frontend/vnc.html`.
