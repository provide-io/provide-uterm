# On-Call Runbook

This runbook is for incident triage of the hosted terminal server.

## Alert thresholds

- `auth_failures_http_total` or `auth_failures_ws_total` spikes:
  - Warning: > 50 failures in 5 minutes
  - Critical: > 200 failures in 5 minutes
- Elevated server errors:
  - Warning: `http_requests_5xx_total` delta > 20 in 5 minutes
  - Critical: `http_requests_5xx_total` delta > 100 in 5 minutes
- Reconnect instability (from load probes / external monitors):
  - Warning: p95 reconnect > 2.5 s
  - Critical: p99 reconnect > 6.0 s

## Immediate triage

1. Confirm service health:
   - `GET /api/health`
2. Pull request counters:
   - `GET /api/metrics`
   - Focus counters:
     - `hijack_conflicts_total`
     - `hijack_lease_expiries_total`
     - `ws_disconnect_total`
     - `ws_disconnect_worker_total`
     - `ws_disconnect_browser_total`
3. Correlate failing calls with request IDs:
   - use `x-request-id` response header from failed requests
   - filter logs with `request_id=<id>`

## Common incidents

### Auth failures spike

1. Check deployment auth mode/config changes.
2. Verify JWT issuer/audience and system clock skew.
3. Confirm cookie policy changes at edge/proxy.
4. Roll back auth config to last known-good RC if user lockout is ongoing.

### 5xx increase

1. Identify dominant failing endpoint path.
2. Check backend connector/session status for affected sessions.
3. Restart only impacted sessions before considering full service restart.
4. If regression is tied to latest RC, execute rollback drill procedure.

### Reconnect instability

1. Run `scripts/failure_injection.py` against staging.
2. Compare reconnect p95/p99 against SLO targets.
3. If p99 exceeds target, pause promotion and triage connector lifecycle regressions.

## Rollback trigger

Execute rollback when either condition is true:
- Sev1/Sev2 incident persists beyond 15 minutes with no clear mitigation.
- SLO breach persists for 30 minutes after first mitigation attempt.
