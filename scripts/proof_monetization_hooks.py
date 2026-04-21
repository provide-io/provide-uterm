#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Proof of Concept: Mock Fleet Manager to verify Node discovery and policy hooks."""

import json

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="Mock Fleet Manager")


@app.post("/discovery")
async def discovery(request: Request):
    data = await request.json()
    print(
        f"\n[DISCOVERY] Node '{data.get('node_id')}' reporting: {data.get('active_sessions')} sessions, {data.get('worker_count')} workers"
    )
    return {"status": "ok"}


@app.post("/telemetry")
async def telemetry(request: Request):
    data = await request.json()
    # DAS Event logging
    event = data.get("event")
    payload = data.get("data", {})
    print(f"[TELEMETRY] Event: {event} | {json.dumps(payload)}")
    return {"status": "ok"}


@app.post("/policy/spawn")
async def policy_spawn(request: Request):
    data = await request.json()
    agent_id = data.get("agent_id")
    # Example logic: Allow all but specific IDs
    allow = agent_id != "blocked-agent"
    print(f"[POLICY] Agent Spawn Check: {agent_id} -> {'ALLOWED' if allow else 'REJECTED'}")
    return {"allow": allow}


@app.post("/policy/input")
async def policy_input(request: Request):
    data = await request.json()
    input_data = data.get("data", "")
    # Example: Block common destructive commands
    allow = "rm -rf" not in input_data
    print(f"[POLICY] Input Intercept: '{input_data.strip()}' -> {'ALLOWED' if allow else 'REJECTED'}")
    return {"allow": allow}


@app.post("/authz")
async def authz(request: Request):
    data = await request.json()
    action = data.get("action")
    subject = data.get("principal", {}).get("subject_id")
    # Example: Simple external authz decision
    allow = True
    print(f"[AUTHZ] Decision for {subject} on {action} -> ALLOWED")
    return {"allow": allow}


if __name__ == "__main__":
    print("Mock Fleet Manager starting on port 8888...")
    print("Point your uterm-server to these endpoints in server.toml to see the hooks in action.")
    uvicorn.run(app, port=8888, log_level="warning")
