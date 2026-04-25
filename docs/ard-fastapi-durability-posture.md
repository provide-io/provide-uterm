# ARD: FastAPI Backend Durability Posture

## Status

Accepted

## Problem

The FastAPI backend is the reference self-hosted deployment for provide-terminal, but it currently keeps control-plane state in process memory. That includes tunnel/share tokens, approvals, resume state, webhook registrations, live session arbitration, and hijack ownership.

That posture is acceptable for a single active instance. It is not safe to treat the FastAPI backend as a durable or active-active control plane behind a load balancer, because restart or failover can drop in-flight state and split ownership decisions across processes.

## Current Behavior

The repo already documents the intended operating model in `README.md`:

- FastAPI is the full-control self-hosted backend.
- The control plane state is in memory only.
- The supported operating mode is a single active instance.
- Cloudflare Workers + Durable Objects is the durability/HA option when the deployment needs multi-node persistence.

In practice, the FastAPI backend is currently a reference implementation with ephemeral control-plane state. It can serve production traffic, but only if the deployment topology preserves one active process for the control plane.

## Decision

Keep FastAPI as a reference / single-active-instance control plane.

Do not invest in HA semantics for the FastAPI backend at this layer. If a deployment needs durable session arbitration, failover-safe approvals, or multi-node control state, route that deployment to the Cloudflare Workers + Durable Objects backend instead.

## Options Considered

### 1. Keep FastAPI single-active, with in-process control state

This matches the current implementation and keeps the backend simple.

Pros:
- No new storage or leader-election infrastructure.
- Lowest operational and implementation complexity.
- Matches the current test and docs footprint.

Cons:
- Restart/failover loses live approvals, lease state, resume state, and webhook registrations.
- No horizontal scaling for the control plane.
- A load balancer can create split-brain behavior if multiple FastAPI instances are treated as interchangeable.

### 2. Make FastAPI durable by externalizing state

Move control-plane state into a shared durable store and add coordination for lease ownership and failover.

Pros:
- Could support multi-instance deployment.
- Could preserve some state across restarts.

Cons:
- Large scope increase: storage schema, migrations, consistency rules, lease fencing, cleanup jobs, and recovery behavior.
- Would need careful handling for races on hijack ownership, approval callbacks, and resume token rotation.
- Duplicates the durability work already solved by the Cloudflare backend.

### 3. Rebuild FastAPI as an HA control plane

Treat FastAPI as a full active-active control plane with leader election and distributed state.

Pros:
- Operationally flexible if completed.

Cons:
- Highest complexity and highest risk.
- Hard to make correct for session leases and browser resumption.
- Not justified for the self-hosted reference backend while Cloudflare already provides the durable option.

## Recommendation

Use option 1.

The FastAPI backend should remain the reference implementation and single-active deployment target. The docs should say that explicitly so operators do not infer HA semantics from the presence of a web server, reverse proxy, or multiple app workers.

## Consequences

- Deployment guidance must say: run one active FastAPI control-plane instance, or use Cloudflare for HA.
- Health checks and orchestration should avoid active-active load balancing for FastAPI control state.
- Any future HA work on FastAPI must be treated as a separate architectural decision, not as an implicit backend enhancement.
- Tests should continue to assume that FastAPI state can disappear on restart unless a specific store is configured for a narrower feature.

## Risks

- Operators may still place FastAPI behind a load balancer and expect failover to be safe.
- Ephemeral state loss can break active approvals or ownership leases if the process restarts mid-session.
- This posture is easy to misunderstand because the backend is otherwise production-capable.

## Mitigations

- Keep the durability note in `README.md` explicit and concrete.
- Cross-link the protocol matrix so backend capability differences are visible before deployment.
- Document the Cloudflare backend as the durable alternative rather than implying feature parity where durability differs.

## Verification

- Confirm the README durability note remains accurate after backend changes.
- Keep tests that depend on ephemeral control-plane state scoped to the FastAPI backend.
- Ensure any future storage-backed FastAPI work has its own ADR with a clear migration and consistency model.
