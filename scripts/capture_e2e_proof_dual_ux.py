import os
import subprocess
import time

from playwright.sync_api import sync_playwright


def run_proof():
    print("\n--- FINAL DUAL-UX E2E PROOF ---\n")

    # 1. Start Mock External Management Tier
    fleet_code = """
from fastapi import FastAPI, Request
import uvicorn
app = FastAPI()
@app.post("/policy")
async def policy(request: Request):
    data = await request.json()
    cmd = data.get("data", "")
    if "rm -rf" in cmd:
        return {"action": "hold", "request_id": "proof-123", "timeout_s": 60}
    return {"action": "allow"}
uvicorn.run(app, port=8888)
"""
    with open("mock_fleet.py", "w") as f:
        f.write(fleet_code)

    fleet_proc = subprocess.Popen(["python3", "mock_fleet.py"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(2)

    # 2. Setup Server Config
    config_content = """
[server]
port = 8000
[auth]
mode = "dev"
[governance]
policy_webhook_url = "http://localhost:8888/policy"
[[sessions]]
session_id = "proof-session"
display_name = "Dual UX Proof"
connector_type = "shell"
visibility = "public"
"""
    with open("proof-ux.toml", "w") as f:
        f.write(config_content)

    server_proc = subprocess.Popen(
        ["uv", "run", "uterm-server", "--config", "proof-ux.toml", "--port", "8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    time.sleep(5)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # --- SCENARIO 1: OPERATOR VIEW (Status Bar) ---
            print("[*] Testing Operator View (Status Bar)...")
            op_context = browser.new_context(
                extra_http_headers={"x-uterm-principal": "operator-alice", "x-uterm-role": "operator"}
            )
            op_page = op_context.new_page()
            op_page.goto("http://localhost:8000/app/operator/proof-session")
            op_page.wait_for_timeout(5000)  # Wait for terminal load

            input_sel = "input.hijack-input-field"
            op_page.wait_for_selector(input_sel)
            op_page.fill(input_sel, "rm -rf /")
            op_page.keyboard.press("Enter")
            op_page.wait_for_timeout(3000)

            # Check for Status Bar
            if op_page.query_selector(".hijack-approval-statusbar"):
                print("[+] SUCCESS: Operator sees Status Bar.")
            else:
                print("[-] FAIL: Status Bar not found for Operator.")
            op_page.screenshot(path="proof_op_statusbar.png")

            # --- SCENARIO 2: ADMIN VIEW (Modal) ---
            print("[*] Testing Admin View (Modal)...")
            adm_context = browser.new_context(
                extra_http_headers={"x-uterm-principal": "admin-bob", "x-uterm-role": "admin"}
            )
            adm_page = adm_context.new_page()
            adm_page.goto("http://localhost:8000/app/operator/proof-session")
            adm_page.wait_for_timeout(5000)

            # Modal should be visible automatically because there is a pending request
            if adm_page.query_selector(".hijack-approval-modal"):
                print("[+] SUCCESS: Admin sees Modal.")
                if adm_page.query_selector(".hijack-btn-approve"):
                    print("[+] SUCCESS: Admin sees Approve/Reject buttons.")
                else:
                    print("[-] FAIL: Admin buttons missing from Modal.")
            else:
                print("[-] FAIL: Modal not found for Admin.")
            adm_page.screenshot(path="proof_admin_modal.png")

            # --- RESOLUTION ---
            print("[*] Admin click Reject...")
            adm_page.click(".hijack-btn-reject")
            adm_page.wait_for_timeout(2000)

            if not adm_page.query_selector(".hijack-approval-modal"):
                print("[+] SUCCESS: Modal dismissed after resolution.")

            if not op_page.query_selector(".hijack-approval-statusbar"):
                print("[+] SUCCESS: Status Bar dismissed for Operator.")

            browser.close()
    finally:
        server_proc.terminate()
        fleet_proc.terminate()
        if os.path.exists("mock_fleet.py"):
            os.remove("mock_fleet.py")
        if os.path.exists("proof-ux.toml"):
            os.remove("proof-ux.toml")
        print("\n--- PROOF COMPLETE ---\n")


if __name__ == "__main__":
    run_proof()
