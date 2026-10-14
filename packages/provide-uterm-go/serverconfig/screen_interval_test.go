//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import "testing"

// recording.screen_interval_s is off (0) by default, is read from [recording],
// and is never negative, with the reference's refusal text.
func TestTheScreenIntervalIsOffByDefaultAndNeverNegative(t *testing.T) {
	if got := DefaultServerConfig().Recording.ScreenIntervalS; got != 0 {
		t.Fatalf("screen_interval_s defaults to %v, want 0", got)
	}
	cfg, err := ConfigFromMapping(map[string]any{"recording": map[string]any{"screen_interval_s": 0.5}})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Recording.ScreenIntervalS != 0.5 {
		t.Fatalf("screen_interval_s = %v, want 0.5", cfg.Recording.ScreenIntervalS)
	}
	_, err = ConfigFromMapping(map[string]any{"recording": map[string]any{"screen_interval_s": -1}})
	want := "recording.screen_interval_s must be >= 0 (0 = only screens sent to a viewer), got: -1.0"
	if err == nil || err.Error() != want {
		t.Fatalf("err = %v, want %q", err, want)
	}
}
