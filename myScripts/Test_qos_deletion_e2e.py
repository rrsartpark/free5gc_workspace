#!/usr/bin/env python3
"""
test_qos_deletion_e2e.py
------------------------
Phase 2 — AF-requested QoS flow DELETION via NEF (Nnef_AFsessionWithQoS_Revoke).

Companion to test_qos_e2e.py (same conventions, helpers, and environment).
Covers the northbound API contract (diagram Phases 1-3) plus regression tests
that catch the control-plane leaks documented in
QoS_Flow_Deletion_Gap_Analysis_and_Plan.md.

Tests:
  T1  Delete happy path (non-GBR)   create → DELETE 204 → GET 404 → absent from collection
  T2  Delete idempotency            second DELETE on same sub → 404
  T3  Delete non-existent sub       DELETE random subID → 404
  T4  AF isolation                  DELETE under wrong afID → 404, sub survives under owner
  T5  GBR budget restore            create GBR → delete → re-create GBR x3 (catches
                                    PCF RemainGbr leak / IncreaseRemainGBR regression)
  T6  Create/delete churn           N cycles of create+delete (catches PCF AppSession
                                    leaks; N > 62 also crosses the SMF QFIGenerator's
                                    wraparound point and N > 254 the NAS rule-ID
                                    generator's — both are round-robin allocators that
                                    reuse freed IDs once the range wraps, so a genuine
                                    regression here means "exhausted", not "slow to reuse")
  T7  Multi-sub isolation           two subs, delete one, other must stay intact
  T8  Delete after PATCH            create → PATCH MBR → DELETE → 404 (updated
                                    AppSession must still tear down cleanly)
  T9  Strict delete w/ PCF down     OPERATOR-GUIDED, run separately with --pcf-down:
                                    create → (operator stops PCF) → DELETE must return
                                    5xx and RETAIN the sub → (operator restarts PCF) →
                                    retry DELETE → 204. Validates the strict-mode NEF
                                    change (gap-analysis §4.7, work item 2F). FAILS by
                                    design on today's best-effort NEF.
  T10 Create rejected, no QoS flow  POST with a ueIpv4Addr matching no PDU session →
                                    500 PDU_SESSION_NOT_AVAILABLE, nothing stored, then
                                    shows the delete-side is exactly T3. See "No QoS
                                    flow?" below — this is the answer to that question.

Independent of test_qos_e2e.py?
  Yes. No shared state, no file dependency, different AF_ID ("test-af-del" vs
  "test-af-e2e"). Both files are pure NEF-API clients — neither attaches a UE
  or creates a PDU session. What they DO share is an environment precondition:
  T1, T4, T5, T7, T8, T9 need a UE with an active PDU session at UE_IPV4
  already up (via UERANSIM or a real device) BEFORE this script runs — that
  attachment doesn't come from either test script. Without it, every create
  in this file fails and those tests are SKIPPED, not aborted (see below).
  T2, T3, T10 don't need a live UE and always run.

Does this file check that a QoS flow actually exists? What if there isn't one?
  Every assertion here is against NEF's HTTP response code — a 201 from POST
  means "NEF+PCF accepted and stored the request", not proof that SMF applied
  the PCC rule or that a QFI/DRB exists over the air (that needs the pcap/UE
  checks in the gap-analysis doc's §7 verification guide).
  Whether a *subscription with no real flow behind it* can exist to delete is
  answered by T10: NEF's create is strict (verified directly in
  NFs/nef/internal/sbi/processor/qos.go, PostQoSSubscription) — it calls PCF's
  Npcf_PolicyAuthorization Create synchronously and only stores the local
  subscription if that succeeds, which itself requires PCF's SessionBinding
  to find a PDU session matching ueIpv4Addr (NFs/pcf/.../policyauthorization.go).
  So a flow-less subscription can never be created through the API — T10
  confirms the rejection (500/PDU_SESSION_NOT_AVAILABLE, no orphan stored),
  and its delete-side collapses to T3 (DELETE on a subID that never
  existed → 404) because there is nothing else it could collapse to.

What this file can NOT verify (needs pcap / UE / UPF inspection — see §7 of the
gap-analysis doc): NAS delete op codes, NGAP QosFlowToReleaseList, PFCP
Remove PDR/FAR/QER, RAN DRB release. Run those checks manually after Phase 2/3.

Prerequisites:
  - NEF running at http://127.0.0.5:8000
  - For T1, T4, T5, T7, T8, T9: one UE already registered with an active PDU
    session whose IP matches UE_IPV4. T2, T3, T10 work with no UE at all.

Usage:
    python3 test_qos_deletion_e2e.py [churn_cycles]
        churn_cycles  optional int, default 70 — crosses the QFIGenerator's
                      62-ID wraparound at least once (2..63 range) so a
                      genuine reuse regression would surface; use 260+ to
                      also cross the NAS rule-ID generator's 255-ID range.
    python3 test_qos_deletion_e2e.py --pcf-down
        Runs ONLY the operator-guided strict-mode scenario (T9).
"""

import json
import sys
import time
import uuid
import requests

# ── Configuration ─────────────────────────────────────────────────────────────
NEF_BASE_URL = "http://127.0.0.5:8000/3gpp-as-session-with-qos/v1"
AF_ID        = "test-af-del"
OTHER_AF_ID  = "test-af-del-intruder"

UE_IPV4      = "10.60.0.1"
NOTIFY_URI   = "http://127.0.0.1:5043/notify"
REMOTE_IP    = "1.1.1.1/32"
REMOTE_IP_B  = "8.8.8.8/32"

# Deletion is confirmed to the AF before PCF→SMF propagation completes
# (PCF notifies SMF in a goroutine). Small settle time between steps.
PROPAGATION_WAIT = 1.0

CHURN_CYCLES_DEFAULT = 70

# RFC 5737 TEST-NET-1 — reserved, will never be a real UE address on this
# network's 10.60.0.0/24 pool. Used by T10 so that test is deterministic
# regardless of whether the "real" UE at UE_IPV4 happens to be attached.
NO_UE_TEST_IP = "192.0.2.44"

# ── Helpers (same conventions as test_qos_e2e.py) ─────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

passed = failed = skipped = 0
step_n = [0]

def ok(msg):
    global passed; passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg, detail=""):
    global failed; failed += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {YELLOW}{detail}{RESET}")

def skip(msg):
    """Note a step/test as skipped — does not count as pass or fail."""
    global skipped; skipped += 1
    print(f"  {YELLOW}⊘{RESET} SKIPPED — {msg}")

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
    if r is None:
        return "<no response>"
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

def post_sub(body, af_id=AF_ID):
    return _request("post", f"{NEF_BASE_URL}/{af_id}/subscriptions", json=body)

def get_subs(af_id=AF_ID):
    return _request("get", f"{NEF_BASE_URL}/{af_id}/subscriptions")

def get_sub(sub_id, af_id=AF_ID):
    return _request("get", f"{NEF_BASE_URL}/{af_id}/subscriptions/{sub_id}")

def patch_sub(sub_id, body, af_id=AF_ID):
    return _request("patch", f"{NEF_BASE_URL}/{af_id}/subscriptions/{sub_id}", json=body)

def delete_sub(sub_id, af_id=AF_ID):
    return _request("delete", f"{NEF_BASE_URL}/{af_id}/subscriptions/{sub_id}")

# ── Bodies ────────────────────────────────────────────────────────────────────

def non_gbr_body(flow_id=1, remote_ip=REMOTE_IP):
    return {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": UE_IPV4,
        "flowInfo": [
            {
                "flowId": flow_id,
                "flowDescriptions": [
                    f"permit out 6 from {remote_ip} to assigned",
                    f"permit in  6 from assigned to {remote_ip}",
                ],
            }
        ],
        "5qi": 7,
        "mbrDl": "20000000 bps",
        "mbrUl": "10000000 bps",
    }

def gbr_body(flow_id=2, remote_ip=REMOTE_IP):
    return {
        "notificationDestination": NOTIFY_URI,
        "ueIpv4Addr": UE_IPV4,
        "flowInfo": [
            {
                "flowId": flow_id,
                "flowDescriptions": [
                    f"permit out 17 from {remote_ip} to assigned",
                    f"permit in  17 from assigned to {remote_ip}",
                ],
            }
        ],
        "5qi":   1,
        "gbrDl": "64000 bps",
        "gbrUl": "32000 bps",
        "mbrDl": "128000 bps",
        "mbrUl": "64000 bps",
    }

# ── Shared actions ────────────────────────────────────────────────────────────

_ue_hint_shown = [False]

def warn_no_ue_hint():
    """One-time hint when a create fails — the common cause in this suite is
    no live UE/PDU session at UE_IPV4 (see header docstring for why)."""
    if _ue_hint_shown[0]:
        return
    _ue_hint_shown[0] = True
    print(f"\n{YELLOW}  Hint: creation fails without a live UE/PDU session at "
          f"UE_IPV4={UE_IPV4}.{RESET}")
    print(f"{YELLOW}  NEF's create is strict — it will not store a subscription "
          f"unless PCF finds a matching session (SessionBinding). Attach a UE "
          f"(UERANSIM or real) with a PDU session at that IP, or update "
          f"UE_IPV4, then re-run. T2, T3, T10 don't need this and still ran.{RESET}\n")

def create_sub_or_skip(body, label, af_id=AF_ID):
    """POST a subscription. Returns the sub_id, or None if creation failed —
    callers must handle None by skipping their own remaining steps via
    skip(), not by aborting the whole suite."""
    r = post_sub(body, af_id=af_id)
    if r is None or r.status_code != 201:
        fail(f"{label} creation failed (status={getattr(r, 'status_code', 'n/a')})", pretty(r))
        warn_no_ue_hint()
        return None
    loc = r.headers.get("Location", "")
    sub_id = loc.rstrip("/").split("/")[-1] if loc else ""
    if not sub_id:
        fail("No subscription ID in Location header", f"Location: {loc}")
        return None
    ok(f"{label} created (sub_id={sub_id})")
    return sub_id

def sub_ids_in_collection(af_id=AF_ID):
    """Return the set of self-URI sub IDs in the collection (handles null body)."""
    r = get_subs(af_id)
    if r is None or r.status_code != 200:
        return None
    try:
        body = r.json()
    except Exception:
        return None
    if body is None:          # NEF returns `null` when the AF has no QoS subs
        return set()
    ids = set()
    for item in body:
        self_uri = (item or {}).get("self", "")
        if self_uri:
            ids.add(self_uri.rstrip("/").split("/")[-1])
    return ids

# ── Tests ─────────────────────────────────────────────────────────────────────

def t1_delete_happy_path():
    divider("T1 — Delete happy path (non-GBR, 5QI=7)")

    step("POST — Create subscription")
    sub_id = create_sub_or_skip(non_gbr_body(), "non-GBR subscription")
    if sub_id is None:
        skip("T1 needs a live UE/PDU session at UE_IPV4")
        return None
    time.sleep(PROPAGATION_WAIT)

    step("DELETE — Remove subscription")
    r = delete_sub(sub_id)
    if r is not None:
        check(r.status_code == 204,
              f"204 No Content (got {r.status_code})",
              f"Expected 204, got {r.status_code}", pretty(r))

    step("GET — Confirm 404 after delete")
    time.sleep(PROPAGATION_WAIT)
    r2 = get_sub(sub_id)
    if r2 is not None:
        check(r2.status_code == 404,
              f"404 Not Found after delete (got {r2.status_code})",
              f"Expected 404, got {r2.status_code}", pretty(r2))

    step("GET collection — sub must be absent")
    ids = sub_ids_in_collection()
    if ids is None:
        fail("Could not read subscription collection")
    else:
        check(sub_id not in ids,
              "Deleted subscription absent from collection",
              f"Deleted sub {sub_id} still listed", f"collection={sorted(ids)}")
    return sub_id


def t2_delete_idempotency(deleted_sub_id):
    divider("T2 — Delete idempotency (second DELETE → 404)")
    if deleted_sub_id is None:
        skip("T1 produced no subscription to re-delete")
        return

    step("DELETE — Same subscription again")
    r = delete_sub(deleted_sub_id)
    if r is not None:
        check(r.status_code == 404,
              f"404 on repeated delete (got {r.status_code})",
              f"Expected 404, got {r.status_code}", pretty(r))


def t3_delete_nonexistent():
    divider("T3 — Delete non-existent subscription")

    bogus = f"no-such-sub-{uuid.uuid4().hex[:8]}"
    step(f"DELETE — Random subID ({bogus})")
    r = delete_sub(bogus)
    if r is not None:
        check(r.status_code == 404,
              f"404 Not Found (got {r.status_code})",
              f"Expected 404, got {r.status_code}", pretty(r))


def t4_af_isolation():
    divider("T4 — AF isolation (delete under wrong afID)")

    step("POST — Create subscription under owner AF")
    sub_id = create_sub_or_skip(non_gbr_body(), "owner subscription")
    if sub_id is None:
        skip("T4 needs a live UE/PDU session at UE_IPV4")
        return
    time.sleep(PROPAGATION_WAIT)

    step("DELETE — Attempt delete under a different afID")
    r = delete_sub(sub_id, af_id=OTHER_AF_ID)
    if r is not None:
        check(r.status_code == 404,
              f"404 for foreign AF (got {r.status_code})",
              f"Expected 404, got {r.status_code}", pretty(r))

    step("GET — Subscription must still exist under owner AF")
    r2 = get_sub(sub_id)
    if r2 is not None:
        check(r2.status_code == 200,
              "Subscription intact after foreign delete attempt",
              f"Expected 200, got {r2.status_code}", pretty(r2))

    step("DELETE — Cleanup under owner AF")
    r3 = delete_sub(sub_id)
    if r3 is not None:
        check(r3.status_code == 204,
              "Cleanup delete returned 204",
              f"Expected 204, got {r3.status_code}", pretty(r3))
    time.sleep(PROPAGATION_WAIT)


def t5_gbr_budget_restore():
    divider("T5 — GBR budget restore across delete (5QI=1)")
    # PCF debits RemainGbrUL/DL on GBR create and must credit it back on delete
    # (IncreaseRemainGBR). If the credit is lost, repeated create/delete of the
    # same GBR flow eventually fails Session Binding / bitrate admission.
    for i in range(1, 4):
        step(f"Cycle {i}: POST GBR subscription")
        sub_id = create_sub_or_skip(gbr_body(), f"GBR subscription #{i}")
        if sub_id is None:
            skip(f"T5 needs a live UE/PDU session at UE_IPV4 — stopping remaining cycles")
            return
        time.sleep(PROPAGATION_WAIT)

        step(f"Cycle {i}: DELETE GBR subscription")
        r = delete_sub(sub_id)
        if r is not None:
            check(r.status_code == 204,
                  f"204 on delete (cycle {i})",
                  f"Expected 204, got {r.status_code}", pretty(r))
        time.sleep(PROPAGATION_WAIT)


def t6_churn(cycles):
    divider(f"T6 — Create/delete churn ({cycles} cycles, non-GBR)")
    # Unpatched leaks this can expose (see gap-analysis doc):
    #   PCF AppSession pool growth              → any cycle count (check PCF logs/memory)
    #   SMF QFI generator (QFIGenerator 2..63)  → past ~62 cycles, a stuck/never-freed ID
    #                                              would surface as "Allocate failed" — the
    #                                              generator is round-robin and normally
    #                                              reuses freed IDs once the range wraps
    #   SMF NAS rule-ID generator (1..255)      → same, past ~254 cycles
    step(f"Running {cycles} create→delete cycles")
    for i in range(1, cycles + 1):
        r = post_sub(non_gbr_body())
        if r is None or r.status_code != 201:
            fail(f"Cycle {i}: create failed (status={getattr(r, 'status_code', 'n/a')})",
                 pretty(r))
            warn_no_ue_hint()
            return
        loc = r.headers.get("Location", "")
        sub_id = loc.rstrip("/").split("/")[-1] if loc else ""
        time.sleep(0.3)
        r2 = delete_sub(sub_id)
        if r2 is None or r2.status_code != 204:
            fail(f"Cycle {i}: delete failed (status={getattr(r2, 'status_code', 'n/a')})",
                 pretty(r2))
            return
        time.sleep(0.3)
    ok(f"All {cycles} create/delete cycles returned 201/204")

    step("GET collection — must be empty for this AF after churn")
    ids = sub_ids_in_collection()
    if ids is None:
        fail("Could not read subscription collection")
    else:
        check(len(ids) == 0,
              "No residual subscriptions after churn",
              f"{len(ids)} subscriptions leaked", f"collection={sorted(ids)}")


def t7_multi_sub_isolation():
    divider("T7 — Multi-subscription isolation")

    step("POST — Create subscription A (flowId 1)")
    sub_a = create_sub_or_skip(non_gbr_body(flow_id=1, remote_ip=REMOTE_IP),
                              "subscription A")
    if sub_a is None:
        skip("T7 needs a live UE/PDU session at UE_IPV4")
        return
    step("POST — Create subscription B (flowId 3, different remote)")
    sub_b = create_sub_or_skip(non_gbr_body(flow_id=3, remote_ip=REMOTE_IP_B),
                              "subscription B")
    if sub_b is None:
        skip("T7 needs a live UE/PDU session at UE_IPV4 — cleaning up A")
        delete_sub(sub_a)
        return
    time.sleep(PROPAGATION_WAIT)

    step("DELETE — Remove subscription A only")
    r = delete_sub(sub_a)
    if r is not None:
        check(r.status_code == 204,
              f"204 deleting A (got {r.status_code})",
              f"Expected 204, got {r.status_code}", pretty(r))
    time.sleep(PROPAGATION_WAIT)

    step("GET — A gone, B intact")
    ra = get_sub(sub_a)
    if ra is not None:
        check(ra.status_code == 404,
              "A returns 404",
              f"Expected 404 for A, got {ra.status_code}", pretty(ra))
    rb = get_sub(sub_b)
    if rb is not None:
        check(rb.status_code == 200,
              "B still returns 200",
              f"Expected 200 for B, got {rb.status_code}", pretty(rb))
        if rb.status_code == 200:
            rj = rb.json()
            check(rj.get("flowInfo", [{}])[0].get("flowId") == 3,
                  "B payload unchanged (flowId=3)",
                  f"B payload mutated: {rj.get('flowInfo')}")

    step("DELETE — Cleanup subscription B")
    rc = delete_sub(sub_b)
    if rc is not None:
        check(rc.status_code == 204,
              "Cleanup delete of B returned 204",
              f"Expected 204, got {rc.status_code}", pretty(rc))
    time.sleep(PROPAGATION_WAIT)


def t8_delete_after_patch():
    divider("T8 — Delete after PATCH (modified AppSession must tear down)")

    step("POST — Create subscription")
    sub_id = create_sub_or_skip(non_gbr_body(), "subscription")
    if sub_id is None:
        skip("T8 needs a live UE/PDU session at UE_IPV4")
        return
    time.sleep(PROPAGATION_WAIT)

    step("PATCH — Update MBR")
    r = patch_sub(sub_id, {"mbrDl": "25000000 bps", "mbrUl": "12000000 bps"})
    if r is not None:
        check(r.status_code in (200, 204),
              f"200/204 on PATCH (got {r.status_code})",
              f"Expected 200/204, got {r.status_code}", pretty(r))
    time.sleep(PROPAGATION_WAIT)

    step("DELETE — Remove patched subscription")
    r2 = delete_sub(sub_id)
    if r2 is not None:
        check(r2.status_code == 204,
              f"204 on delete (got {r2.status_code})",
              f"Expected 204, got {r2.status_code}", pretty(r2))

    step("GET — Confirm 404")
    time.sleep(PROPAGATION_WAIT)
    r3 = get_sub(sub_id)
    if r3 is not None:
        check(r3.status_code == 404,
              f"404 after delete (got {r3.status_code})",
              f"Expected 404, got {r3.status_code}", pretty(r3))

def t9_strict_pcf_down():
    """Operator-guided validation of strict DELETE semantics (run with --pcf-down).

    Expected on a strict-mode NEF (gap-analysis §4.7):
      - DELETE while PCF is down  → 5xx (502 PCF_UNREACHABLE or PCF's ProblemDetails)
      - Subscription is RETAINED  → GET still 200
      - DELETE after PCF restart  → 204, then GET → 404
    On today's best-effort NEF the first DELETE returns 204 → this test fails,
    which is the point: it goes green when work item 2F lands.
    """
    divider("T9 — Strict delete with PCF down (operator-guided)")

    print(f"{YELLOW}  Ensure PCF is currently RUNNING, then press Enter...{RESET}")
    input()

    step("POST — Create subscription (PCF up)")
    sub_id = create_sub_or_skip(non_gbr_body(), "subscription")
    if sub_id is None:
        skip("T9 needs a live UE/PDU session at UE_IPV4 — aborting operator-guided run")
        return
    time.sleep(PROPAGATION_WAIT)

    print(f"\n{YELLOW}  Now STOP the PCF (e.g. kill the pcf process / docker stop pcf).{RESET}")
    print(f"{YELLOW}  Press Enter when PCF is down...{RESET}")
    input()

    step("DELETE — With PCF down (expect 5xx, strict mode)")
    r = delete_sub(sub_id)
    if r is not None:
        check(500 <= r.status_code <= 599,
              f"5xx returned with PCF down (got {r.status_code})",
              f"Expected 5xx, got {r.status_code} — NEF is still best-effort",
              pretty(r))

    step("GET — Subscription must be retained after failed delete")
    r2 = get_sub(sub_id)
    if r2 is not None:
        check(r2.status_code == 200,
              "Subscription retained (200)",
              f"Expected 200, got {r2.status_code} — sub was deleted despite PCF failure",
              pretty(r2))

    print(f"\n{YELLOW}  Now RESTART the PCF and wait until it re-registers with NRF.{RESET}")
    print(f"{YELLOW}  Press Enter when PCF is back up...{RESET}")
    input()

    step("DELETE — Retry after PCF restart (expect 204)")
    r3 = delete_sub(sub_id)
    if r3 is not None:
        check(r3.status_code == 204,
              f"204 on retry (got {r3.status_code})",
              f"Expected 204, got {r3.status_code}", pretty(r3))

    step("GET — Confirm 404 after successful retry")
    time.sleep(PROPAGATION_WAIT)
    r4 = get_sub(sub_id)
    if r4 is not None:
        check(r4.status_code == 404,
              f"404 after delete (got {r4.status_code})",
              f"Expected 404, got {r4.status_code}", pretty(r4))


def t10_create_rejected_no_matching_ue():
    """Answers "what if there's no QoS flow?" for the delete path.

    NEF's create is strict (NFs/nef/internal/sbi/processor/qos.go,
    PostQoSSubscription lines ~188-208): it calls PCF's
    Npcf_PolicyAuthorization Create synchronously and only stores the local
    subscription in the success branch. When ueIpv4Addr matches no PDU
    session, PCF's SessionBinding fails
    (NFs/pcf/internal/sbi/processor/policyauthorization.go ~line 195) and
    PCF returns ProblemDetails{Status:500, Cause:"PDU_SESSION_NOT_AVAILABLE"}
    (NFs/pcf/internal/util/pcf_util.go PcpErrHttpStatusMap), which NEF
    forwards verbatim — no subscription is ever stored.

    So "delete a subscription that has no real QoS flow behind it" is not a
    reachable state through the API: it can never be created in the first
    place. This test proves the rejection (and the no-orphan claim via a
    before/after collection diff), then shows the delete side collapses to
    exactly T3 — DELETE on a subID that never existed → 404.
    """
    divider("T10 — Create rejected when no matching UE/PDU session")

    step("GET collection — baseline before the rejected create")
    ids_before = sub_ids_in_collection()

    step(f"POST — ueIpv4Addr with no PDU session ({NO_UE_TEST_IP})")
    body = non_gbr_body(flow_id=9, remote_ip=REMOTE_IP)
    body["ueIpv4Addr"] = NO_UE_TEST_IP
    r = post_sub(body)
    if r is not None:
        check(r.status_code == 500,
              f"500 Internal Server Error (got {r.status_code})",
              f"Expected 500 (PDU_SESSION_NOT_AVAILABLE), got {r.status_code}", pretty(r))
        try:
            cause = r.json().get("cause")
            check(cause == "PDU_SESSION_NOT_AVAILABLE",
                  "cause=PDU_SESSION_NOT_AVAILABLE",
                  f"Unexpected cause: {cause}", pretty(r))
        except Exception:
            fail("Response body is not valid JSON", pretty(r))
        check("Location" not in r.headers,
              "No Location header on rejected create",
              "Location header present despite rejected create", pretty(r))

    step("GET collection — must be unchanged (no orphan subscription stored)")
    ids_after = sub_ids_in_collection()
    if ids_before is not None and ids_after is not None:
        check(ids_after == ids_before,
              "Collection unchanged after rejected create",
              "Collection changed — a subscription was stored despite PCF rejecting it",
              f"before={sorted(ids_before)} after={sorted(ids_after)}")
    else:
        fail("Could not read subscription collection to compare before/after")

    step("DELETE — the 'no flow' delete-side is exactly T3 (random subID → 404)")
    bogus = f"no-flow-sub-{uuid.uuid4().hex[:8]}"
    r2 = delete_sub(bogus)
    if r2 is not None:
        check(r2.status_code == 404,
              f"404 Not Found (got {r2.status_code}) — same code path as T3",
              f"Expected 404, got {r2.status_code}", pretty(r2))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    pcf_down_mode = "--pcf-down" in args
    args = [a for a in args if a != "--pcf-down"]

    cycles = CHURN_CYCLES_DEFAULT
    if args:
        try:
            cycles = max(1, int(args[0]))
        except ValueError:
            print(f"{YELLOW}Ignoring non-integer churn_cycles argument "
                  f"'{args[0]}', using {CHURN_CYCLES_DEFAULT}{RESET}")

    print(f"\n{BOLD}NEF QoS Subscription — Deletion End-to-End Tests{RESET}")
    print(f"NEF: {NEF_BASE_URL}")
    print(f"AF:  {AF_ID}  |  UE: {UE_IPV4}")

    if pcf_down_mode:
        t9_strict_pcf_down()
    else:
        print(f"Churn cycles: {cycles}")
        print(f"\n{YELLOW}Note: AF-facing 204 precedes SMF/RAN/UE release (async).{RESET}")
        print(f"{YELLOW}      Over-the-air release needs pcap/UE checks — see the{RESET}")
        print(f"{YELLOW}      verification guide in the gap-analysis document.{RESET}")
        print(f"{YELLOW}      T1,T4,T5,T7,T8,T9 need a live UE and are SKIPPED{RESET}")
        print(f"{YELLOW}      (not failed) without one. T2,T3,T10 always run.{RESET}")

        deleted = t1_delete_happy_path()
        t2_delete_idempotency(deleted)
        t3_delete_nonexistent()
        t10_create_rejected_no_matching_ue()
        t4_af_isolation()
        t5_gbr_budget_restore()
        t6_churn(cycles)
        t7_multi_sub_isolation()
        t8_delete_after_patch()

    print(f"\n{'═'*60}")
    total = passed + failed
    skip_note = f"  {YELLOW}{skipped} skipped{RESET}" if skipped else ""
    print(f"Results: {GREEN}{passed}/{total} passed{RESET}  {RED}{failed}/{total} failed{RESET}{skip_note}")
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()