#!/usr/bin/env python3
"""
test_qos_e2e.py
---------------
Phase 1 — End-to-end QoS provisioning test.

Steps tested:
  1. POST QoS subscription to NEF  → 201 Created
  2. Verify Location header and subscription ID returned
  3. GET individual subscription   → 200, correct body
  4. PATCH to update MBR           → 200/204
  5. GET again to confirm update   → 200
  6. DELETE subscription           → 204
  7. Confirm deletion              → 404

The test then runs the GBR (5QI=1 Voice) variant of the same flow.

Prerequisites:
  - NEF running at http://127.0.0.5:8000
  - At least one UE connected with a PDU session (UE_IPV4 reachable by PCF)

Configure UE_IPV4 to match the actual UE's IP address in your test environment.

Usage:
    /home/artg/.pyenv/versions/3.10.12/bin/python3 test_qos_e2e.py
"""

import json
import sys
import time
import requests

# ── Configuration ─────────────────────────────────────────────────────────────
NEF_BASE_URL = "http://127.0.0.5:8000/3gpp-as-session-with-qos/v1"
AF_ID        = "test-af-e2e"

# Set this to the actual UE IP address when a UE is registered in the network.
# If no UE is connected, steps 1 and 6 will still return 201/204 (NEF stores the
# subscription) but PCF-side QoS provisioning will fail silently or return an error
# from PCF — check PCF logs in that case.
UE_IPV4      = "10.60.0.1"

NOTIFY_URI   = "http://127.0.0.1:5043/notify"

# Remote server address (used in flow descriptions)
REMOTE_IP    = "1.1.1.1/32"

# ── Helpers ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

passed = failed = 0
step_n = [0]

def ok(msg):
    global passed; passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg, detail=""):
    global failed; failed += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {YELLOW}{detail}{RESET}")

def check(condition, pass_msg, fail_msg, detail=""):
    if condition:
        ok(pass_msg)
    else:
        fail(fail_msg, detail)

def step(title):
    step_n[0] += 1
    print(f"\n{CYAN}Step {step_n[0]}: {title}{RESET}")

def divider(title):
    step_n[0] = 0
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")

def pretty(r):
    try:
        return json.dumps(r.json(), indent=4)
    except Exception:
        return r.text[:400]

# ── HTTP wrappers ─────────────────────────────────────────────────────────────

def _request(method, url, **kwargs):
    try:
        return requests.request(method, url, timeout=5, **kwargs)
    except requests.exceptions.Timeout:
        fail(f"Request timed out: {method.upper()} {url}")
    except requests.exceptions.ConnectionError:
        fail(f"Connection error: Cannot connect to NEF at {url}")
    except requests.exceptions.RequestException as e:
        fail(f"An unexpected request error occurred: {method.upper()} {url}", str(e))
    return None

def post_sub(body):
    return _request("post", f"{NEF_BASE_URL}/{AF_ID}/subscriptions", json=body)

def get_sub(sub_id):
    return _request("get", f"{NEF_BASE_URL}/{AF_ID}/subscriptions/{sub_id}")

def patch_sub(sub_id, body):
    return _request("patch", f"{NEF_BASE_URL}/{AF_ID}/subscriptions/{sub_id}", json=body)

def delete_sub(sub_id):
    return _request("delete", f"{NEF_BASE_URL}/{AF_ID}/subscriptions/{sub_id}")

# ── E2E flow ──────────────────────────────────────────────────────────────────

def run_e2e(label, subscription_body, patch_body):
    divider(f"E2E Flow — {label}")

    # Step 1: Create
    step("POST — Create subscription")
    r = post_sub(subscription_body)
    check(r.status_code == 201,
          f"201 Created  (status={r.status_code})",
          f"Expected 201, got {r.status_code}", pretty(r))
    if not r or r.status_code != 201:
        fail("Cannot continue — subscription not created")
        # Exit if creation fails, as subsequent steps depend on it
        sys.exit(1)

    loc = r.headers.get("Location", "")
    sub_id = loc.rstrip("/").split("/")[-1] if loc else ""
    print(f"    {YELLOW}→ sub_id = {sub_id}{RESET}")
    print(f"    {YELLOW}→ Location: {loc}{RESET}")

    check(bool(sub_id),
          "Subscription ID extracted from Location header",
          "Could not extract subscription ID", f"Location: {loc}")
    if not sub_id:
        sys.exit(1)

    # Step 2: Verify response body
    step("Verify POST response body")
    try:
        rj = r.json()
        check("5qi" in rj or "flowInfo" in rj,
              "Response body contains QoS fields",
              "Response body missing expected fields", str(rj.keys()))
    except Exception:
        fail("Response body is not valid JSON", r.text[:200])

    # Step 3: Read back
    step("GET — Read individual subscription")
    time.sleep(0.2)   # small pause to allow context to be stored
    r2 = get_sub(sub_id)
    if r2 is not None:
        check(r2.status_code == 200,
              f"200 OK (got {r2.status_code})",
              f"Expected 200, got {r2.status_code}", r2.text[:200])
        if r2.status_code == 200:
            rj2 = r2.json()
            check(rj2.get("ueIpv4Addr") == UE_IPV4,
                  f"ueIpv4Addr matches ({UE_IPV4})",
                  f"ueIpv4Addr mismatch: {rj2.get('ueIpv4Addr')}")

    # Step 4: Patch
    step("PATCH — Update MBR")
    r3 = patch_sub(sub_id, patch_body)
    if r3 is not None:
        check(r3.status_code in (200, 204),
              f"200/204 on PATCH (got {r3.status_code})",
              f"Expected 200 or 204, got {r3.status_code}", r3.text[:200])

    # Step 5: Read after patch
    if r3 and r3.status_code in (200, 204):
        step("GET — Verify patch was applied")
        r4 = get_sub(sub_id)
        if r4 and r4.status_code == 200 and patch_body.get("mbrDl"):
            rj4 = r4.json()
            check(rj4.get("mbrDl") == patch_body["mbrDl"],
                  f"mbrDl updated to {patch_body['mbrDl']}",
                  f"mbrDl not updated: {rj4.get('mbrDl')}")
    
    print("Waiting to delete ...")
    time.sleep(10)

    # Step 6: Delete
    step("DELETE — Remove subscription")
    r5 = delete_sub(sub_id)
    if r5 is not None:
        check(r5.status_code == 204,
              f"204 No Content (got {r5.status_code})",
              f"Expected 204, got {r5.status_code}", r5.text[:200])

    # Step 7: Confirm gone
    step("GET — Confirm 404 after delete")
    time.sleep(0.1)
    r6 = get_sub(sub_id)
    if r6 is not None:
        check(r6.status_code == 404,
              f"404 Not Found after delete (got {r6.status_code})",
              f"Expected 404, got {r6.status_code}", r6.text[:200])


# ── Test scenarios ────────────────────────────────────────────────────────────

NON_GBR_BODY = {
    "notificationDestination": NOTIFY_URI,
    "ueIpv4Addr": UE_IPV4,
    "flowInfo": [
        {
            "flowId": 1,
            "flowDescriptions": [
                f"permit out 6 from {REMOTE_IP} to assigned",
                f"permit in  6 from assigned to {REMOTE_IP}"
            ]
        }
    ],
    "5qi": 7,
    "mbrDl": "20000000 bps",
    "mbrUl": "10000000 bps"
}

NON_GBR_PATCH = {
    "mbrDl": "25000000 bps",
    "mbrUl": "12000000 bps"
}

GBR_BODY = {
    "notificationDestination": NOTIFY_URI,
    "ueIpv4Addr": UE_IPV4,
    "flowInfo": [
        {
            "flowId": 2,
            "flowDescriptions": [
                f"permit out 17 from {REMOTE_IP} to assigned",
                f"permit in  17 from assigned to {REMOTE_IP}"
            ]
        }
    ],
    "5qi":   1,
    "gbrDl": "64000 bps",
    "gbrUl": "32000 bps",
    "mbrDl": "128000 bps",
    "mbrUl": "64000 bps"
}

GBR_PATCH = {
    "gbrDl": "96000 bps",
    "gbrUl": "48000 bps",
    "mbrDl": "192000 bps",
    "mbrUl": "96000 bps"
}

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}NEF QoS Subscription — End-to-End Tests{RESET}")
    print(f"NEF: {NEF_BASE_URL}")
    print(f"AF:  {AF_ID}  |  UE: {UE_IPV4}")
    print(f"\n{YELLOW}Note: UE must be connected with an active PDU session{RESET}")
    print(f"{YELLOW}      for the PCF/SMF provisioning steps to succeed.{RESET}")
    print(f"{YELLOW}      NEF-side CRUD will work regardless.{RESET}")

    run_e2e("Non-GBR (5QI=7)", NON_GBR_BODY, NON_GBR_PATCH)
    time.sleep(10)  # brief pause between tests
    run_e2e("GBR Voice (5QI=1)", GBR_BODY, GBR_PATCH)

    print(f"\n{'═'*60}")
    total = passed + failed
    print(f"Results: {GREEN}{passed}/{total} passed{RESET}  {RED}{failed}/{total} failed{RESET}")
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()
