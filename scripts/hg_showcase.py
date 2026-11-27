#!/usr/bin/env python3
"""Save the Seed to Stash showcase snapshot for kc1fqa.com/hg.

Usage: python3 scripts/hg_showcase.py snapshot.json
Reads the snapshot the app writes (data/users/<uid>/core/pub/showcase), keeps only the
public fields, and writes assets/hg/showcase.json if anything but the timestamp changed.
Prints "changed" or "unchanged".
"""
import json, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets" / "hg" / "showcase.json"
PLANT = ["tag", "strain", "breeder", "cross", "pheno", "status", "keeper", "born", "flip", "hd", "ph"]
TENT = ["name", "kind", "start", "hv", "wkOff", "planFlip", "planHv", "sched", "recipe", "plants"]
LOT = ["strain", "tn", "chop", "cure", "stage", "rating", "ph"]
STRAIN = ["name", "rank", "breeder", "cross", "note", "where"]

def pick(o, keys):
    return {k: o[k] for k in keys if k in o}

def clean(d):
    ph = lambda xs: [pick(x, ["a", "d", "n"]) for x in (xs or [])]
    out = {"v": 1, "at": d.get("at", ""), "strains": [pick(x, STRAIN) for x in d.get("strains", [])], "tents": [], "lots": [], "past": []}
    for t in d.get("tents", []):
        t = pick(t, TENT)
        t["plants"] = [dict(pick(p, PLANT), ph=ph(p.get("ph"))) for p in t.get("plants", [])]
        out["tents"].append(t)
    out["lots"] = [dict(pick(l, LOT), ph=ph(l.get("ph"))) for l in d.get("lots", [])]
    out["past"] = [dict(pick(p, PLANT), ph=ph(p.get("ph"))) for p in d.get("past", [])]
    return out

new = clean(json.load(open(sys.argv[1])))
old = json.loads(OUT.read_text()) if OUT.exists() else None
same = old is not None and {k: v for k, v in old.items() if k != "at"} == {k: v for k, v in new.items() if k != "at"}
if not same:
    OUT.write_text(json.dumps(new, separators=(",", ":"), ensure_ascii=False) + "\n")
print("unchanged" if same else "changed")
