#!/usr/bin/env python3
"""
ROI approach tracker -- watches AF location reports and flags UEs heading for a
target tracking area.

Logic, per the operator's specification:
  1. A UE that enters the ARM tracking area starts being tracked.
  2. While tracked, on every cell change, predict the next tracking area.
  3. If the predicted next TAI is the ROI, put the UE on the notification list.

It also raises an earlier, weaker WATCH when the ROI is within --horizon TAI
hops along the direction of travel. That matters here: in this topology TAI 11
is never entered directly from TAI 3 (site-3's nearest TAI-3 cell is 2764 m
away, while site-2 in TAI 10 is 1927 m), so the real path is 3 -> 10 -> 11. A
literal one-hop check would never fire; continuous tracking after arming does.

INPUT -- two ways to connect to the AF, no AF changes needed for either:

  --tail af_out/notifications.jsonl     (default) follow the file as it grows
  --replay af_out/notifications.jsonl   read the whole file once, for testing
  --udp 0.0.0.0:9999                    listen for JSON datagrams, if you would
                                        rather have the AF forward them

OUTPUT
  alerts.csv          one row per state change (TRACKING / WATCH / ALERT / ENTERED)
  tracker_state.json  current per-UE state, rewritten on each change
  console             live table

Usage
  python3 tracker.py --tai-csv lroi/nlm/towers_tai.csv \
                     --arm-tai 3 --roi-tai 11 \
                     --tail ~/free5GC_NEF_DEV/free5gc_workspace/myScripts/af_out/notifications.jsonl
"""

import argparse
import csv
import json
import math
import os
import re
import socket
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

R_EARTH = 6371000.0


# --------------------------------------------------------------------- geometry
def hav(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(h))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def ang_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


# ------------------------------------------------------------------- topology
class Topology:
    def __init__(self, path, adj_factor=1.0):
        self.cells = {}
        for r in csv.DictReader(open(path)):
            g = int(r["gnb_id"])
            self.cells[g] = {
                "gnb_id": g, "site": int(r["site"]), "name": r["name"],
                "tac": int(r["tac"]), "lat": float(r["lat"]),
                "lon": float(r["lon"]), "range_m": float(r["range_m"]),
            }
        if not self.cells:
            sys.exit(f"{path} has no cells")
        self.by_tac = defaultdict(list)
        for c in self.cells.values():
            self.by_tac[c["tac"]].append(c)

        # cell adjacency: overlapping-ish coverage
        self.neigh = defaultdict(set)
        ids = list(self.cells)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ca, cb = self.cells[a], self.cells[b]
                d = hav(ca["lat"], ca["lon"], cb["lat"], cb["lon"])
                # two cells are neighbours when their nominal coverage nearly
                # touches. Calibrated on this topology: factor 1.0 is the
                # smallest that reproduces every observed TAI transition, and it
                # correctly reports TAI 3 -> 11 as 2 hops (via TAI 10). At 1.4+
                # it wrongly claims they are directly adjacent.
                if d <= adj_factor * (ca["range_m"] + cb["range_m"]):
                    self.neigh[a].add(b)
                    self.neigh[b].add(a)

        # TAI adjacency graph, derived from cell adjacency
        self.tai_neigh = defaultdict(set)
        for a, ns in self.neigh.items():
            ta = self.cells[a]["tac"]
            for b in ns:
                tb = self.cells[b]["tac"]
                if ta != tb:
                    self.tai_neigh[ta].add(tb)
                    self.tai_neigh[tb].add(ta)

    def tai_hops(self, src, dst):
        """BFS hop count between tracking areas, or None if unreachable."""
        if src == dst:
            return 0
        seen, q = {src}, deque([(src, 0)])
        while q:
            t, h = q.popleft()
            for n in self.tai_neigh[t]:
                if n == dst:
                    return h + 1
                if n not in seen:
                    seen.add(n)
                    q.append((n, h + 1))
        return None

    def decode(self, nr_cell_id):
        try:
            return int(nr_cell_id, 16) >> 4
        except (TypeError, ValueError):
            return None


# ------------------------------------------------------------------ predictor
def predict_next(topo, history, top_k=3, recency_penalty=0.35):
    """
    history: newest-last list of cell dicts the UE has served on.
    Returns [(cell, score), ...] best first.

    Geographic and direction-aware rather than a learned transition table: with
    one lap of data a Markov table cannot generalise, whereas a heading plus the
    cell layout predicts routes never seen before.
    """
    if not history:
        return []
    cur = history[-1]
    if len(history) < 2:
        # no heading yet: fall back to nearest neighbours
        cands = [(c, -hav(cur["lat"], cur["lon"], c["lat"], c["lon"])
                     / (cur["range_m"] + c["range_m"]))
                 for c in topo.cells.values() if c["gnb_id"] != cur["gnb_id"]]
        cands.sort(key=lambda x: -x[1])
        return cands[:top_k]

    # heading over the last few served cells
    ref = history[max(0, len(history) - 3)]
    if ref["gnb_id"] == cur["gnb_id"] and len(history) >= 2:
        ref = history[-2]
    head = bearing(ref["lat"], ref["lon"], cur["lat"], cur["lon"])

    recent = {h["gnb_id"] for h in history[-3:]}
    out = []
    for c in topo.cells.values():
        if c["gnb_id"] == cur["gnb_id"]:
            continue
        d = hav(cur["lat"], cur["lon"], c["lat"], c["lon"])
        if d <= 0:
            continue
        brg = bearing(cur["lat"], cur["lon"], c["lat"], c["lon"])
        align = math.cos(math.radians(ang_diff(head, brg)))   # 1 ahead, -1 behind
        norm_d = d / max(1.0, cur["range_m"] + c["range_m"])
        score = align - 0.55 * norm_d
        if c["gnb_id"] in recent:
            score -= recency_penalty
        out.append((c, score))
    out.sort(key=lambda x: -x[1])
    return out[:top_k]


# ------------------------------------------------------------------- tracking
class Tracker:
    def __init__(self, topo, arm_tai, roi_tai, horizon, top_k, outdir,
                 alert_on_topk=False, confirm=1, pattern=None):
        self.topo = topo
        self.arm = arm_tai
        self.roi = roi_tai
        self.horizon = horizon
        self.top_k = top_k
        self.alert_on_topk = alert_on_topk
        self.confirm = max(1, confirm)
        self.pattern = pattern
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.ue = {}
        self.alerts_path = os.path.join(outdir, "alerts.csv")
        new = not os.path.exists(self.alerts_path) \
            or os.path.getsize(self.alerts_path) == 0
        self.af = open(self.alerts_path, "a", newline="", buffering=1)
        self.aw = csv.writer(self.af)
        if new:
            self.aw.writerow(["wall_time", "supi", "level", "cur_site", "cur_tai",
                              "pred_site", "pred_tai", "score", "tai_hops_to_roi",
                              "detail"])
        self.notify = []

    def _st(self, supi):
        if supi not in self.ue:
            self.ue[supi] = {"hist": [], "tais": [], "tracking": False,
                             "level": "-", "alerted": False, "entered": False,
                             "last": None, "pos": 0}
        return self.ue[supi]

    def _advance_pattern(self, s, tac):
        """
        Strict prefix match of the TAI pattern. A TAI that is not the expected
        next element resets the match -- to 1 if it happens to be the pattern's
        first element, otherwise to 0.

        Matching the ORDER is what makes this directional: a UE travelling
        11 -> 10 -> 3 never advances past position 1, so a UE leaving the ROI is
        excluded without any heading logic.
        """
        pat = self.pattern
        pos = s["pos"]
        if pos < len(pat) and tac == pat[pos]:
            pos += 1
        elif tac == pat[0]:
            pos = 1
        else:
            pos = 0
        s["pos"] = pos
        return pos

    def _emit(self, supi, level, s, pred=None, score=None, hops=None, detail=""):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = s["hist"][-1] if s["hist"] else None
        self.aw.writerow([
            now, supi, level,
            cur["name"] if cur else "", cur["tac"] if cur else "",
            pred["name"] if pred else "", pred["tac"] if pred else "",
            f"{score:.3f}" if score is not None else "",
            hops if hops is not None else "", detail,
        ])
        self.af.flush()
        tag = {"TRACKING": "[track]", "WATCH": "[WATCH]", "ALERT": "[ALERT]",
               "ENTERED": "[ENTER]", "CLEARED": "[clear]"}.get(level, "[  ?  ]")
        line = f"{now[11:19]} {tag} {supi[-6:]}"
        if cur:
            line += f"  at {cur['name']}/T{cur['tac']}"
        if pred:
            line += f"  -> next {pred['name']}/T{pred['tac']}"
            if score is not None:
                line += f" ({score:.2f})"
        if hops is not None:
            line += f"  ROI {hops} TAI-hop(s) away"
        if detail:
            line += f"  {detail}"
        print(line, flush=True)

    def on_location_pattern(self, supi, gnb_id):
        """TAI-sequence mode: flag a UE once it has matched all but the last
        element of the pattern, so the alert precedes arrival."""
        cell = self.topo.cells.get(gnb_id)
        if cell is None:
            print(f"  ? unknown gNB id {gnb_id} for {supi}", file=sys.stderr)
            return
        s = self._st(supi)
        if s["hist"] and s["hist"][-1]["gnb_id"] == gnb_id:
            return
        s["hist"].append(cell)
        tac_changed = not s["tais"] or s["tais"][-1] != cell["tac"]
        if tac_changed:
            s["tais"].append(cell["tac"])
        else:
            return                      # pattern only advances on a TAI change

        pat = self.pattern
        before = s["pos"]
        pos = self._advance_pattern(s, cell["tac"])
        shown = "/".join(str(p) for p in pat)

        if pos >= len(pat):
            s["entered"] = True
            s["level"] = "COMPLETE"
            self._emit(supi, "ENTERED", s,
                       detail=f"pattern {shown} complete ({pos}/{len(pat)})")
        elif pos == len(pat) - 1:
            s["tracking"] = True
            s["level"] = "ALERT"
            if not s["alerted"]:
                s["alerted"] = True
                if supi not in self.notify:
                    self.notify.append(supi)
            nxt = pat[pos]
            self._emit(supi, "ALERT", s, hops=self.topo.tai_hops(cell["tac"], self.roi),
                       detail=f"matched {shown} up to {pos}/{len(pat)}, next expected "
                              f"TAI {nxt} -- NOTIFICATION LIST")
        elif pos > 0:
            s["tracking"] = True
            s["level"] = "TRACKING"
            self._emit(supi, "TRACKING", s,
                       detail=f"pattern {shown} at {pos}/{len(pat)}")
        elif before > 0:
            s["level"] = "CLEARED"
            self._emit(supi, "CLEARED", s,
                       detail=f"pattern broken at TAI {cell['tac']} (was {before}/{len(pat)})")
        self._save()

    def on_location(self, supi, gnb_id):
        if self.pattern:
            return self.on_location_pattern(supi, gnb_id)
        cell = self.topo.cells.get(gnb_id)
        if cell is None:
            print(f"  ? unknown gNB id {gnb_id} for {supi} -- not in towers_tai.csv",
                  file=sys.stderr)
            return
        s = self._st(supi)

        # ignore repeats of the same serving cell
        if s["hist"] and s["hist"][-1]["gnb_id"] == gnb_id:
            return
        s["hist"].append(cell)
        if not s["tais"] or s["tais"][-1] != cell["tac"]:
            s["tais"].append(cell["tac"])

        # ---- reached the ROI?
        if cell["tac"] == self.roi:
            if not s["entered"]:
                s["entered"] = True
                s["level"] = "ENTERED"
                self._emit(supi, "ENTERED", s, detail=f"now inside ROI TAI {self.roi}")
            return

        # ---- arm on entering the arm TAI
        if not s["tracking"] and cell["tac"] == self.arm:
            s["tracking"] = True
            s["level"] = "TRACKING"
            hops = self.topo.tai_hops(cell["tac"], self.roi)
            self._emit(supi, "TRACKING", s, hops=hops,
                       detail=f"entered arm TAI {self.arm}")

        if not s["tracking"]:
            return

        # ---- predict the next tracking area
        preds = predict_next(self.topo, s["hist"], top_k=self.top_k)
        if not preds:
            return
        best, best_score = preds[0]
        hops = self.topo.tai_hops(cell["tac"], self.roi)

        roi_in_top = next(((c, sc) for c, sc in preds if c["tac"] == self.roi), None)

        if best["tac"] == self.roi:
            # Confirmation counter. On real road routes a UE that merely passes
            # through the gateway cell and turns away predicts the ROI exactly
            # once, whereas UEs that go on to enter it predict the ROI on two or
            # more consecutive cell changes. Requiring --confirm hits therefore
            # removes that false positive without losing any true positive.
            s["roi_streak"] = s.get("roi_streak", 0) + 1
            if s["roi_streak"] >= self.confirm:
                s["level"] = "ALERT"
                if not s["alerted"]:
                    s["alerted"] = True
                    if supi not in self.notify:
                        self.notify.append(supi)
                self._emit(supi, "ALERT", s, pred=best, score=best_score, hops=hops,
                           detail=f"predicted next TAI is the ROI "
                                  f"({s['roi_streak']}/{self.confirm}) "
                                  f"-- NOTIFICATION LIST")
            else:
                s["level"] = "WATCH"
                self._emit(supi, "WATCH", s, pred=best, score=best_score, hops=hops,
                           detail=f"ROI predicted, awaiting confirmation "
                                  f"({s['roi_streak']}/{self.confirm})")
        elif roi_in_top:
            c, sc = roi_in_top
            s["roi_streak"] = s.get("roi_streak", 0) + 1
            if self.alert_on_topk and s["roi_streak"] >= self.confirm:
                # The ROI being a plausible next cell, even if not the single
                # most likely one, is worth notifying: a heading-based predictor
                # under-ranks sharp turns, and on the 5-UE evaluation set the ROI
                # never appeared in top-k for a UE that did not go on to enter it.
                s["level"] = "ALERT"
                if not s["alerted"]:
                    s["alerted"] = True
                    if supi not in self.notify:
                        self.notify.append(supi)
                self._emit(supi, "ALERT", s, pred=c, score=sc, hops=hops,
                           detail=f"ROI in top-{self.top_k} -- NOTIFICATION LIST")
            else:
                s["level"] = "WATCH"
                self._emit(supi, "WATCH", s, pred=c, score=sc, hops=hops,
                           detail=f"ROI in top-{self.top_k} predictions")
        elif hops is not None and hops <= self.horizon:
            if s["level"] not in ("WATCH", "ALERT"):
                s["level"] = "WATCH"
            self._emit(supi, "WATCH", s, pred=best, score=best_score, hops=hops,
                       detail=f"ROI within {self.horizon} TAI hop(s)")
        else:
            s["roi_streak"] = 0
            self._emit(supi, "TRACKING", s, pred=best, score=best_score, hops=hops)

        self._save()

    def on_loss(self, supi, reason):
        s = self._st(supi)
        s["last"] = reason

    def _save(self):
        state = {}
        for supi, s in self.ue.items():
            cur = s["hist"][-1] if s["hist"] else None
            state[supi] = {
                "level": s["level"], "tracking": s["tracking"],
                "entered_roi": s["entered"],
                "current": {"site": cur["name"], "tai": cur["tac"]} if cur else None,
                "tai_path": s["tais"],
                "cells": [h["name"] for h in s["hist"]],
            }
        with open(os.path.join(self.outdir, "tracker_state.json"), "w") as f:
            json.dump({"arm_tai": self.arm, "roi_tai": self.roi,
                       "notification_list": self.notify, "ues": state}, f, indent=1)

    def summary(self):
        print("\n" + "=" * 66)
        print(f"arm TAI {self.arm}  ->  ROI TAI {self.roi}")
        print(f"TAI adjacency of ROI: "
              f"{sorted(self.topo.tai_neigh[self.roi])}")
        print(f"hops from arm to ROI: {self.topo.tai_hops(self.arm, self.roi)}")
        print("-" * 66)
        for supi, s in self.ue.items():
            path = " -> ".join(f"T{t}" for t in s["tais"])
            print(f"{supi}  [{s['level']}]")
            print(f"   TAI path : {path}")
            print(f"   cells    : {' -> '.join(h['name'] for h in s['hist'])}")
        print("-" * 66)
        print(f"NOTIFICATION LIST ({len(self.notify)}): "
              f"{', '.join(self.notify) if self.notify else '(empty)'}")
        print("=" * 66)


# ---------------------------------------------------------------------- input
def parse_line(line):
    """Yield (kind, supi, payload) from one AF notification line."""
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    for rep in obj.get("reportList") or []:
        supi = rep.get("supi")
        if not supi:
            continue
        t = rep.get("type")
        if t == "LOCATION_REPORT":
            nr = (rep.get("location") or {}).get("nrLocation") or {}
            ncgi = (nr.get("ncgi") or {}).get("nrCellId")
            tac = (nr.get("tai") or {}).get("tac")
            if ncgi:
                yield ("loc", supi, {"ncgi": ncgi, "tac": tac})
        elif t == "LOSS_OF_CONNECTIVITY":
            yield ("loss", supi, {"reason": rep.get("lossOfConnectReason")})


def follow(path, from_start, poll=0.4):
    """Tail a growing file. Survives the AF being restarted (inode change)."""
    while not os.path.exists(path):
        print(f"waiting for {path} ...", flush=True)
        time.sleep(1.0)
    f = open(path, "r")
    if not from_start:
        f.seek(0, os.SEEK_END)
    ino = os.fstat(f.fileno()).st_ino
    while True:
        line = f.readline()
        if line:
            yield line
            continue
        time.sleep(poll)
        try:
            if os.stat(path).st_ino != ino:
                f.close()
                f = open(path, "r")
                ino = os.fstat(f.fileno()).st_ino
        except FileNotFoundError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tai-csv", required=True)
    ap.add_argument("--arm-tai", type=int, default=3)
    ap.add_argument("--roi-tai", type=int, default=11)
    ap.add_argument("--horizon", type=int, default=2,
                    help="raise WATCH when the ROI is within this many TAI hops")
    ap.add_argument("--top-k", type=int, default=3,
                    help="predictions considered; ROI appearing in them raises WATCH")
    ap.add_argument("--pattern",
                    help="TAI sequence to match, e.g. 3,10,11. Flags a UE once it "
                         "has matched all but the last element, so the alert fires "
                         "before arrival. Matching the ORDER makes it directional: "
                         "a UE travelling 11->10->3 never advances, so UEs leaving "
                         "the ROI are excluded. Overrides the predictor.")
    ap.add_argument("--confirm", type=int, default=1,
                    help="require the ROI to be predicted on this many consecutive "
                         "cell changes before notifying. 2 removes the "
                         "pass-through-and-turn-away false positive.")
    ap.add_argument("--alert-on-topk", action="store_true",
                    help="notify when the ROI appears anywhere in the top-k "
                         "predictions, not only as the single best. Catches sharp "
                         "turns that a heading-based predictor under-ranks.")
    ap.add_argument("--adj-factor", type=float, default=1.0,
                    help="cells are neighbours within this x (range_a + range_b); "
                         "raise it for a denser adjacency graph")
    ap.add_argument("--outdir", default="tracker_out")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tail", help="follow this notifications.jsonl as it grows")
    src.add_argument("--replay", help="read this notifications.jsonl once and exit")
    src.add_argument("--udp", help="listen on host:port for JSON datagrams")
    ap.add_argument("--from-start", action="store_true",
                    help="with --tail, also process what is already in the file")
    args = ap.parse_args()

    topo = Topology(args.tai_csv, adj_factor=args.adj_factor)
    tacs = sorted(topo.by_tac)
    print(f"topology: {len(topo.cells)} cells, {len(tacs)} TAIs {tacs}")
    if args.arm_tai not in topo.by_tac:
        sys.exit(f"arm TAI {args.arm_tai} has no cells")
    if args.roi_tai not in topo.by_tac:
        sys.exit(f"ROI TAI {args.roi_tai} has no cells")
    hops = topo.tai_hops(args.arm_tai, args.roi_tai)
    print(f"arm TAI {args.arm_tai} "
          f"({len(topo.by_tac[args.arm_tai])} cells) -> "
          f"ROI TAI {args.roi_tai} ({len(topo.by_tac[args.roi_tai])} cells)")
    print(f"ROI is adjacent to TAIs {sorted(topo.tai_neigh[args.roi_tai])}; "
          f"{hops} hop(s) from the arm TAI")
    if hops is not None and hops > 1:
        print(f"  note: the ROI is NOT directly adjacent to the arm TAI, so a "
              f"one-shot next-TAI check would never fire.\n"
              f"  Tracking stays armed and re-predicts on every cell change.")
    print()

    pat = None
    if args.pattern:
        pat = [int(x) for x in re.findall(r"\d+", args.pattern)]
        if len(pat) < 2:
            sys.exit("--pattern needs at least two TAIs, e.g. 3,10,11")
        missing = [t for t in pat if t not in topo.by_tac]
        if missing:
            sys.exit(f"--pattern references TAI(s) with no cells: {missing}")
        print(f"TAI pattern mode: {' -> '.join(str(p) for p in pat)}  "
              f"(alert at {len(pat)-1}/{len(pat)}, before reaching TAI {pat[-1]})")
        print()

    tr = Tracker(topo, args.arm_tai, args.roi_tai, args.horizon, args.top_k,
                 args.outdir, alert_on_topk=args.alert_on_topk,
                 confirm=args.confirm, pattern=pat)

    def handle(line):
        for kind, supi, p in parse_line(line):
            if kind == "loc":
                gid = topo.decode(p["ncgi"])
                if gid is not None:
                    tr.on_location(supi, gid)
            elif kind == "loss":
                tr.on_loss(supi, p.get("reason"))

    try:
        if args.replay:
            if not os.path.exists(args.replay):
                sys.exit(f"{args.replay} not found")
            n = 0
            for line in open(args.replay):
                handle(line)
                n += 1
            print(f"\nreplayed {n} notification line(s)")
        elif args.tail:
            print(f"tailing {args.tail}  (Ctrl-C to stop)\n")
            for line in follow(args.tail, args.from_start):
                handle(line)
        else:
            host, _, port = args.udp.rpartition(":")
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((host or "0.0.0.0", int(port)))
            print(f"listening on udp {host or '0.0.0.0'}:{port}  (Ctrl-C to stop)\n")
            while True:
                data, _ = s.recvfrom(65535)
                handle(data.decode("utf-8", "replace"))
    except KeyboardInterrupt:
        print()
    finally:
        tr._save()
        tr.summary()
        print(f"\nalerts  -> {tr.alerts_path}")
        print(f"state   -> {os.path.join(args.outdir, 'tracker_state.json')}")


if __name__ == "__main__":
    main()