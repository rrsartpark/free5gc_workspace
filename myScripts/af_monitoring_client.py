#!/usr/bin/env python3
"""
AF monitoring client -- logs NEF notifications durably to files.

Writes three files:
  notifications.jsonl  every payload, one JSON object per line, flushed on write
  af.log               same events as the console, plus subscribe/unsubscribe
  trail.csv            DEDUPED location trail -- the file your predictor reads

Run:
    python3 af_monitoring_client.py
"""

import csv
import json
import logging
import os
import sys
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response

# --- CONFIGURATION ---------------------------------------------------------
NEF_API_URL = "http://127.0.0.5:8000/3gpp-monitoring-event/v1/test-scsas/subscriptions"
NOTIFY_HOST = "0.0.0.0"
NOTIFY_PORT = 5042
NOTIFY_PATH = "/notify"
PUBLIC_NOTIFY_HOST = "127.0.0.1"  # The IP NEF will use to reach this AF (TS 29.122 notificationDestination)

MONITORING_TYPES = [
    "LOSS_OF_CONNECTIVITY",
    "UE_REACHABILITY",
    "LOCATION_REPORTING",
]

OUT_DIR = "af_out"
RAW_FILE = f"{OUT_DIR}/notifications.jsonl"
LOG_FILE = f"{OUT_DIR}/af.log"
TRAIL_FILE = f"{OUT_DIR}/trail.csv"

# nci = gNB id << 4 with idLength 32 (matches gen_configs.py). Change if you
# alter the NCI scheme, or set to None to skip gNB-id decoding.
CELL_ID_SHIFT = 4

# Optional: point this at lroi/<name>/towers_tai.csv and the trail gains the
# sector azimuth/beamwidth and the physical site group, so a consumer can tell
# a sector-to-sector switch at one mast from a move between masts. The AF works
# without it -- nrCellId alone already identifies the exact serving cell.
TAI_CSV = os.environ.get("TAI_CSV", "")

# --- logging: console AND file, so nothing depends on terminal scrollback ---
os.makedirs(OUT_DIR, exist_ok=True)
logger = logging.getLogger("af")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
_fh = logging.FileHandler(LOG_FILE)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)

_raw = open(RAW_FILE, "a", buffering=1)          # line-buffered
_trail_new = not os.path.exists(TRAIL_FILE) or os.path.getsize(TRAIL_FILE) == 0
_trail_f = open(TRAIL_FILE, "a", newline="", buffering=1)
_trail = csv.writer(_trail_f)
if _trail_new:
    _trail.writerow(["recv_time", "ue_timestamp", "supi", "tac",
                     "nr_cell_id", "gnb_id", "cell_name", "site", "site_group",
                     "azimuth", "beamwidth", "switch_kind", "event_type"])

# last (tac, ncgi) seen per SUPI, for dedup
CELLS = {}
if TAI_CSV and os.path.exists(TAI_CSV):
    with open(TAI_CSV, newline="") as _f:
        for _r in csv.DictReader(_f):
            CELLS[int(_r["gnb_id"])] = {
                "name": _r.get("name", ""),
                "site": _r.get("site", ""),
                "site_group": _r.get("site_group", ""),
                "azimuth": _r.get("azimuth", ""),
                "beamwidth": _r.get("beamwidth", ""),
            }
    logger.info(f"loaded {len(CELLS)} cell(s) from {TAI_CSV} for sector enrichment")
elif TAI_CSV:
    logger.warning(f"TAI_CSV={TAI_CSV} not found -- trail will omit sector columns")

_last_loc = {}
_last_group = {}
_counts = {"total": 0, "location": 0, "trail": 0, "dup": 0}
_subscriptions = []


def decode_gnb_id(nr_cell_id):
    """nrCellId hex string -> gNB id. Returns None if undecodable."""
    if not nr_cell_id or CELL_ID_SHIFT is None:
        return None
    try:
        return int(nr_cell_id, 16) >> CELL_ID_SHIFT
    except ValueError:
        return None


def handle_report(rep, recv_iso):
    """Log one report; append to the trail only when the location actually changed."""
    etype = rep.get("type", "?")
    supi = rep.get("supi", "?")
    _counts["total"] += 1

    if etype != "LOCATION_REPORT":
        detail = (rep.get("reachability")
                  or rep.get("lossOfConnectReason")
                  or "")
        logger.info(f"  {etype:<22} {supi} {detail}")
        return

    _counts["location"] += 1
    nr = (rep.get("location") or {}).get("nrLocation") or {}
    tac = (nr.get("tai") or {}).get("tac")
    ncgi = (nr.get("ncgi") or {}).get("nrCellId")
    gnb = decode_gnb_id(ncgi)
    # ageOfLocationInformation is deliberately ignored: this deployment
    # returns large negative values, which are invalid per TS 29.571.
    uts = nr.get("ueLocationTimestamp", "")

    info = CELLS.get(gnb, {})
    grp = info.get("site_group", "")
    bw = info.get("beamwidth", "")
    az = info.get("azimuth", "")

    key = (tac, ncgi)
    changed = _last_loc.get(supi) != key
    prev_grp = _last_group.get(supi)
    _last_loc[supi] = key
    if grp:
        _last_group[supi] = grp

    # Distinguish a sector change at one mast from a move to another mast.
    # Both arrive as ordinary LOCATION_REPORTs -- each sector is its own gNB
    # process, so each switch is its own registration.
    if not changed:
        kind = "repeat"
    elif prev_grp is None:
        kind = "initial"
    elif grp and grp == prev_grp:
        kind = "sector"
    else:
        kind = "site"

    gnb_s = f"gNB {gnb}" if gnb is not None else "gNB ?"
    sect_s = ""
    if bw and az:
        try:
            if float(bw) < 360:
                sect_s = f" az {float(az):.0f}d/{float(bw):.0f}d"
        except ValueError:
            pass
    if changed:
        _counts["trail"] += 1
        _counts[kind] = _counts.get(kind, 0) + 1
        _trail.writerow([recv_iso, uts, supi, tac, ncgi, gnb,
                         info.get("name", ""), info.get("site", ""), grp,
                         az, bw, kind, etype])
        tag = "SECTOR CHANGE, same mast" if kind == "sector" else \
              ("new mast" if kind == "site" else "initial")
        logger.info(f"  LOCATION_REPORT        {supi} TAC {tac} {ncgi} -> "
                    f"{gnb_s}{sect_s}  [{tag} -- trail #{_counts['trail']}]")
    else:
        _counts["dup"] += 1
        logger.info(f"  LOCATION_REPORT        {supi} TAC {tac} {ncgi} -> "
                    f"{gnb_s}  (same as previous, not added to trail)")


async def subscribe(client):
    for mtype in MONITORING_TYPES:
        payload = {
            "monitoringType": mtype,
            "notificationDestination":
                f"http://{PUBLIC_NOTIFY_HOST}:{NOTIFY_PORT}{NOTIFY_PATH}",
            "notifyCorrelationId": f"notif-{mtype.lower()}",
            "nfId": "123e4567-e89b-12d3-a456-426614174000",
        }
        try:
            r = await client.post(NEF_API_URL, json=payload, timeout=10)
            r.raise_for_status()
            loc = r.headers.get("Location")
            sub_id = None
            try:
                sub_id = (r.json() or {}).get("self") or loc
            except Exception:
                sub_id = loc
            if sub_id:
                _subscriptions.append(sub_id)
            logger.info(f"SUBSCRIBED {mtype} -> {r.status_code} "
                        f"{'(' + str(sub_id) + ')' if sub_id else ''}")
        except httpx.HTTPStatusError as e:
            logger.error(f"SUBSCRIBE FAILED {mtype}: {e.response.status_code} "
                         f"{e.response.text}")
        except Exception as e:
            logger.error(f"SUBSCRIBE FAILED {mtype}: {e!r}")


async def unsubscribe(client):
    """Without this, the NEF keeps POSTing to a dead endpoint after Ctrl-C,
    and stale subscriptions pile up across runs."""
    for url in _subscriptions:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        try:
            r = await client.delete(url, timeout=10)
            logger.info(f"UNSUBSCRIBED {url} -> {r.status_code}")
        except Exception as e:
            logger.warning(f"UNSUBSCRIBE FAILED {url}: {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 70)
    logger.info(f"AF starting -- writing {RAW_FILE}, {LOG_FILE}, {TRAIL_FILE}")
    if not CELLS:
        logger.info("  no topology loaded: set TAI_CSV=<path to towers_tai.csv> "
                    "to record sector and physical-site columns in the trail")
    async with httpx.AsyncClient() as client:
        await subscribe(client)
        yield
        logger.info(f"totals: {_counts['total']} reports, "
                    f"{_counts['location']} LOCATION_REPORT "
                    f"({_counts['trail']} new / {_counts['dup']} repeat)")
        if _counts.get("sector") or _counts.get("site"):
            logger.info(f"  of the new ones: {_counts.get('site', 0)} moved to a "
                        f"different mast, {_counts.get('sector', 0)} were "
                        f"sector-to-sector at the same mast")
        await unsubscribe(client)
    _raw.flush()
    _trail_f.flush()
    logger.info("AF stopped")


app = FastAPI(lifespan=lifespan)


@app.post(NOTIFY_PATH)
async def receive_notification(request: Request):
    body = await request.json()
    recv_iso = datetime.now(timezone.utc).isoformat()

    # raw first, so a crash mid-processing still leaves the payload on disk
    _raw.write(json.dumps({"_recv": recv_iso, **body}) + "\n")
    _raw.flush()

    corr = body.get("notifyCorrelationId", "?")
    reports = body.get("reportList") or []
    logger.info(f"NOTIFY corr={corr} ({len(reports)} report(s))")
    for rep in reports:
        try:
            handle_report(rep, recv_iso)
        except Exception as e:
            logger.error(f"  parse error: {e!r}")

    return Response(status_code=204)


@app.get("/stats")
async def stats():
    return {"counts": _counts, "last_location": {k: list(v) for k, v in _last_loc.items()}}


if __name__ == "__main__":
    import uvicorn
    # reload=False on purpose: the reloader re-runs startup on every file
    # change, which re-POSTs the subscriptions and leaves duplicates in the NEF.
    uvicorn.run(app, host=NOTIFY_HOST, port=NOTIFY_PORT, reload=False,
                log_config=None)