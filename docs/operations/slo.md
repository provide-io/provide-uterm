# Service SLOs

These SLO targets are the release baseline for hosted terminal control-plane deployments.

## User-facing latency SLOs

- Snapshot delivery latency (worker event -> browser receive):
  - p95: <= 350 ms
  - p99: <= 900 ms
- Command round-trip latency (browser input -> worker ack/event):
  - p95: <= 250 ms
  - p99: <= 700 ms
- Reconnect recovery time (browser WS reconnect -> first `hello`):
  - p95: <= 2.5 s
  - p99: <= 6.0 s

## Availability SLOs

- Browser WS successful connect rate: >= 99.9%
- Auth success rate for valid credentials: >= 99.99%

## Measurement

- Run load/churn with `scripts/load_profile.py`.
- Run restart-failure injection with `scripts/failure_injection.py` (scenarios: `restart`, `ws_flap`, `lease_expiry`).
- Run REST hijack latency probe with `scripts/latency_probe.py`.
- Run true hub->browser WS broadcast delivery probe with `scripts/ws_delivery_probe.py`.
  This measures the real worker-event->hub->WS delivery path via hijack-state broadcast frames.
- Note: `scripts/latency_probe.py` measures REST-hijack command/send and snapshot-fetch timings;
  use it as a comparative release-over-release signal, not a direct substitute for browser WS snapshot-delivery SLOs.
- Record results per release candidate in `artifacts/rc-baseline/`.
- Do not promote RCs that miss p95 or p99 targets without a written exception.

## RC-captured baseline (v0.4.0-rc3)

Live numbers measured against the reference server on the rc3 cut.
Treat these as the current production-equivalent ceiling; later RCs
that regress past them need an explicit waiver.

| Probe | p95 | p99 | Source |
|---|---|---|---|
| Browser WS connect | ~22 ms | **23.86 ms** | `artifacts/load-profile/load-profile-*.txt` |
| Browser WS hello frame after connect | ~3.4 ms | **5.68 ms** | (same) |
| Session reconnect after disrupt | — | **3.13 ms** | `artifacts/rollback-drill/rollback-drill-*.json` |

All three are well under the SLO targets above. The hello-frame
number (5.68 ms p99) is the tightest one and the most sensitive to
control channel changes — if you touch `provide.uterm.control_channel`
or the hub's hello-emission path, re-run `scripts/load_profile.py`
before landing.

## Alerting thresholds

Page on-call when *any* of these fire in production for >5 min:

- Browser WS connect p95 > 500 ms (≈ 20× current baseline; symptom
  of uvicorn/asgi backpressure or DNS issues upstream).
- Browser WS hello p95 > 100 ms (≈ 30× baseline; symptom of TermHub
  state-machine contention or a slow consumer holding the bridge lock).
- Reconnect time after disrupt > 30 s (≈ 10000× baseline; suggests
  a hard failure of session re-attach rather than a latency blip).
- Hijack conflict counter rate > 10/min sustained (broken client
  retry policy, or a malicious client probing for hijack).
- WS upgrade rejected by origin allowlist > 100/hour (someone is
  actively scanning the WS surface from a disallowed origin — could
  be a stale tab or a real probe).
- HTTP 5xx rate > 0.1% over a 5-min window.
- Rollback drill in staging fails (run nightly via cron; alert on
  non-zero exit code).

Each alert should link to the relevant section in
[`runbook.md`](runbook.md).

## How to refresh

Run the probes locally against a live `uterm server`:

```bash
uv run uterm server --config scripts/uterm-server.example.toml &
SERVER_PID=$!
trap 'kill $SERVER_PID' EXIT

# Wait for /api/health = 200, then:
uv run python scripts/load_profile.py \
  --base-url http://127.0.0.1:27780 \
  --worker-id provide-shell --concurrency 5 --rounds 5

uv run python scripts/rollback_drill.py \
  --base-url http://127.0.0.1:27780 \
  --session-id provide-shell \
  --out-dir artifacts/rollback-drill
```

Append the output to `artifacts/load-profile/` and
`artifacts/rollback-drill/` respectively. Compare against the table
above; document any regression in the RC review.
