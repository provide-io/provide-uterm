//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

// EngineHook is called after each ProcessScreen. Errors are logged and
// swallowed so one hook cannot abort the pipeline (matching the Python
// contract).
type EngineHook func(snapshot Snapshot, detection *PromptDetection, buffer *ScreenBuffer, isIdle bool) error

// DetectionEngine is a rule-based prompt-detection and data-extraction engine.
// Faithful port of the Python DetectionEngine.
type DetectionEngine struct {
	normalizer    func(string) string
	enabled       bool
	lastFinger    string
	lastMatch     *PromptMatch
	detector      *PromptDetector
	bufferManager *BufferManager
	idleThreshold float64
	screenSaver   *ScreenSaver
	namespace     string
	hooks         []EngineHook
}

// EngineOption configures a DetectionEngine.
type EngineOption func(*DetectionEngine)

// WithEngineNormalizer sets the prompt-region normalizer.
func WithEngineNormalizer(fn func(string) string) EngineOption {
	return func(e *DetectionEngine) { e.normalizer = fn }
}

// WithBufferSize sets the screen buffer capacity (default 50).
func WithBufferSize(n int) EngineOption {
	return func(e *DetectionEngine) { e.bufferManager = NewBufferManager(n) }
}

// WithIdleThreshold sets the idle threshold in seconds (default 2.0).
func WithIdleThreshold(s float64) EngineOption {
	return func(e *DetectionEngine) { e.idleThreshold = s }
}

// WithScreenSaver attaches a screen saver.
func WithScreenSaver(saver *ScreenSaver) EngineOption {
	return func(e *DetectionEngine) { e.screenSaver = saver }
}

// WithNamespace sets the game/namespace identifier.
func WithNamespace(ns string) EngineOption {
	return func(e *DetectionEngine) { e.namespace = ns }
}

// NewDetectionEngine builds an engine from a rule source (*RuleSet, RulesPath,
// or inline JSON string). Returns an error if the rules cannot be loaded.
func NewDetectionEngine(rules any, opts ...EngineOption) (*DetectionEngine, error) {
	e := &DetectionEngine{
		enabled:       true,
		bufferManager: NewBufferManager(50),
		idleThreshold: 2.0,
	}
	for _, opt := range opts {
		opt(e)
	}
	ruleset, err := LoadRuleset(rules)
	if err != nil {
		return nil, err
	}
	e.detector = mustDetectorWithNormalizer(ruleset.ToPromptPatterns(), e.normalizer)
	return e, nil
}

func mustDetectorWithNormalizer(patterns []Pattern, normalizer func(string) string) *PromptDetector {
	var opts []DetectorOption
	if normalizer != nil {
		opts = append(opts, WithNormalizer(normalizer))
	}
	d, _ := NewPromptDetector(patterns, opts...)
	return d
}

// SyncProcessScreen detects a prompt and extracts KV data (pure CPU). Returns
// nil when no prompt matched or the engine is disabled.
func (e *DetectionEngine) SyncProcessScreen(snapshot Snapshot) *PromptDetection {
	if !e.enabled {
		return nil
	}
	fingerprint := e.detector.PromptFingerprint(snapshot)
	var promptMatch *PromptMatch
	if fingerprint != "" && fingerprint == e.lastFinger {
		promptMatch = e.lastMatch
	} else {
		promptMatch = e.detector.DetectPrompt(snapshot)
		e.lastFinger = fingerprint
		e.lastMatch = promptMatch
	}
	if promptMatch == nil {
		return nil
	}
	kvData := map[string]any{}
	if pyTruthy(promptMatch.KVExtract) {
		screen, _ := snapshot["screen"].(string)
		if extracted := ExtractKV(screen, promptMatch.KVExtract); extracted != nil {
			kvData = extracted
		}
	}
	return &PromptDetection{
		PromptID:  promptMatch.PromptID,
		InputType: promptMatch.InputType,
		KVData:    kvData,
		Match:     promptMatch,
	}
}

// ProcessScreen detects a prompt with buffering, idle detection, screen saving,
// and hooks. Detection is pure CPU; hooks run sequentially afterward.
func (e *DetectionEngine) ProcessScreen(snapshot Snapshot) *PromptDetection {
	buffer := e.bufferManager.AddScreen(snapshot)
	isIdle := e.bufferManager.DetectIdleState(e.idleThreshold)

	detection := e.SyncProcessScreen(snapshot)
	if detection != nil && detection.Match != nil {
		buffer.MatchedPromptID = detection.Match.PromptID
	}

	if e.screenSaver != nil {
		promptID := ""
		if detection != nil {
			promptID = detection.PromptID
		}
		// A saver failure must not discard the detection.
		_, _ = e.screenSaver.SaveScreen(snapshot, promptID, false)
	}

	if detection != nil {
		idle := isIdle
		detection.IsIdle = &idle
		detection.Buffer = buffer
	}

	for _, hook := range e.hooks {
		_ = hook(snapshot, detection, buffer, isIdle)
	}
	return detection
}

// AddHook registers a hook called after each ProcessScreen.
func (e *DetectionEngine) AddHook(fn EngineHook) { e.hooks = append(e.hooks, fn) }

// HookCount returns the number of registered hooks.
func (e *DetectionEngine) HookCount() int { return len(e.hooks) }

// IsIdle reports whether the screen has been stable for >= the idle threshold.
func (e *DetectionEngine) IsIdle() bool {
	return e.bufferManager.DetectIdleState(e.idleThreshold)
}

// Namespace returns the game/namespace identifier.
func (e *DetectionEngine) Namespace() string { return e.namespace }

// SetNamespace updates the namespace and propagates it to the screen saver.
func (e *DetectionEngine) SetNamespace(ns string) {
	e.namespace = ns
	if e.screenSaver != nil {
		e.screenSaver.SetNamespace(ns)
	}
}

// GetScreenSaverStatus returns the screen-saver status.
func (e *DetectionEngine) GetScreenSaverStatus() map[string]any {
	if e.screenSaver == nil {
		return map[string]any{"enabled": false}
	}
	return map[string]any{
		"enabled":     e.screenSaver.Enabled(),
		"screens_dir": e.screenSaver.GetScreensDir(),
		"saved_count": e.screenSaver.GetSavedCount(),
		"namespace":   e.screenSaver.Namespace(),
	}
}

// SetScreenSaving enables or disables the screen saver, if present.
func (e *DetectionEngine) SetScreenSaving(enabled bool) {
	if e.screenSaver != nil {
		e.screenSaver.SetEnabled(enabled)
	}
}

// DebugState returns internal debug info.
func (e *DetectionEngine) DebugState() map[string]any {
	bm := e.bufferManager
	recent := bm.GetRecent(1)
	var isIdle bool
	var lastChange float64
	if len(recent) > 0 {
		isIdle = bm.DetectIdleState(e.idleThreshold)
		lastChange = recent[0].TimeSinceLastChange
	}
	var saverStatus any
	if e.screenSaver != nil {
		saverStatus = e.GetScreenSaverStatus()
	}
	return map[string]any{
		"idle_threshold_s": e.idleThreshold,
		"namespace":        e.namespace,
		"screen_buffer": map[string]any{
			"size":                    bm.Len(),
			"max_size":                bm.MaxSize(),
			"is_idle":                 isIdle,
			"last_change_seconds_ago": lastChange,
		},
		"screen_saver": saverStatus,
	}
}

// DetectWithDiagnostics detects with partial-match info for debugging.
func (e *DetectionEngine) DetectWithDiagnostics(snapshot Snapshot) PromptDetectionDiagnostics {
	return e.detector.DetectPromptWithDiagnostics(snapshot)
}

// ReloadRules hot-reloads rules transactionally: on failure the old rules
// remain active.
func (e *DetectionEngine) ReloadRules(rules any) error {
	ruleset, err := LoadRuleset(rules)
	if err != nil {
		return err
	}
	e.detector = mustDetectorWithNormalizer(ruleset.ToPromptPatterns(), e.normalizer)
	e.lastFinger = ""
	e.lastMatch = nil
	return nil
}

// Detector returns the underlying PromptDetector.
func (e *DetectionEngine) Detector() *PromptDetector { return e.detector }

// PatternCount returns the number of compiled patterns.
func (e *DetectionEngine) PatternCount() int { return e.detector.PatternCount() }

// Enabled reports whether the engine processes screens.
func (e *DetectionEngine) Enabled() bool { return e.enabled }

// SetEnabled toggles screen processing.
func (e *DetectionEngine) SetEnabled(v bool) { e.enabled = v }
