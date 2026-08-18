"""Ingest an activity into Sentry: the workout becomes a trace, its segments
become spans, and each detected event becomes an issue.

Sport-agnostic — it just consumes the structure `activity.analyze()` produces.

Runs as a DRY RUN by default (no DSN needed): it prints the exact trace and
issues it *would* send. Provide a DSN to actually ingest.

    # see what would be sent, no network:
    python sentry_ingest.py

    # really send it (all four files), backdated to the real activity dates:
    SENTRY_DSN=https://...  python sentry_ingest.py --send

    # Sentry drops events older than ~30 days. --shift restages each activity
    # to end a day apart near 'now', preserving its internal timeline, so even
    # the June hike lands. Use for demos.
    SENTRY_DSN=https://...  python sentry_ingest.py --send --shift
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

from activity import CLIMB, DESCENT, FLAT, STOP, FILES, analyze

# Which detected events map to which Sentry level. Level is cosmetic here —
# every captured event becomes an issue; the fingerprint controls grouping.
LEVEL = {"redline": "warning", "long_stop": "info", "surge": "info"}
SPAN_OP = {CLIMB: "segment.climb", DESCENT: "segment.descent",
           FLAT: "segment.flat", STOP: "segment.stop"}


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1].replace(".fit", "").replace("_", " ").strip()


def build_plan(path: str, end_at: datetime | None = None) -> dict | None:
    """Turn one activity into a serialisable plan: trace + spans + measurements + issues.

    end_at, if given, restages the activity so it ends then (durations preserved).
    """
    sport, samples, segs, events = analyze(path)
    if len(samples) < 2:
        return None

    real_start, real_end = samples[0].t, samples[-1].t
    shift = (end_at - real_end) if end_at else timedelta(0)

    def at(t: datetime) -> datetime:
        return t + shift

    spans = []
    for seg in segs:
        s0, s1 = samples[seg.start].t, samples[seg.end].t
        data = {"duration_s": round(seg.dur_s, 1)}
        if seg.state != STOP:
            data["distance_m"] = round(seg.dist_m, 1)
        if seg.avg_hr is not None:
            data["avg_hr"] = round(seg.avg_hr, 1)
        desc = f"{seg.state}" + (" (gap)" if seg.is_gap else "")
        spans.append({"op": SPAN_OP[seg.state], "description": desc,
                      "start": at(s0), "end": at(s1), "data": data,
                      "events": list(seg.events)})

    hrs = [s.hr for s in samples if s.hr is not None]
    elapsed = (real_end - real_start).total_seconds()
    moving = sum(s["data"]["duration_s"] for s in spans
                 if not s["op"].endswith("stop"))
    dist_m = samples[-1].dist or 0
    max_speed = max((s.speed for s in samples if s.speed is not None), default=0)
    measurements = {
        "distance": (round(dist_m / 1000.0, 2), "kilometer"),
        "elapsed": (round(elapsed / 60, 1), "minute"),
        "moving": (round(moving / 60, 1), "minute"),
        # km/h; query in Explore as tags[max_speed,number]:>40 (see send_plan)
        "avg_speed": (round(dist_m / moving * 3.6, 1), "none") if moving else (0, "none"),
        "max_speed": (round(max_speed * 3.6, 1), "none"),
        "avg_hr": (round(sum(hrs) / len(hrs), 0), "none") if hrs else (0, "none"),
        "max_hr": (max(hrs), "none") if hrs else (0, "none"),
        "segments": (len(segs), "none"),
    }

    issues = []
    for e in events:
        issues.append({
            "type": e["type"],
            "message": f"{e['label']} — {sport}",
            "level": LEVEL.get(e["type"], "info"),
            "timestamp": at(e["at_t"]),
            "fingerprint": [e["type"]],  # groups all redlines together, etc.
            "tags": {
                "sport": sport,
                "activity": _name(path),
                "at_km": round(e.get("at_km", 0), 1),
                **({"peak_hr": int(e["peak_hr"])} if "peak_hr" in e else {}),
                **({"peak_kmh": round(e["peak_kmh"], 1)} if "peak_kmh" in e else {}),
            },
        })

    return {"name": _name(path), "sport": sport, "trace_op": "activity",
            "start": at(real_start), "end": at(real_end), "real_start": real_start,
            "spans": spans, "measurements": measurements, "issues": issues}


def print_plan(plan: dict) -> None:
    print(f"\n╭─ TRACE  {plan['name']}  ·  op={plan['trace_op']}  ·  sport={plan['sport']}")
    win = f"{plan['start']:%Y-%m-%d %H:%MZ} → {plan['end']:%H:%MZ}"
    if plan["start"].date() != plan["real_start"].date():
        win += f"   (restaged from {plan['real_start']:%Y-%m-%d})"
    print(f"│  {win}")
    print(f"│  measurements: " + ", ".join(
        f"{k}={v}{'' if u == 'none' else u[:3]}"
        for k, (v, u) in plan["measurements"].items()))
    print(f"│  {len(plan['spans'])} spans:")
    for s in plan["spans"]:
        mark = "  ⚠ " + ", ".join(s["events"]) if s["events"] else ""
        d = s["data"]
        stat = f"{d['duration_s']:.0f}s" + (f" {d['distance_m']:.0f}m" if "distance_m" in d else "")
        print(f"│    {s['op']:16} {stat:14} {mark}")
    print(f"│  {len(plan['issues'])} issues (grouped by fingerprint):")
    by_fp: dict[str, int] = {}
    for i in plan["issues"]:
        by_fp[i["fingerprint"][0]] = by_fp.get(i["fingerprint"][0], 0) + 1
    for fp, n in by_fp.items():
        example = next(i for i in plan["issues"] if i["fingerprint"][0] == fp)
        print(f"│    {fp:10} ×{n:<3} e.g. \"{example['message']}\"  tags={example['tags']}")
    print("╰─")


def send_plan(plan: dict, trace_id: str | None = None,
              replay_id: str | None = None) -> list[str]:
    """Send the trace + issues. If trace_id is given, the transaction and every
    issue share it (so a replay can link them). If replay_id is given, the
    transaction and issues carry contexts.replay.replay_id — this is what the
    replay's Trace tab actually searches on (spans query `replay.id:<id>`),
    NOT the replay's own trace_ids. Returns the issue event ids."""
    import sentry_sdk
    from sentry_sdk.tracing import Transaction

    tx = sentry_sdk.start_transaction(Transaction(
        name=plan["name"], op=plan["trace_op"], start_timestamp=plan["start"],
        trace_id=trace_id, sampled=True))
    tx.set_tag("sport", plan["sport"])
    if replay_id:
        # makes the transaction's spans carry the `replay.id` attribute the
        # replay Trace tab filters on, so this trace links back to the replay
        tx.set_context("replay", {"replay_id": replay_id})
    # set_data (not the deprecated set_measurement): these land as custom span
    # attributes on the transaction segment, which is what the Traces/Explore
    # spans dataset actually searches. Query a numeric one with the typed-tag
    # syntax, e.g. tags[max_speed,number]:>40 — the ,number suffix is required
    # (a bare key resolves as a string and won't match). set_measurement instead
    # writes measurements.*, which the spans dataset rejects as an unknown key.
    for k, (v, unit) in plan["measurements"].items():
        tx.set_data(k, v)
    for s in plan["spans"]:
        span = tx.start_child(op=s["op"], description=s["description"],
                              start_timestamp=s["start"])
        for dk, dv in s["data"].items():
            span.set_data(dk, dv)
        span.finish(end_timestamp=s["end"])
    tx.finish(end_timestamp=plan["end"])

    error_ids: list[str] = []
    for i in plan["issues"]:
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = i["fingerprint"]
            for tk, tv in i["tags"].items():
                scope.set_tag(tk, tv)
            event: dict = {
                "message": i["message"], "level": i["level"],
                "timestamp": i["timestamp"],
            }
            ctx: dict = {}
            if trace_id:
                # hang the issue under the ride's trace, at its moment in time
                ctx["trace"] = {
                    "trace_id": trace_id, "span_id": tx.span_id, "op": i["type"]}
            if replay_id:
                ctx["replay"] = {"replay_id": replay_id}
            if ctx:
                event["contexts"] = ctx
            eid = sentry_sdk.capture_event(event)
            if eid:
                error_ids.append(eid.replace("-", ""))
    return error_ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=FILES)
    ap.add_argument("--send", action="store_true", help="actually send (needs SENTRY_DSN)")
    ap.add_argument("--shift", action="store_true",
                    help="restage activities to end ~1 day apart near now (beats 30d retention)")
    args = ap.parse_args()
    paths = args.paths or FILES

    dsn = os.environ.get("SENTRY_DSN")
    sending = args.send and bool(dsn)
    if args.send and not dsn:
        print("!! --send given but SENTRY_DSN not set — falling back to dry run\n")

    if sending:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, traces_sample_rate=1.0, default_integrations=False)

    now = datetime.now(timezone.utc)
    for idx, path in enumerate(paths):
        # restage each activity to end (idx+1) days ago, newest first
        end_at = (now - timedelta(days=idx + 1)) if args.shift else None
        plan = build_plan(path, end_at=end_at)
        if plan is None:
            print(f"{path}: too few samples, skipped")
            continue
        print_plan(plan)
        if sending:
            send_plan(plan)
            print(f"   ✓ sent to Sentry")

    if sending:
        import sentry_sdk
        sentry_sdk.flush()
        print("\nflushed. check your Sentry project → Traces and Issues.")
    else:
        print("\n(dry run — set SENTRY_DSN and pass --send to actually ingest)")


if __name__ == "__main__":
    main()
