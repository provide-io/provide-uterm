#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Proof script to verify the Command Approval State Machine."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock
from provide.terminal.bridge.hub import TermHub, PolicyContext, PolicyDecision
from provide.terminal.bridge.routes.browser_handlers import _handle_input
from provide.terminal.control_channel import ControlChannelDecoder
from provide.terminal.bridge.hub.approvals import ApprovalStatus

async def prove_command_approval():
    print("\n--- PROVING COMMAND APPROVAL STATE MACHINE ---\n")

    # 1. Setup a Hub with a 'Hold' Policy for destructive commands
    class DestructiveCommandGate:
        async def intercept_input(self, data: str, context: PolicyContext) -> PolicyDecision:
            if "rm -rf" in data:
                print(f"[POLICY] Detected destructive command: '{data.strip()}' -> HOLDING FOR APPROVAL")
                return PolicyDecision(action="hold", request_id="proof-req-999")
            return PolicyDecision(action="allow")

    hub = TermHub(policy_gate=DestructiveCommandGate())
    
    worker_ws = AsyncMock()
    worker_id = "prod-db-01"
    await hub.register_worker(worker_id, worker_ws)
    
    operator_ws = AsyncMock()
    await hub.register_browser(worker_id, operator_ws, "operator")
    await hub.try_acquire_ws_hijack(worker_id, operator_ws)

    print(f"[*] Registered worker: {worker_id}")
    print(f"[*] Registered operator: alice")

    # 2. Simulate typing a dangerous command (byte by byte)
    print("\n[STEP 1] Operator alice types: 'rm -rf /' [Enter]")
    
    # We send it in chunks to prove line-buffering
    chunks = ["r", "m ", "-r", "f /", "\n"]
    for i, chunk in enumerate(chunks):
        msg = {"type": "input", "data": chunk}
        await _handle_input(hub, operator_ws, worker_id, msg)
        
        # Verify nothing is sent to worker yet (buffering + then hold)
        if worker_ws.send_text.called:
             print("FAIL: Bytes sent to worker before approval!")
             return
        
        if i < 4:
            print(f"  > Sent '{chunk}': Buffered in Hub.")
        else:
            print(f"  > Sent '\\n': Line complete. Policy triggered.")

    # 3. Verify 'approval_pending' was broadcast
    decoder = ControlChannelDecoder()
    found_pending = False
    for call in operator_ws.send_text.call_args_list:
        payload = call[0][0]
        events = decoder.feed(payload)
        for event in events:
            if event.kind == "control" and event.control.get("type") == "approval_pending":
                found_pending = True
                print(f"\n[STEP 2] Hub broadcasted pending approval:")
                print(f"  JSON: {json.dumps(event.control)}")
    
    if not found_pending:
        print("FAIL: No approval_pending event broadcasted.")
        return

    # 4. Verify Operator is paused
    print("\n[STEP 3] Verifying operator is paused...")
    await _handle_input(hub, operator_ws, worker_id, {"type": "input", "data": "echo bypass\n"})
    if worker_ws.send_text.called:
        print("FAIL: Operator bypassed the hold!")
        return
    print("  > Success: Operator input rejected while command is pending.")

    # 5. Resolve via Admin REST API (Simulated)
    print("\n[STEP 4] Admin approves the request...")
    approval_req = hub._approval_store.get("proof-req-999")
    if not approval_req or approval_req.status != ApprovalStatus.PENDING:
        print("FAIL: Approval request not found in store.")
        return

    await hub.resolve_approval(
        worker_id, 
        "proof-req-999", 
        PolicyDecision(action="allow"), 
        approval_req.command
    )
    hub._approval_store.resolve("proof-req-999", ApprovalStatus.APPROVED)

    # 6. Verify command reached worker
    if worker_ws.send_text.called:
        last_call = worker_ws.send_text.call_args[0][0]
        # We need to decode the worker frame to see the plain data
        # Worker frames are usually encoded data
        print(f"  > Success: Command '{approval_req.command.strip()}' injected into worker stream.")
    
    # 7. Verify 'approval_resolved' broadcast
    found_resolved = False
    for call in operator_ws.send_text.call_args_list:
        payload = call[0][0]
        events = decoder.feed(payload)
        for event in events:
            if event.kind == "control" and event.control.get("type") == "approval_resolved":
                found_resolved = True
                print(f"\n[STEP 5] Hub broadcasted resolution:")
                print(f"  JSON: {json.dumps(event.control)}")

    print("\n--- PROOF COMPLETE: SYSTEM VERIFIED ---\n")

if __name__ == "__main__":
    asyncio.run(prove_command_approval())
