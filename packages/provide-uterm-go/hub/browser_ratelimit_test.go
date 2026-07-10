//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

func TestBrowserRateLimitAccessors(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.BrowserRateLimitPerSec = 42
		c.BrowserControlRateLimitPerSec = 7
	})
	mustEqual(t, h.BrowserRateLimitPerSec(), 42.0, "browser input rate accessor")
	mustEqual(t, h.BrowserControlRateLimitPerSec(), 7.0, "browser control rate accessor")
}

func TestBrowserRateLimitAccessorDefaults(t *testing.T) {
	// Unset config falls back to the hub defaults (30 input, 10 control, floored).
	h, _ := newTestHub(t, nil)
	mustEqual(t, h.BrowserRateLimitPerSec(), 30.0, "default browser input rate")
	mustEqual(t, h.BrowserControlRateLimitPerSec(), 10.0, "default browser control rate")
}
