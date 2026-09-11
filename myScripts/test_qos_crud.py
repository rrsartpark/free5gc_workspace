#!/usr/bin/env python3
"""
test_qos_crud.py
----------------
Phase 1 — QoS Subscription CRUD tests.

Tests POST / GET (list) / GET (individual) / PUT / PATCH / DELETE
on the 3gpp-as-session-with-qos/v1 NEF API.

Run BEFORE a UE is connected to verify API structure and error handling,
or WITH a UE connected to test the full provisioning path.

Usage:
    /home/artg/.pyenv/versions/3.10.12/bin/python3 test_qos_crud.py

Configure NEF_BASE_URL and AF_ID below if your setup differs.
"""

import json
import sys
import requests

# ── Configuration ────────────────────────────────────────────────────────────
NEF_BASE_URL  = "http://127.0.0.5:8000/3gpp-as-session-with-qos/v1"
AF_ID         = "test-af-qos"
# UE IP — must match a UE that has an active PDU session.
# PCF's UePool is populated by SMF when the UE establishes a PDU session.
# To find the actual IP: check free5gc.log for "PDU Session Establishment" or
# run: grep -r "ueIpv4Addr\|10.60.0" /var/log/free5gc/ (adjust path as needed) 192.168.70.150
UE_IPV4       = "10.60.0.1"
NOTIFY_URI    = "http://127.0.0.1:5043/notify"

# ── Helpers ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

passed = 0
failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg, detail=""):
    global failed
    failed += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {YELLOW}{detail}{RESET}")

def section(title):
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

def check(condition, pass_msg, fail_msg, detail=""):
    if condition:
        ok(pass_msg)
    else:
        fail(fail_msg, detail)

def _request(method, path, **kwargs):
    url = f"{NEF_BASE_URL}/{path}"
    try:
        return requests.request(method, url, timeout=5, **kwargs)
    except requests.exceptions.Timeout:
        fail(f"Request timed out: {method.upper()} {url}")
        return None
    except requests.exceptions.ConnectionError:
        fail(f"Connection error: Cannot connect to NEF at {url}")
        return None
    except requests.exceptions.RequestException as e:
        fail(f"An unexpected request error occurred: {method.upper()} {url}", str(e))
        return None

def get(path):
    return _request("get", path)

def post(path, body):
    return _request("post", path, json=body)

def put(path, body):
    return _request("put", path, json=body)

def patch(path, body):
    return _request("patch", path, json=body)

def delete(path):
    return _request("delete", path)

# ── Subscription bodies ───────────────────────────────────────────────────────

def base_subscription(ipv4=UE_IPV4):
    """Non-GBR subscription (5QI=7). No gbrDl/gbrUl needed."""
    return {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": ipv4,
        "flowInfo": [
            {
                "flowId": 1,
                "flowDescriptions": [
                    "permit out ip from 203.0.113.10 to assigned",
                    "permit in  ip from assigned to 203.0.113.10"
                ]
            }
        ],
        "5qi": 7,
        "mbrDl": "20000000 bps",
        "mbrUl": "10000000 bps"
    }

def gbr_subscription(ipv4=UE_IPV4):
    """GBR subscription (5QI=1 — Voice). Requires gbrDl + gbrUl."""
    return {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": ipv4,
        "flowInfo": [
            {
                "flowId": 1,
                "flowDescriptions": [
                    "permit out 17 from 203.0.113.20 to assigned",
                    "permit in  17 from assigned to 203.0.113.20"
                ]
            }
        ],
        "5qi":   1,
        "gbrDl": "128000 bps",
        "gbrUl": "64000 bps",
        "mbrDl": "256000 bps",
        "mbrUl": "128000 bps"
    }

# ── Test cases ────────────────────────────────────────────────────────────────

def test_create_non_gbr():
    section("1. POST — Create non-GBR subscription (5QI=7)")
    body = base_subscription()
    r = post(f"{AF_ID}/subscriptions", body)
    if r is not None:
        check(r.status_code == 201,
              f"201 Created (got {r.status_code})",
              f"Expected 201, got {r.status_code}", r.text[:200])
        loc = r.headers.get("Location", "")
        check(bool(loc),
              f"Location header present: {loc}",
              "Location header missing")
        rj = r.json() if r.status_code == 201 else {}
        check("self" in rj or bool(loc),
              "Response contains self link or Location set",
              "No self link in response body")
        return loc
    return ""

def test_create_gbr():
    section("2. POST — Create GBR subscription (5QI=1, Voice)")
    body = gbr_subscription()
    r = post(f"{AF_ID}/subscriptions", body)
    if r is not None:
        check(r.status_code == 201,
              f"201 Created (got {r.status_code})",
              f"Expected 201, got {r.status_code}", r.text[:200])
        return r.headers.get("Location", "")
    return ""

def test_get_list(sub_id):
    section("3. GET — List all subscriptions for AF")
    r = get(f"{AF_ID}/subscriptions")
    if r is not None:
        check(r.status_code == 200,
              f"200 OK (got {r.status_code})",
              f"Expected 200, got {r.status_code}", r.text[:200])
        if r.status_code == 200:
            items = r.json()
            check(isinstance(items, list),
                  f"Response is a list ({len(items)} items)",
                  "Response is not a list")
            if sub_id:
                ids = [s.get("self", "").split("/")[-1] for s in items]
                check(sub_id in ids,
                      f"Created sub {sub_id} appears in list",
                      f"Sub {sub_id} not found in list", str(ids))

def test_get_individual(sub_id):
    section(f"4. GET — Read individual subscription {sub_id}")
    if not sub_id:
        fail("Skipped — no subscription ID from create step")
        return

    r = get(f"{AF_ID}/subscriptions/{sub_id}")
    if r is not None:
        check(r.status_code == 200,
              f"200 OK (got {r.status_code})",
              f"Expected 200, got {r.status_code}", r.text[:200])
        if r.status_code == 200:
            rj = r.json()
            check(str(rj.get("5qi","")) != "",
                  f"5QI present in response: {rj.get('5qi')}",
                  "5QI missing from response")
            check("ueIpv4Addr" in rj or "ueIpv6Addr" in rj,
                  "UE IP address present",
                  "UE IP address missing from response")

def test_put(sub_id):
    section(f"5. PUT — Full replace subscription {sub_id}")
    if not sub_id:
        fail("Skipped — no subscription ID")
        return
    body = base_subscription()
    body["mbrDl"] = "30000000 bps"  # changed value
    r = put(f"{AF_ID}/subscriptions/{sub_id}", body)
    if r is not None:
        check(r.status_code in (200, 204),
              f"200/204 on PUT (got {r.status_code})",
              f"Expected 200 or 204, got {r.status_code}", r.text[:200])
        if r.status_code == 200:
            rj = r.json()
            check(rj.get("mbrDl") == "30000000 bps",
                  "Updated mbrDl reflected in response",
                  f"mbrDl not updated: {rj.get('mbrDl')}")

def test_patch(sub_id):
    section(f"6. PATCH — Partial update subscription {sub_id}")
    if not sub_id:
        fail("Skipped — no subscription ID")
        return
    patch_body = {
        "mbrDl": "15000000 bps",
        "mbrUl": "5000000 bps"
    }
    r = patch(f"{AF_ID}/subscriptions/{sub_id}", patch_body)
    if r is not None:
        check(r.status_code in (200, 204),
              f"200/204 on PATCH (got {r.status_code})",
              f"Expected 200 or 204, got {r.status_code}", r.text[:200])

def test_delete(sub_id):
    section(f"7. DELETE — Remove subscription {sub_id}")
    if not sub_id:
        fail("Skipped — no subscription ID")
        return
    r = delete(f"{AF_ID}/subscriptions/{sub_id}")
    if r is not None:
        check(r.status_code == 204,
              f"204 No Content (got {r.status_code})",
              f"Expected 204, got {r.status_code}", r.text[:200])

def test_get_after_delete(sub_id):
    section(f"8. GET after DELETE — should return 404")
    if not sub_id:
        fail("Skipped — no subscription ID")
        return
    r = get(f"{AF_ID}/subscriptions/{sub_id}")
    if r is not None:
        check(r.status_code == 404,
              f"404 Not Found after delete (got {r.status_code})",
              f"Expected 404, got {r.status_code}", r.text[:200])

def test_get_nonexistent():
    section("9. GET — Non-existent subscription → 404")
    r = get(f"{AF_ID}/subscriptions/does-not-exist-99999")
    if r is not None:
        check(r.status_code == 404,
              f"404 for unknown subscription (got {r.status_code})",
              f"Expected 404, got {r.status_code}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}NEF QoS Subscription — CRUD Tests{RESET}")
    print(f"NEF: {NEF_BASE_URL}")
    print(f"AF:  {AF_ID}  |  UE: {UE_IPV4}")

    # Create and get the subscription ID from Location header
    loc1 = test_create_non_gbr()
    sub_id1 = loc1.rstrip("/").split("/")[-1] if loc1 else ""

    loc2 = test_create_gbr()
    sub_id2 = loc2.rstrip("/").split("/")[-1] if loc2 else ""

    test_get_list(sub_id1)
    test_get_individual(sub_id1)
    test_put(sub_id1)
    test_patch(sub_id1)
    test_delete(sub_id1)
    test_get_after_delete(sub_id1)
    test_get_nonexistent()

    # Clean up second subscription
    if sub_id2:
        delete(f"{AF_ID}/subscriptions/{sub_id2}")

    print(f"\n{'─'*60}")
    total = passed + failed
    print(f"Results: {GREEN}{passed}/{total} passed{RESET}  {RED}{failed}/{total} failed{RESET}")
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()
