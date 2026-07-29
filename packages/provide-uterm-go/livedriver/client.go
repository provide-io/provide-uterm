//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// BadToken is the credential presented for a step with auth "bad": a value no
// server driver mints, so every cell of the matrix offers the same rejectable
// bearer token. // pragma: allowlist secret
const BadToken = "uterm-live-driver-not-a-real-token" //nolint:gosec // deliberately invalid

// ClientOptions configures a client-role run.
type ClientOptions struct {
	// BaseURL is the origin reported by the server driver.
	BaseURL string
	// Token is the bearer token reported by the server driver.
	Token string
	// ScenarioPath is the scenario file to run.
	ScenarioPath string
}

// RunClient loads the scenario, runs it, and writes exactly one result line.
// A scenario that cannot even be parsed still produces a result — an errored
// one, attributed to the scenario's file name — because a driver that wrote
// nothing would look to the harness like a driver that crashed.
func RunClient(ctx context.Context, opts ClientOptions, stdout io.Writer) error {
	sc, err := LoadScenario(opts.ScenarioPath)
	if err != nil {
		return WriteLine(stdout, errorResult(ScenarioIDFromPath(opts.ScenarioPath), err))
	}
	return WriteLine(stdout, RunScenario(ctx, sc, opts))
}

// RunScenario performs every step and reports what was observed. It never
// evaluates an expectation: the harness owns the verdict.
func RunScenario(ctx context.Context, sc *Scenario, opts ClientOptions) Result {
	if missing := MissingCapabilities(sc.Requires, Capabilities()); len(missing) > 0 {
		r := newResult(sc.ID, StatusUnsupported)
		msg := "missing capabilities: " + strings.Join(missing, ", ")
		r.Error = &msg
		return r
	}

	runCtx, cancel := context.WithTimeout(ctx, sc.Timeout())
	defer cancel()

	result := newResult(sc.ID, StatusCompleted)
	runner := newStepRunner(opts)
	// What each step recorded, so a later step can refer to an earlier answer.
	seen := make(map[string]StepFields, len(sc.Steps))
	for _, step := range sc.Steps {
		// A reference that cannot be resolved is a malformed scenario rather
		// than something a server did, so it ends the run without leaving a
		// step behind for the harness to compare.
		// References are resolved once, here, and not again inside the
		// repetitions: a reference can never name a repeated step, so nothing
		// it could read changes between one repetition and the next.
		if err := resolveStep(&step, seen); err != nil {
			failRun(&result, step.ID, err)
			break
		}
		if !runner.perform(runCtx, step, seen, &result) {
			break
		}
	}
	return result
}

// perform runs one step's repetitions, recording each as its own observation
// under the id [Step.observationIDs] gives it. It reports whether the run may
// continue.
//
// A repetition that merely fails — a refusal, a dead socket — is an
// observation like any other and the repetitions carry on, because a scenario
// repeats a step exactly when it expects the answers to change. Only a step
// the driver cannot perform at all ends the run.
func (r *stepRunner) perform(
	ctx context.Context,
	step Step,
	seen map[string]StepFields,
	result *Result,
) bool {
	for _, observed := range step.observationIDs() {
		fields, fatal := r.run(ctx, step)
		seen[observed] = fields
		result.Steps = append(result.Steps, StepResult{ID: observed, Fields: fields})
		if fatal != nil {
			// The driver could not perform this step. That is a driver failure,
			// not an observation, so the run is reported as an error rather
			// than silently skipping the step.
			failRun(result, observed, fatal)
			return false
		}
	}
	return true
}

// failRun marks a run as a driver failure, naming the step that ended it.
func failRun(result *Result, stepID string, err error) {
	result.Status = StatusError
	msg := "step " + stepID + ": " + err.Error()
	result.Error = &msg
}

// stepRunner performs steps against one server, recording every exchange.
type stepRunner struct {
	baseURL string
	token   string
	obs     *observer
	http    *http.Client
}

// newStepRunner wires one observer under both the client library and the raw
// HTTP path, so all six actions are recorded identically.
func newStepRunner(opts ClientOptions) *stepRunner {
	obs := newObserver(nil)
	return &stepRunner{
		baseURL: strings.TrimRight(opts.BaseURL, "/"),
		token:   opts.Token,
		obs:     obs,
		http:    &http.Client{Transport: obs},
	}
}

// authHeaders selects the Authorization header for a step. An empty selector
// means "token"; an unrecognised one is a driver failure rather than a guess.
func (r *stepRunner) authHeaders(mode string) (map[string]string, error) {
	switch mode {
	case "", AuthToken:
		if r.token == "" {
			// Nothing to present. Sending "Bearer " would be a third,
			// unspecified credential shape.
			return map[string]string{}, nil
		}
		return map[string]string{"Authorization": "Bearer " + r.token}, nil
	case AuthNone:
		return map[string]string{}, nil
	case AuthBad:
		return map[string]string{"Authorization": "Bearer " + BadToken}, nil
	default:
		return nil, fmt.Errorf("unknown auth mode %q", mode)
	}
}

// libClient builds the real consumer-facing client for one step's credentials.
func (r *stepRunner) libClient(headers map[string]string) *client.HijackClient {
	return client.NewHijackClient(r.baseURL,
		client.WithHeaders(headers),
		client.WithHTTPClient(r.http),
	)
}

// run performs one step. The second return value is non-nil only when the
// driver could not perform the step at all, which ends the run.
func (r *stepRunner) run(ctx context.Context, step Step) (StepFields, error) {
	headers, err := r.authHeaders(step.Auth)
	if err != nil {
		return failFields(err.Error()), err
	}
	r.obs.reset()
	switch step.Action {
	case ActionHTTPGet:
		return r.rawRequest(ctx, http.MethodGet, step, headers)
	case ActionHTTPPost:
		return r.rawRequest(ctx, http.MethodPost, step, headers)
	default:
		return r.libraryAction(ctx, step, headers)
	}
}

// libraryAction performs the actions that must go through the client library,
// so what is under test is the library a consumer would actually use. The
// library shapes ok and body; the observer underneath it supplies the status
// the library drops.
func (r *stepRunner) libraryAction(ctx context.Context, step Step, headers map[string]string) (StepFields, error) {
	perform, err := callFor(r.libClient(headers), step)
	if err != nil {
		return failFields(err.Error()), err
	}
	value, callErr := perform(ctx)
	return r.obs.libFields(value, callErr), nil
}

// rawRequest performs an http_get/http_post step directly, covering the
// surfaces no client method reaches.
func (r *stepRunner) rawRequest(
	ctx context.Context,
	method string,
	step Step,
	headers map[string]string,
) (StepFields, error) {
	if step.Path == "" {
		return fatalf("action %s requires path", step.Action)
	}
	var body io.Reader
	if len(step.Body) > 0 {
		body = bytes.NewReader(step.Body)
	}
	req, err := http.NewRequestWithContext(ctx, method, r.baseURL+step.Path, body)
	if err != nil {
		return fatalf("action %s cannot request %q: %v", step.Action, step.Path, err)
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	resp, doErr := r.http.Do(req)
	if resp != nil {
		// The observer already drained and replaced the body; this releases the
		// connection.
		_ = resp.Body.Close()
	}
	return r.obs.rawFields(doErr), nil
}

// fatalf describes a step the driver could not perform: the fields record what
// little was seen, and the error ends the run.
func fatalf(format string, args ...any) (StepFields, error) {
	err := fmt.Errorf(format, args...)
	return failFields(err.Error()), err
}
