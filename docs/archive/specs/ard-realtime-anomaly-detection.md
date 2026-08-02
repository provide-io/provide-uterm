# ARD: Real-Time Anomaly Detection

> **As-built (2026-06):** the `provide.uterm.detection` module / `TerminalDetector`
> / `DetectionEvent` / `DetectionSink` design below was **not** built as
> specified. The shipped pattern-detection feature is the
> **`provide-uterm-annotation`** package (`provide.uterm.annotation`):
> `PatternDetector.detect(event_type, text, seq) -> list[Annotation]`
> over a recording-oriented `DetectionRule` (`rule_id`, `label`,
> `pattern: re.Pattern`, `severity`, `description_template`, `event_types`,
> `category`) that produces `Annotation`/`AnnotationSpan` tied to recording
> `seq` numbers — *not* hot-path `DetectionEvent`s posted to webhook sinks.
> The severity scale is `info | high | critical` (no `warn`), built-in rule
> IDs are dotted (`cred.aws_access_key`, `esc.sudo`, `dest.rm_rf`, …), there
> are **20** built-in rules, and the AWS pattern is `AKIA[0-9A-Z]{12}`.
> Separately, the **enforcing** real-time governance layer that did ship is
> the fail-closed `WebhookBehavioralAuditGate` (`bridge/hub/ext.py`): on
> webhook error/timeout it returns `PolicyDecision(action="deny")` unless
> `fail_open=True`, and `GovernanceConfig.behavioral_fail_open` defaults to
> `False` — i.e. it *blocks*, contrary to the emit-only Non-Goal below.
> The `detection_rules_list` / `detection_events_recent` MCP tools were not
> built. The rule table and sink design below are retained as the original
> proposal.

## Problem

Terminal sessions in privileged environments can expose credentials, trigger privilege escalation, or execute destructive commands — and no one finds out until after the fact. SIEM and log-aggregation tools operate on finalized logs with minutes-to-hours of lag. EDR tools live on the host, not the proxy.

provide-uterm receives every byte of terminal output *before* it reaches the browser. This is the earliest possible interception point — before the operator sees the output, before screenshots are taken, before any downstream system processes it. A rule engine at this layer can emit alerts in real time, with session context that host-level tools cannot see (who is connected, what role they hold, whether a hijack is active).

---

## Goals

- Evaluate terminal output (and optionally input) against a pluggable rule engine in real time.
- Support multiple rule types: regex patterns, multi-line rolling-window matches, LLM-assisted classification (optional, async).
- Emit structured `DetectionEvent` objects to configurable sinks (webhook, asyncio callback, in-memory queue, log).
- Add < 2 ms latency to the output broadcast path on rule-miss (the common case).
- Integrate with the existing `append_event` audit trail in `TermHub`.
- Require zero changes to workers, browsers, or remote hosts.

---

## Non-Goals

- Blocking or modifying the terminal stream (see ARD: Command Approval Workflows).
- Replacing a SIEM — this emits events *to* a SIEM, not replaces one.
- Running LLM inference in the hot broadcast path (LLM checks run in a background task).

---

## Architecture

### Rule Interface

```python
@dataclass(slots=True)
class DetectionRule:
    rule_id: str
    name: str
    severity: str  # "info" | "warn" | "critical"
    kind: str  # "output" | "input" | "both"
    pattern: str  # regex (for "regex" engine) or prompt template (for "llm" engine)
    engine: str  # "regex" | "rolling" | "llm"
    window_lines: int = 5  # for "rolling" engine: how many output lines to buffer
    context_lines: int = 2  # lines of context to include in DetectionEvent
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DetectionEvent:
    rule_id: str
    rule_name: str
    severity: str
    session_id: str
    worker_id: str
    ts: float
    kind: str  # "output" | "input"
    matched_text: str  # the specific text that triggered the rule
    context: str  # surrounding lines for human review
    principal: str | None  # subject_id of connected browser(s), or None
    role: str | None
    hijack_active: bool
```

### Detector

```python
class TerminalDetector:
    def __init__(self, rules: list[DetectionRule], sink: DetectionSink) -> None: ...
    def check_output(self, session_id: str, worker_id: str, text: str, *, ctx: DetectionContext) -> None: ...
    def check_input(self, session_id: str, worker_id: str, text: str, *, ctx: DetectionContext) -> None: ...
```

`check_output` and `check_input` are **synchronous** on the hot path. Regex rules are evaluated inline. Rolling-window rules maintain a `deque[str]` per session. LLM rules submit a `asyncio.create_task` and return immediately — they can never block the broadcast loop.

### Sink Interface

```python
class DetectionSink(Protocol):
    async def emit(self, event: DetectionEvent) -> None: ...
```

Built-in sinks:
- `LogDetectionSink` — structured log via `provide.telemetry`
- `WebhookDetectionSink` — HTTP POST to a configurable URL (with retry and dead-letter queue)
- `CallbackDetectionSink` — asyncio callback for programmatic consumers (tests, MCP tools)
- `HubEventDetectionSink` — calls `hub.append_event(worker_id, "detection", {...})` so events appear in the session event stream

### Hot Path Integration

`TermHub` constructor gains:

```python
hub = TermHub(
    ...
    detector=TerminalDetector(rules=RULES, sink=WebhookDetectionSink(url=config.alert_webhook)),
)
```

In `ws_worker_term` (the worker receive loop), after `await hub.broadcast(worker_id, frame)`:

```python
if hub._detector is not None and frame.get("type") == "snapshot":
    screen_text = frame.get("screen", "")
    hub._detector.check_output(session_id, worker_id, screen_text, ctx=_build_ctx(hub, worker_id))
```

For input detection, in `_handle_input` in `browser_handlers.py`, after the send:

```python
if hub._detector is not None and data:
    hub._detector.check_input(session_id, worker_id, data, ctx=_build_ctx(hub, worker_id, ws=ws))
```

### Built-In Rule Library

A default `provide.uterm.detection.rules` module ships a curated starter set:

| Rule ID | Name | Severity | Pattern |
|---|---|---|---|
| `cred-001` | AWS key exposure | critical | `AKIA[0-9A-Z]{16}` |
| `cred-002` | Private key header | critical | `-----BEGIN .* PRIVATE KEY-----` |
| `cred-003` | Password in prompt | warn | `(?i)(password|passwd|secret)\s*[:=]` |
| `priv-001` | sudo su | warn | `sudo su` / `sudo -i` |
| `priv-002` | chmod 777 | warn | `chmod\s+[0-7]*7[0-7]*7` |
| `dest-001` | rm -rf / | critical | `rm\s+-[a-z]*r[a-z]*f\s+/` |
| `dest-002` | DROP TABLE | critical | `(?i)DROP\s+TABLE` |

### Rolling-Window Engine

For multi-line patterns (e.g., a failed `sudo` followed by a password entry), the rolling engine buffers the last N lines per session in a `collections.deque`. On each new line, it joins the window and applies the pattern. Window state is discarded on worker disconnect.

### LLM Engine (Optional)

Rules with `engine="llm"` submit a background task that calls a configurable LLM endpoint (OpenAI-compatible, Claude API via `anthropic`) with the matched text and a prompt template. The task calls `sink.emit()` asynchronously if the model classifies the event as a true positive. This path is strictly opt-in and has no effect on the hot path.

---

## CF Backend Parity

The CF DO backend runs the same `TerminalDetector` (pure Python, no asyncio dependency on the synchronous check path). The `RuntimeProtocol` gains:

```python
def get_detector(self) -> TerminalDetector | None: ...
```

Webhook sink fires via `fetch()` in the CF runtime.

---

## MCP Integration

Two new MCP tools:

- `detection_rules_list` — list active rules for a session
- `detection_events_recent` — return the last N `DetectionEvent` objects for a session (from the in-memory ring buffer, capacity configurable)

---

## Security Considerations

- Detection results are never sent to the browser that triggered them (no side channel).
- Rule patterns are compiled at startup, not per-message (no ReDoS from dynamic patterns).
- LLM engine never sends raw credentials to the model — the matched text is truncated to 256 chars and the prompt instructs the model not to log or store it.
- Detection sinks with external HTTP calls run in background tasks with a circuit breaker to prevent a slow webhook from accumulating unbounded goroutines.

---

## Testing

- `test_detector_regex.py` — built-in rules trigger on known bad strings, do not trigger on clean strings.
- `test_detector_rolling.py` — multi-line rules trigger correctly across window boundaries.
- `test_detector_hot_path_latency.py` — benchmark: 1000 broadcasts with 20 active rules < 2 ms per call.
- `test_detector_sink_webhook.py` — webhook sink emits correct payload, retries on 5xx, does not block broadcast.
- `test_detector_llm_async.py` — LLM engine submits task, does not block, emits event when model returns True.

---

## Open Questions

1. Should detection rules be configurable at runtime (hot-reload) or only at startup?
2. Should the rolling-window buffer survive worker reconnects, or reset on each new connection?
3. Should `DetectionEvent` objects be included in the compliance recording (ARD: Session Audit)?
4. What is the cardinality limit on per-session rolling buffers (max sessions × window_lines × avg line length)?
5. Should there be a per-rule suppression window (e.g., same rule fires at most once per 60s per session) to prevent alert storms?
