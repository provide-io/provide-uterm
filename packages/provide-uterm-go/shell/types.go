//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

// AnimatedResult is the return type for animated render/cast output — the
// caller handles frame timing. Port of commands/types.py:AnimatedResult.
type AnimatedResult struct {
	Frames []string
	FPS    float64
	Loop   bool
}

// Result is the return value of a dispatched command. Exactly one of Text or
// Animated is populated: Text holds raw output frames (the Python list[str]),
// while Animated is non-nil for the AnimatedResult case.
type Result struct {
	Text     []string
	Animated *AnimatedResult
}

// textResult builds a Result carrying one or more text frames.
func textResult(frames ...string) Result {
	return Result{Text: frames}
}

// animatedResult builds a Result carrying an animation.
func animatedResult(a AnimatedResult) Result {
	return Result{Animated: &a}
}
