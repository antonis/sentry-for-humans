"""Inventory a FIT workout file: what signals are present, sample rate, gaps, ranges.

Read-only. No Sentry, no network. Just tells us what we're working with.

Usage: python inventory.py /path/to/workout.fit
"""

import glob
import os
import sys
from collections import Counter
from datetime import timezone

from fitparse import FitFile

# Fields we care about for "human telemetry". Maps FIT record field -> friendly name.
SIGNALS = {
    "heart_rate": "HR (bpm)",
    "cadence": "cadence (rpm)",
    "power": "power (W)",
    "speed": "speed (m/s)",
    "enhanced_speed": "speed_enh (m/s)",
    "altitude": "altitude (m)",
    "enhanced_altitude": "altitude_enh (m)",
    "temperature": "temp (C)",
    "distance": "distance (m)",
    "grade": "grade (%)",
    "position_lat": "gps_lat",
    "position_long": "gps_long",
}


def summarize(path: str) -> None:
    fit = FitFile(path)

    records = []  # list of dicts: field_name -> value for each 'record' message
    msg_types: Counter = Counter()
    session_info: dict = {}
    sport = None

    for msg in fit.get_messages():
        msg_types[msg.name] += 1
        if msg.name == "record":
            row = {d.name: d.value for d in msg}
            records.append(row)
        elif msg.name == "session":
            session_info = {d.name: d.value for d in msg}
        elif msg.name == "sport":
            sport = {d.name: d.value for d in msg}

    print(f"\n=== {path} ===")
    print(f"message types: {dict(msg_types)}")
    if sport:
        print(f"sport: {sport.get('sport')} / {sport.get('sub_sport')}")
    elif session_info:
        print(f"sport (session): {session_info.get('sport')} / {session_info.get('sub_sport')}")

    if not records:
        print("!! no 'record' messages found — this file has no per-sample track")
        return

    # Timing
    times = [r["timestamp"] for r in records if r.get("timestamp")]
    if times:
        t0, t1 = min(times), max(times)
        dur = (t1 - t0).total_seconds()
        gaps = []
        st = sorted(times)
        for a, b in zip(st, st[1:]):
            dt = (b - a).total_seconds()
            if dt > 5:  # gaps over 5s worth noting
                gaps.append(dt)
        print(f"\nsamples: {len(records)}")
        print(f"start:   {t0.astimezone(timezone.utc).isoformat()}")
        print(f"end:     {t1.astimezone(timezone.utc).isoformat()}")
        print(f"elapsed: {dur/60:.1f} min ({dur:.0f}s)")
        print(f"avg sample spacing: {dur/max(len(records)-1,1):.2f}s")
        if gaps:
            print(f"gaps >5s: {len(gaps)} (largest {max(gaps):.0f}s) — likely pauses/dropouts")
        else:
            print("gaps >5s: none")

    # Per-signal coverage + ranges
    print("\nsignal                 present   min      avg      max")
    print("-" * 58)
    for field, label in SIGNALS.items():
        vals = [r[field] for r in records if r.get(field) is not None]
        if not vals:
            continue
        coverage = 100 * len(vals) / len(records)
        if field in ("position_lat", "position_long"):
            # semicircles -> degrees, just show it's present
            print(f"{label:22} {coverage:5.0f}%   (gps present)")
            continue
        try:
            lo, hi = min(vals), max(vals)
            avg = sum(vals) / len(vals)
            print(f"{label:22} {coverage:5.0f}%   {lo:7.1f}  {avg:7.1f}  {hi:7.1f}")
        except TypeError:
            print(f"{label:22} {coverage:5.0f}%   (non-numeric)")

    # Which of the "interesting" signals are missing — matters for error rules
    have = {f for f in SIGNALS if any(r.get(f) is not None for r in records)}
    interesting = {"heart_rate", "power", "cadence"}
    missing = interesting - have
    if missing:
        print(f"\nnote: missing {sorted(missing)} — some 'error' rules won't apply")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        found = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "*.fit")))
        path = found[0] if found else None
    if not path:
        sys.exit("usage: python inventory.py /path/to/workout.fit  (or drop one in ./data)")
    summarize(path)
