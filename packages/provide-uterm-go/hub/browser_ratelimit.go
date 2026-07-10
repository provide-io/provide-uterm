//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

// BrowserRateLimitPerSec returns the per-connection input-frame rate limit
// (tokens/sec) used to size a browser WS input token bucket. Mirrors the Python
// hub.browser_rate_limit_per_sec attribute read by ws_browser_term.
func (h *TermHub) BrowserRateLimitPerSec() float64 { return h.browserRateLimitPerSec }

// BrowserControlRateLimitPerSec returns the per-connection control-frame rate
// limit (tokens/sec) used to size a browser WS control token bucket. Mirrors the
// Python hub.browser_control_rate_limit_per_sec attribute.
func (h *TermHub) BrowserControlRateLimitPerSec() float64 { return h.browserControlRateLimitPerSec }
