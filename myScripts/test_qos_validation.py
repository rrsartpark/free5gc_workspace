#!/usr/bin/env python3
"""
test_qos_validation.py
-----------------------
Phase 1 — Input validation tests for the QoS subscription API.

Verifies NEF returns HTTP 400 for malformed / incomplete requests,
and HTTP 201 for correct requests.

All tests are stateless (each creates/deletes its own subscription).

Usage:
    /home/artg/.pyenv/versions/3.10.12/bin/python3 test_qos_validation.py
"""

import sys
import requests

# ── Configuration ─────────────────────────────────────────────────────────────
NEF_BASE_URL = "http://127.0.0.5:8000/3gpp-as-session-with-qos/v1"
AF_ID        = "test-af-validation"
UE_IPV4      = "10.60.0.1"
NOTIFY_URI   = "http://127.0.0.1:5043/notify"

# ── Helpers ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

passed = failed = 0

def ok(msg):
    global passed; passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg, detail=""):
    global failed; failed += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {YELLOW}{detail}{RESET}")

def check(condition, pass_msg, fail_msg, detail=""):
    # (ok if condition else fail)(pass_msg if condition else fail_msg, detail if not condition else "")
    # print("Condition: ", condition)
    # print("Pass msg: ", pass_msg)
    # print("Fail msg: ", fail_msg)
    # print("Detail: ", detail)
    if condition:
        ok(pass_msg)
    else:
        fail(fail_msg, detail)

def section(title):
    print(f"\n{BOLD}{'─'*60}{RESET}\n{BOLD}{title}{RESET}\n{BOLD}{'─'*60}{RESET}")

def post(body):
    try:
        r = requests.post(f"{NEF_BASE_URL}/{AF_ID}/subscriptions", json=body, timeout=5)
        return r
    except requests.ConnectionError as e:
        fail("Cannot connect to NEF", str(e))
        return None

def cleanup(loc):
    if loc:
        sub_id = loc.rstrip("/").split("/")[-1]
        requests.delete(f"{NEF_BASE_URL}/{AF_ID}/subscriptions/{sub_id}", timeout=5)

# ── Valid base body ────────────────────────────────────────────────────────────

VALID_FLOW = [{"flowId": 1, "flowDescriptions": [
    "permit out ip from 198.51.100.1 to assigned",
    "permit in  ip from assigned to 198.51.100.1"
]}]

VALID_BODY = {
    "notificationDestination": NOTIFY_URI,
    "ueIpv4Addr": UE_IPV4,
    "flowInfo": VALID_FLOW,
    "5qi": 7,
    "mbrDl": "10000000 bps",
    "mbrUl": "5000000 bps"
}

# ── Test cases ────────────────────────────────────────────────────────────────

def test_valid():
    section("1. Valid non-GBR request → 201")
    r = post(VALID_BODY)
    if r is not None:
        check(r.status_code == 201,
              f"201 Created (got {r.status_code})",
              f"Expected 201, got {r.status_code}", r.text[:200])
        cleanup(r.headers.get("Location", ""))

def test_missing_ue_ip():
    section("2. Missing UE IP (no ueIpv4Addr, no ueIpv6Addr) → 400")
    body = {k: v for k, v in VALID_BODY.items() if k not in ("ueIpv4Addr", "ueIpv6Addr")}
    r = post(body)
    if r is not None:
        check(r.status_code == 400,
              f"400 Bad Request (got {r.status_code})",
              f"Expected 400, got {r.status_code}", r.text[:200])

def test_missing_flow_info():
    section("3. Missing flowInfo and ethFlowInfo → 400")
    body = {k: v for k, v in VALID_BODY.items() if k not in ("flowInfo", "ethFlowInfo")}
    r = post(body)
    if r is not None:
        check(r.status_code == 400,
              f"400 Bad Request (got {r.status_code})",
              f"Expected 400, got {r.status_code}", r.text[:200])

def test_missing_notification_dest():
    section("4. Missing notificationDestination → 400")
    body = {k: v for k, v in VALID_BODY.items() if k != "notificationDestination"}
    r = post(body)
    if r is not None:
        check(r.status_code == 400,
              f"400 Bad Request (got {r.status_code})",
              f"Expected 400, got {r.status_code}", r.text[:200])

def test_5qi_zero():
    section("5. 5QI = 0 (out of range) → 400")
    body = {**VALID_BODY, "5qi": 0}
    r = post(body)
    if r is not None:
        check(r.status_code == 400,
              f"400 for 5QI=0 (got {r.status_code})",
              f"Expected 400, got {r.status_code}", r.text[:200])

def test_5qi_256():
    section("6. 5QI = 256 (out of range) → 400")
    body = {**VALID_BODY, "5qi": 256}
    r = post(body)
    if r is not None:
        check(r.status_code == 400,
              f"400 for 5QI=256 (got {r.status_code})",
              f"Expected 400, got {r.status_code}", r.text[:200])

def test_gbr_5qi_without_gbr_values():
    section("7. GBR 5QI=1 without gbrDl / gbrUl → 400")
    body = {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": UE_IPV4,
        "flowInfo": VALID_FLOW,
        "5qi":   1,      # GBR 5QI — requires gbrDl + gbrUl
        "mbrDl": "256000 bps",
        "mbrUl": "128000 bps"
        # gbrDl and gbrUl intentionally omitted
    }
    r = post(body)
    if r is not None:
        check(r.status_code == 400,
              f"400 — GBR 5QI without GBR values (got {r.status_code})",
              f"Expected 400, got {r.status_code}", r.text[:200])

def test_gbr_5qi_with_gbr_values():
    section("8. GBR 5QI=1 WITH gbrDl + gbrUl → 201")
    body = {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": UE_IPV4,
        "flowInfo": VALID_FLOW,
        "5qi":   1,
        "gbrDl": "64000 bps",
        "gbrUl": "32000 bps",
        "mbrDl": "128000 bps",
        "mbrUl": "64000 bps"
    }
    r = post(body)
    if r is not None:
        check(r.status_code == 201,
              f"201 Created for valid GBR (got {r.status_code})",
              f"Expected 201, got {r.status_code}", r.text[:200])
        cleanup(r.headers.get("Location", ""))

def test_gbr_5qi_65():
    section("9. GBR 5QI=65 (Mission Critical Push-to-talk) WITH GBR values → 201")
    body = {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": UE_IPV4,
        "flowInfo": VALID_FLOW,
        "5qi":   65,
        "gbrDl": "32000 bps",
        "gbrUl": "32000 bps",
        "mbrDl": "64000 bps",
        "mbrUl": "64000 bps"
    }
    r = post(body)
    if r is not None:
        check(r.status_code == 201,
              f"201 for 5QI=65 (got {r.status_code})",
              f"Expected 201, got {r.status_code}", r.text[:200])
        cleanup(r.headers.get("Location", ""))

def test_ipv6_only():
    section("10. IPv6-only UE identification → 201")
    body = {
        "notificationDestination": NOTIFY_URI,
        "ueIpv6Addr": "2001:db8::1",
        "flowInfo": VALID_FLOW,
        "5qi": 9,
        "mbrDl": "50000000 bps",
        "mbrUl": "25000000 bps"
    }
    r = post(body)
    if r is not None:
        check(r.status_code in (201, 404),
              f"201 (UE found) or 404 (no IPv6 session) — got {r.status_code}",
              f"Unexpected status {r.status_code}", r.text[:200])
        if r.status_code == 201:
            cleanup(r.headers.get("Location", ""))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}NEF QoS Subscription — Input Validation Tests{RESET}")
    print(f"NEF: {NEF_BASE_URL}")
    print(f"AF:  {AF_ID}  |  UE: {UE_IPV4}")

    test_valid()
    test_missing_ue_ip()
    test_missing_flow_info()
    test_missing_notification_dest()
    test_5qi_zero()
    test_5qi_256()
    test_gbr_5qi_without_gbr_values()
    test_gbr_5qi_with_gbr_values()
    test_gbr_5qi_65()
    test_ipv6_only()

    print(f"\n{'─'*60}")
    total = passed + failed
    print(f"Results: {GREEN}{passed}/{total} passed{RESET}  {RED}{failed}/{total} failed{RESET}")
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()
