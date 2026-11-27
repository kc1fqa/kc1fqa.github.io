#!/usr/bin/env python3
"""Seed to Stash climate import. As of Nov 27 2026 Ed's AC Infinity exports go to the app only
(not /hg). Run this on the CSV and write the result to the app database with db_write (set or merge):
  data/users/u_CdsNX_0Pay4npL3isTXUIQ/core/env/<tentId>             {"d": env}
  data/users/u_CdsNX_0Pay4npL3isTXUIQ/core/envh/<tentId>_<yyyymm>   {"d": envh[yyyymm]}
Tent ids: Bloom 1 mwmpv0j7imqg, Bloom 2 mwmpv0j75e3a, The Nursery mwmpv0j7xwh3.
Merge into existing docs (db_get first): keep other dates; on an overlapping date, hours in the new
file replace those hours and other hours stay; rebuild that day's summary (min of lows, max of highs).

Turn AC Infinity / site CSVs (time,temp_f,rh[,vpd]) into Seed to Stash climate docs.
Usage: env_to_app.py OUT.json FROM_DATE file.csv [file.csv ...]
Writes {"env": {date: [tAvg,tLo,tHi,rhAvg,rhLo,rhHi,vpdAvg,co2]}, "envh": {"yyyymm": {date: [[t,rh] or 0 x24]}}}
matching readEnv() in the app. Only dates >= FROM_DATE are kept."""
import csv, json, math, sys
from collections import defaultdict
from datetime import datetime

def svp(c): return 0.6108 * math.exp(17.27 * c / (c + 237.3))
def vpd(t, rh): return svp((t - 32) * 5 / 9) * (1 - rh / 100)
out, frm, files = sys.argv[1], sys.argv[2], sys.argv[3:]
rows = {}
for f in files:
    for r in csv.DictReader(open(f)):
        try:
            ts = datetime.strptime(r["time"].strip()[:16], "%Y-%m-%d %H:%M")
            t, rh = float(r["temp_f"]), float(r["rh"])
        except Exception:
            continue
        v = float(r["vpd"]) if r.get("vpd") not in (None, "") else vpd(t, rh)
        rows[ts] = (t, rh, v)
day, hour = defaultdict(list), defaultdict(lambda: [[] for _ in range(24)])
for ts, x in rows.items():
    d = ts.strftime("%Y-%m-%d")
    if d < frm: continue
    day[d].append(x); hour[d][ts.hour].append(x)
# skip days with too few readings to summarize (e.g. a handful of live polls)
for d in [d for d, a in day.items() if len(a) < 60]:
    del day[d]; del hour[d]
r1 = lambda x: round(x, 1)
env = {d: [r1(sum(x[0] for x in a) / len(a)), r1(min(x[0] for x in a)), r1(max(x[0] for x in a)),
           r1(sum(x[1] for x in a) / len(a)), r1(min(x[1] for x in a)), r1(max(x[1] for x in a)),
           round(sum(x[2] for x in a) / len(a), 2), None] for d, a in sorted(day.items())}
envh = defaultdict(dict)
for d, hs in sorted(hour.items()):
    envh[d[:7].replace("-", "")][d] = [[r1(sum(x[0] for x in a) / len(a)), r1(sum(x[1] for x in a) / len(a))] if a else 0 for a in hs]
json.dump({"env": env, "envh": envh}, open(out, "w"), separators=(",", ":"))
print(f"{len(rows)} readings, {len(env)} days {min(env) if env else '-'}..{max(env) if env else '-'}, env {len(json.dumps(env))}B, envh " + ", ".join(f"{k}:{len(json.dumps(v))}B" for k, v in envh.items()))
