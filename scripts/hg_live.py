#!/usr/bin/env python3
"""Pull current grow-tent readings and append them to the live data files.

Runs on a schedule in GitHub Actions (.github/workflows/hg-live.yml).
Writes into a data folder (the hg-data branch, checked out at ./data):

  live/<slug>.csv    time,temp_f,rh,vpd   (local time, America/New_York)
  live/status.json   when each device was last read, and whether it was online

Sources
  AC Infinity  unofficial cloud API used by the AC Infinity app (same calls as the
               Home Assistant integration github.com/dalinicus/homeassistant-acinfinity).
               Gives the current reading only, so this keeps one row per run.
  Pulse        official API (api.pulsegrow.com, needs an API key from a paid plan).
               Gives minute data, so each run backfills the last few hours.
  Govee        official Developer API (openapi.api.govee.com). Current reading only.
               Not every model reports through it (Wi-Fi thermo-hygrometers usually do).

Secrets (repo Settings > Secrets and variables > Actions)
  ACI_EMAIL, ACI_PASSWORD   AC Infinity app login
  PULSE_API_KEY             Pulse API key (optional)
  GOVEE_API_KEY             Govee Developer API key (optional)

Uses only the Python standard library.
"""

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
KEEP_DAYS = 45                      # live files keep this much history
DATA_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "data") / "live"

# Device name in the app -> file name on the site. Unlisted devices are still
# saved, under a name made from their app name (e.g. "Bloom 4x2" -> aci-bloom-4x2).
DEVICES = {
    "Bloom 3x3": "bloom1",
    "Veg": "veg",
    "4x2": "bloom2",                # Bloom 2, coming online soon
    "Edd Veg": "veg_room",          # Pulse sensor in the mom room lung room
}

ACI_HOST = "https://www.acinfinityserver.com"
PULSE_HOST = "https://api.pulsegrow.com"
GOVEE_HOST = "https://openapi.api.govee.com"
FIELDS = ["time", "temp_f", "rh", "vpd"]
OPTIONAL_FIELDS = ["co2"]


def slug_for(name, prefix):
    if name in DEVICES:
        return DEVICES[name]
    return prefix + "-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "device")


def http(method, url, headers=None, data=None, timeout=30, as_json=False):
    if data is None:
        body = None
    elif as_json:
        body = json.dumps(data).encode()
        headers = {**(headers or {}), "Content-Type": "application/json"}
    else:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def local_minute(dt):
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def c_to_f(c):
    return c * 9 / 5 + 32


# ---------------- AC Infinity ----------------
def read_ac_infinity(email, password):
    ua = {"User-Agent": "okhttp/4.12.0"}
    # The app truncates passwords to 25 characters; the API expects the same.
    login = http("POST", ACI_HOST + "/api/user/appUserLogin", ua, {"appEmail": email, "appPasswordl": password[:25]})
    if login.get("code") != 200:
        raise RuntimeError(f"AC Infinity login refused (code {login.get('code')}): {login.get('msg')}")
    token = login["data"]["appId"]
    body = http("POST", ACI_HOST + "/api/user/devInfoListAll", {**ua, "token": token}, {"userId": token})
    if body.get("code") != 200:
        raise RuntimeError(f"AC Infinity device list failed (code {body.get('code')}): {body.get('msg')}")

    now = datetime.now(timezone.utc)
    out = []
    for c in body.get("data") or []:
        name = c.get("devName") or str(c.get("devId"))
        info = c.get("deviceInfo") or {}
        online = bool(c.get("online", info.get("online", 1)))
        t, h, v = info.get("temperature"), info.get("humidity"), info.get("vpdnums")
        row = None
        if online and t is not None and h is not None:
            # Values come as integers x100; temperature in Celsius.
            row = {"time": local_minute(now), "temp_f": round(c_to_f(t / 100), 2), "rh": round(h / 100, 2),
                   "vpd": round(v / 100, 2) if v is not None else ""}
        out.append({"source": "AC Infinity", "name": name, "slug": slug_for(name, "aci"), "online": online, "rows": [row] if row else []})
    return out


# ---------------- Pulse ----------------
def read_pulse(api_key, hours=3):
    hdr = {"X-API-Key": api_key, "Accept": "application/json"}
    devices = http("GET", PULSE_HOST + "/devices/details", hdr)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    q = urllib.parse.urlencode({"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")})
    out = []
    for d in devices or []:
        name = d.get("name") or str(d.get("id"))
        pts = http("GET", f"{PULSE_HOST}/devices/{d['id']}/data-range?{q}", hdr) or []
        rows = []
        for p in pts:
            ts = p.get("createdAt")
            if not ts or p.get("temperatureF") is None:
                continue
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            rows.append({"time": local_minute(dt), "temp_f": round(p["temperatureF"], 2), "rh": round(p.get("humidityRh") or 0, 1),
                         "vpd": round(p["vpd"], 2) if p.get("vpd") is not None else ""})
        out.append({"source": "Pulse", "name": name, "slug": slug_for(name, "pulse"), "online": bool(rows), "rows": rows})
    return out


# ---------------- Govee ----------------
def vpd_kpa(temp_f, rh):
    import math
    c = (temp_f - 32) * 5 / 9
    return 0.6108 * math.exp(17.27 * c / (c + 237.3)) * (1 - rh / 100)


def read_govee(api_key):
    hdr = {"Govee-API-Key": api_key}
    devs = http("GET", GOVEE_HOST + "/router/api/v1/user/devices", hdr)
    if devs.get("code") != 200:
        raise RuntimeError(f"Govee device list failed (code {devs.get('code')}): {devs.get('message') or devs.get('msg')}")
    now = datetime.now(timezone.utc)
    out = []
    for d in devs.get("data") or []:
        caps = {c.get("instance") for c in d.get("capabilities") or []}
        if "sensorTemperature" not in caps:
            continue  # lights, plugs, etc.
        name = d.get("deviceName") or d.get("device")
        st = http("POST", GOVEE_HOST + "/router/api/v1/device/state", hdr,
                  {"requestId": str(uuid.uuid4()), "payload": {"sku": d["sku"], "device": d["device"]}}, as_json=True)
        vals = {}
        for c in (st.get("payload") or {}).get("capabilities") or []:
            v = (c.get("state") or {}).get("value")
            if isinstance(v, dict):  # some models wrap it, e.g. {"currentHumidity": 55}
                v = next((x for x in v.values() if isinstance(x, (int, float))), None)
            if isinstance(v, (int, float)):
                vals[c.get("instance")] = float(v)
        t, h = vals.get("sensorTemperature"), vals.get("sensorHumidity")
        row = None
        if t is not None and h is not None:
            temp_f = c_to_f(t) if t < 45 else t   # the API reports °F for most models, °C for a few
            row = {"time": local_minute(now), "temp_f": round(temp_f, 2), "rh": round(h, 1), "vpd": round(vpd_kpa(temp_f, h), 2)}
            if "carbonDioxideConcentration" in vals:
                row["co2"] = round(vals["carbonDioxideConcentration"])
        out.append({"source": f"Govee {d.get('sku', '')}".strip(), "name": name, "slug": slug_for(name, "govee"),
                    "online": row is not None, "rows": [row] if row else []})
    return out


# ---------------- Files ----------------
def merge(slug, rows):
    path = DATA_DIR / f"{slug}.csv"
    have, fields = {}, list(FIELDS)
    if path.exists():
        with path.open() as f:
            rd = csv.DictReader(f)
            fields += [x for x in OPTIONAL_FIELDS if x in (rd.fieldnames or []) and x not in fields]
            for r in rd:
                have[r["time"]] = r
    for r in rows:
        fields += [x for x in OPTIONAL_FIELDS if x in r and x not in fields]
        have[r["time"]] = r
    cutoff = (datetime.now(TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M")
    keep = [have[k] for k in sorted(have) if k >= cutoff]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(keep)
    return len(keep)


def main():
    # Secrets pasted into GitHub often carry a trailing newline or spaces
    for k in ("ACI_EMAIL", "ACI_PASSWORD", "PULSE_API_KEY", "GOVEE_API_KEY"):
        if k in os.environ:
            os.environ[k] = os.environ[k].strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status_path = DATA_DIR / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    status.setdefault("devices", {})
    errors = []

    readings = []
    if os.environ.get("ACI_EMAIL") and os.environ.get("ACI_PASSWORD"):
        try:
            readings += read_ac_infinity(os.environ["ACI_EMAIL"], os.environ["ACI_PASSWORD"])
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as e:
            errors.append(f"AC Infinity: {e}")
    else:
        print("AC Infinity: no ACI_EMAIL / ACI_PASSWORD secrets set, skipping")

    if os.environ.get("PULSE_API_KEY"):
        try:
            readings += read_pulse(os.environ["PULSE_API_KEY"])
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as e:
            errors.append(f"Pulse: {e}")
    else:
        print("Pulse: no PULSE_API_KEY secret set, skipping")

    if os.environ.get("GOVEE_API_KEY"):
        try:
            readings += read_govee(os.environ["GOVEE_API_KEY"])
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as e:
            errors.append(f"Govee: {e}")
    else:
        print("Govee: no GOVEE_API_KEY secret set, skipping")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in readings:
        n = merge(d["slug"], d["rows"]) if d["rows"] else None
        last = d["rows"][-1] if d["rows"] else None
        prev = status["devices"].get(d["slug"], {})
        status["devices"][d["slug"]] = {
            "name": d["name"], "source": d["source"], "online": d["online"], "checked": now,
            "lastReading": last or prev.get("lastReading"),
        }
        print(f"{d['source']}: {d['name']} -> {d['slug']}: " + (f"{last['temp_f']}°F {last['rh']}% RH, {n} rows kept" if last else "no reading (offline?)"))

    status["updated"] = now
    status["errors"] = errors
    status_path.write_text(json.dumps(status, indent=1, sort_keys=True) + "\n")
    for e in errors:
        print("ERROR", e)
    # Fail the run only if every configured source failed, so GitHub emails about real outages.
    if errors and not any(d["rows"] for d in readings):
        sys.exit(1)


if __name__ == "__main__":
    main()
