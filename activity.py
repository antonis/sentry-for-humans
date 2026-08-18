"""Load a FIT activity, segment it, and detect 'error' events — sport-agnostic.

No sport branches anywhere. Terrain is measured by grade, effort by the
activity's OWN heart-rate distribution, stops by recording gaps and low speed.
So a bike ride, an open-water swim, and a mountain hike all run through the
exact same path and produce sport-appropriate results, because the thresholds
are derived from each activity's own data.

Still fully local: no Sentry, no network. Prints each activity as a span tree
with the detected events marked, the way it will look as a Sentry trace.

Usage:
    python activity.py                 # run all known files
    python activity.py path/to.fit ... # run specific files
"""

from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fitparse import FitFile

# --- Tunables (all sport-agnostic; printed at runtime so they're never hidden) ---
ALT_SMOOTH_WIN = 9        # samples, centered moving average on altitude
GRADE_WIN_M = 20.0        # half-window (m) over which grade is measured
CLIMB_GRADE = 3.0         # % grade above which a stretch is a CLIMB (below -this: DESCENT)
GAP_S = 10.0              # a recording gap longer than this breaks a sustained run
STOP_SPAN_MIN = 60.0      # only a gap/stop at least this long becomes its own span
MIN_SEG_S = 30.0          # segments shorter than this get merged into neighbours
LONG_STOP_S = 90.0        # a STOP at least this long is a 'long stop' event
REDLINE_PCTL = 90         # HR at/above this percentile (of moving samples) is 'hot'
REDLINE_S = 30.0          # HR must stay hot this long to count as a redline event
SURGE_PCTL = 98           # speed at/above this percentile (of THIS activity) is a 'surge'
SURGE_S = 3.0             # sustained this long to count

CLIMB, DESCENT, FLAT, STOP = "climb", "descent", "flat", "stop"

# Drop your own .fit exports (Garmin Connect / Strava → "Export Original") into
# ./data and they're picked up automatically. Nothing here is sport-specific.
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FILES = sorted(glob.glob(os.path.join(DATA_DIR, "*.fit")))


@dataclass
class Sample:
    t: datetime
    hr: float | None
    speed: float | None       # m/s
    alt: float | None         # m (raw)
    dist: float | None        # m (cumulative)
    cadence: float | None
    lat: float | None
    lng: float | None
    alt_s: float = 0.0        # smoothed altitude, filled later
    grade: float = 0.0        # %, filled later
    state: str = FLAT         # filled later


@dataclass
class Segment:
    state: str
    start: int                # sample index (inclusive)
    end: int                  # sample index (inclusive)
    dur_s: float
    dist_m: float
    avg_hr: float | None
    is_gap: bool = False      # a STOP synthesised from a recording gap
    events: list[str] = field(default_factory=list)


def _pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load(path: str) -> tuple[str, list[Sample]]:
    fit = FitFile(path)
    sport = "unknown"
    rows: list[Sample] = []
    for msg in fit.get_messages():
        if msg.name == "sport":
            sport = {d.name: d.value for d in msg}.get("sport") or sport
        elif msg.name == "record":
            r = {d.name: d.value for d in msg}
            t = r.get("timestamp")
            if t is None:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            lat = r.get("position_lat")
            lng = r.get("position_long")
            rows.append(
                Sample(
                    t=t,
                    hr=r.get("heart_rate"),
                    speed=r.get("enhanced_speed", r.get("speed")),
                    alt=r.get("enhanced_altitude", r.get("altitude")),
                    dist=r.get("distance"),
                    cadence=r.get("cadence"),
                    # FIT stores position in semicircles; convert to degrees
                    lat=lat * (180.0 / 2**31) if lat is not None else None,
                    lng=lng * (180.0 / 2**31) if lng is not None else None,
                )
            )
    rows.sort(key=lambda s: s.t)
    return sport, rows


def enrich(samples: list[Sample]) -> None:
    """Smooth altitude and compute per-sample grade + terrain state."""
    n = len(samples)
    # Smooth altitude (centered moving average, skipping Nones)
    alts = [s.alt for s in samples]
    half = ALT_SMOOTH_WIN // 2
    for i in range(n):
        window = [alts[j] for j in range(max(0, i - half), min(n, i + half + 1)) if alts[j] is not None]
        samples[i].alt_s = sum(window) / len(window) if window else (alts[i] or 0.0)

    # Stop-speed threshold, relative to this activity's own median moving speed
    moving_speeds = [s.speed for s in samples if s.speed and s.speed > 0.1]
    median = _pctl(moving_speeds, 50) if moving_speeds else 0.0
    stop_speed = max(0.15, 0.25 * median)

    # Grade over a distance window, then classify terrain
    for i in range(n):
        s = samples[i]
        if s.dist is None or (s.speed is not None and s.speed < stop_speed):
            s.grade = 0.0
            s.state = STOP if (s.speed is not None and s.speed < stop_speed) else FLAT
            continue
        # walk back/forward until we've covered GRADE_WIN_M of distance each way
        a = i
        while a > 0 and samples[a].dist is not None and s.dist - samples[a].dist < GRADE_WIN_M:
            a -= 1
        b = i
        while b < n - 1 and samples[b].dist is not None and samples[b].dist - s.dist < GRADE_WIN_M:
            b += 1
        dd = (samples[b].dist or 0) - (samples[a].dist or 0)
        s.grade = ((samples[b].alt_s - samples[a].alt_s) / dd * 100.0) if dd > 1 else 0.0
        if s.grade > CLIMB_GRADE:
            s.state = CLIMB
        elif s.grade < -CLIMB_GRADE:
            s.state = DESCENT
        else:
            s.state = FLAT


def _seg_stats(samples: list[Sample], a: int, b: int) -> tuple[float, float, float | None]:
    dur = (samples[b].t - samples[a].t).total_seconds()
    dist = (samples[b].dist or 0) - (samples[a].dist or 0)
    hrs = [samples[j].hr for j in range(a, b + 1) if samples[j].hr is not None]
    return dur, dist, (sum(hrs) / len(hrs) if hrs else None)


def segment(samples: list[Sample]) -> list[Segment]:
    n = len(samples)
    if n < 2:
        return []
    segs: list[Segment] = []
    start = 0
    for i in range(1, n):
        gap = (samples[i].t - samples[i - 1].t).total_seconds()
        boundary = samples[i].state != samples[start].state
        if gap >= STOP_SPAN_MIN:
            # a real pause: close current run, insert a gap-STOP for the paused interval.
            # shorter gaps are absorbed into the surrounding segment (no span confetti).
            if i - 1 >= start:
                dur, dist, hr = _seg_stats(samples, start, i - 1)
                segs.append(Segment(samples[start].state, start, i - 1, dur, dist, hr))
            segs.append(Segment(STOP, i - 1, i, gap, 0.0, None, is_gap=True))
            start = i
        elif boundary:
            dur, dist, hr = _seg_stats(samples, start, i - 1)
            segs.append(Segment(samples[start].state, start, i - 1, dur, dist, hr))
            start = i
    dur, dist, hr = _seg_stats(samples, start, n - 1)
    segs.append(Segment(samples[start].state, start, n - 1, dur, dist, hr))

    # Merge tiny non-gap segments into the previous one to avoid confetti
    merged: list[Segment] = []
    for s in segs:
        if merged and not s.is_gap and not merged[-1].is_gap and s.dur_s < MIN_SEG_S:
            prev = merged[-1]
            prev.end = s.end
            prev.dur_s, prev.dist_m, prev.avg_hr = _seg_stats(samples, prev.start, prev.end)
        elif merged and merged[-1].state == s.state and not s.is_gap and not merged[-1].is_gap:
            prev = merged[-1]
            prev.end = s.end
            prev.dur_s, prev.dist_m, prev.avg_hr = _seg_stats(samples, prev.start, prev.end)
        else:
            merged.append(s)
    return merged


def detect_events(samples: list[Sample], segs: list[Segment]) -> list[dict]:
    """Sport-agnostic events, all thresholds derived from this activity's data."""
    events: list[dict] = []
    moving_hr = [s.hr for s in samples if s.hr is not None and s.speed and s.speed > 0.1]
    hr_hot = _pctl(moving_hr, REDLINE_PCTL) if moving_hr else None
    speeds = [s.speed for s in samples if s.speed is not None]
    spd_hi = _pctl(speeds, SURGE_PCTL) if speeds else None

    def km(i: int) -> float:
        return (samples[i].dist or 0) / 1000.0

    # Long stops (one event per qualifying STOP segment)
    for seg in segs:
        if seg.state == STOP and seg.dur_s >= LONG_STOP_S:
            seg.events.append(f"long stop {seg.dur_s/60:.1f}m")
            events.append({"type": "long_stop", "label": f"long stop {seg.dur_s/60:.1f}m",
                           "at_km": km(seg.start), "at_t": samples[seg.start].t,
                           "dur_s": seg.dur_s})

    # Sustained-run helper over a per-sample boolean
    def sustained(flag, min_s: float, label_type: str, describe):
        i, n = 0, len(samples)
        while i < n:
            if not flag(samples[i]):
                i += 1
                continue
            j = i
            while j + 1 < n and flag(samples[j + 1]) and \
                    (samples[j + 1].t - samples[j].t).total_seconds() <= GAP_S:
                j += 1
            dur = (samples[j].t - samples[i].t).total_seconds()
            if dur >= min_s:
                ev = describe(i, j, dur)
                ev.update({"type": label_type, "at_km": km(i),
                           "at_t": samples[i].t, "dur_s": dur})
                events.append(ev)
                # mark it on whichever segment contains the run's start
                for seg in segs:
                    if seg.start <= i <= seg.end:
                        seg.events.append(ev["label"])
                        break
            i = j + 1

    if hr_hot:
        peak = lambda a, b: max(s.hr for s in samples[a:b + 1] if s.hr is not None)
        sustained(
            lambda s: s.hr is not None and s.hr >= hr_hot,
            REDLINE_S, "redline",
            lambda a, b, d: {"label": f"redline {d:.0f}s (HR {peak(a, b):.0f})",
                             "peak_hr": peak(a, b)},
        )
    if spd_hi and spd_hi > 0:
        top = lambda a, b: max(s.speed for s in samples[a:b + 1] if s.speed is not None)
        sustained(
            lambda s: s.speed is not None and s.speed >= spd_hi,
            SURGE_S, "surge",
            lambda a, b, d: {"label": f"surge {top(a, b)*3.6:.0f} km/h",
                             "peak_kmh": top(a, b) * 3.6},
        )
    return events


def analyze(path: str) -> tuple[str, list[Sample], list[Segment], list[dict]]:
    """Full pipeline: load → enrich → segment → detect events. Reused by ingest."""
    sport, samples = load(path)
    if len(samples) < 2:
        return sport, samples, [], []
    enrich(samples)
    segs = segment(samples)
    events = detect_events(samples, segs)
    return sport, samples, segs, events


def render(path: str) -> None:
    sport, samples, segs, events = analyze(path)
    if len(samples) < 2:
        print(f"{path}: too few samples")
        return

    t0, t1 = samples[0].t, samples[-1].t
    elapsed = (t1 - t0).total_seconds()
    total_dist = (samples[-1].dist or 0) / 1000.0
    moving = sum(s.dur_s for s in segs if s.state != STOP)
    avg_hr = sum(s.hr for s in samples if s.hr) / max(1, sum(1 for s in samples if s.hr))

    name = path.rsplit("/", 1)[-1].replace(".fit", "").replace("_", " ").strip()
    print(f"\n╭─ {name}  ·  {sport}")
    print(f"│  {t0.astimezone(timezone.utc):%Y-%m-%d %H:%MZ}  ·  "
          f"{elapsed/60:.0f}m elapsed ({moving/60:.0f}m moving)  ·  "
          f"{total_dist:.1f} km  ·  avg HR {avg_hr:.0f}")
    print("│")

    glyph = {CLIMB: "▲", DESCENT: "▼", FLAT: "─", STOP: "■"}
    for k, seg in enumerate(segs):
        last = k == len(segs) - 1
        elbow = "╰─" if last else "├─"
        dkm = f"{seg.dist_m/1000:5.2f} km" if seg.state != STOP else " " * 8
        hr = f"HR {seg.avg_hr:.0f}" if seg.avg_hr else "     "
        tag = "  ⚠ " + ", ".join(seg.events) if seg.events else ""
        label = f"{seg.state:7}" + ("(gap)" if seg.is_gap else "     ")
        print(f"{elbow} {glyph[seg.state]} {label} {seg.dur_s/60:5.1f}m  {dkm}  {hr}{tag}")

    # Issue-style summary
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print("│")
    print(f"╰─ {len(segs)} spans  ·  " +
          (", ".join(f"{v}× {k}" for k, v in by_type.items()) if by_type else "no events"))


if __name__ == "__main__":
    paths = sys.argv[1:] or FILES
    print(f"thresholds: climb>{CLIMB_GRADE}% · redline≥p{REDLINE_PCTL} for {REDLINE_S:.0f}s "
          f"· long-stop≥{LONG_STOP_S:.0f}s · surge≥p{SURGE_PCTL}")
    for p in paths:
        render(p)
