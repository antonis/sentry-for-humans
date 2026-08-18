# Sentry for Humans

> AI writes the code now, so Sentry pivots to monitoring the flaky dependency that's
> left — the human.

A hackweek experiment that ingests fitness‑workout telemetry (FIT files from Garmin
or Strava) into Sentry's backend and treats your body like a monitored application.
A workout becomes a **trace**, its segments become **spans**, your heart rate and
speed become **measurements**, and the moments you blow past a physiological limit
become **issues**. There's even a synthesized mobile **Session Replay** that flies
through the route on a map while HR and pace tick along the bottom.

It is **sport‑agnostic from the first line.** There are no `if sport == "cycling"`
branches anywhere. Every threshold is derived from the activity's *own* data
distribution — redline is a heart‑rate percentile, a surge is a speed percentile,
terrain is measured by grade. So a road ride, an open‑water swim, and a mountain
hike all run through the exact same code path and each produces sport‑appropriate
results.

## The mapping

| Human telemetry | Sentry primitive |
| --- | --- |
| A whole workout | Trace / transaction |
| A stretch of climb / descent / flat / stop | Span |
| Heart rate, speed, altitude, distance | Measurements + span data |
| HR redline, a long stop, a speed surge | Issue (grouped by fingerprint) |
| The GPS track, rendered on a map | Session Replay (mobile video) |

One `trace_id` and one `replay_id` tie a ride's trace, its issues, and its replay
together, so you can jump from a redline issue → the trace → watch the replay of the
exact moment.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Drop one or more `.fit` exports into `./data/` (Garmin Connect or Strava →
"Export Original"). They are gitignored — a workout file is a GPS trace of where you
live, so it never belongs in a repo.

You'll also need a Sentry project (any Python/JavaScript project works) and its DSN.
The DSN is only ever read from the environment; it is never written to a file.

## Usage

Everything runs **dry (no network) by default** — it prints exactly what it *would*
send. Add a DSN to actually ingest.

```bash
# 1. Inspect a file: what signals are present, sample rate, gaps, ranges
python inventory.py data/your_ride.fit

# 2. See the trace + spans + issues a workout produces, as a span tree
python activity.py                 # all files in ./data
python activity.py data/your_ride.fit

# 3. Send traces + issues to Sentry
SENTRY_DSN="https://…@…ingest.sentry.io/…" python sentry_ingest.py --send

#    Sentry drops events older than ~30 days; --shift restages each activity to end
#    a day apart near now (its internal timeline preserved) so older files still land.
SENTRY_DSN="https://…" python sentry_ingest.py --send --shift

# 4. Send a whole-ride Session Replay, linked to its trace + issues
SENTRY_DSN="https://…" python replay_spike.py data/your_ride.fit
```

## Files

- **`inventory.py`** — read‑only dump of what a FIT file contains. No Sentry, no network.
- **`activity.py`** — the sport‑agnostic core: load → smooth altitude → segment by
  terrain/stops → detect events, all with thresholds derived from the file's own data.
- **`sentry_ingest.py`** — turns one activity into a trace (per‑segment spans +
  measurements) and each detected event into a fingerprinted issue.
- **`basemap.py`** — stitches CartoDB dark map tiles under the route (Web Mercator)
  and returns a lat/lng → pixel projector. Tiles are cached on disk.
- **`replay_spike.py`** — renders a portrait "phone screen" video that flies through
  the whole ride, then sends it as a mobile Session Replay linked to the trace.

## Two hard‑won gotchas (so you don't have to rediscover them)

**Mobile video replays are one envelope item, not three.** A native/mobile video
replay is a *single* `replay_video` envelope item whose payload is a msgpack map
`{replay_event, replay_recording, replay_video}`. Sending three separate items gets
you a `200` followed by a silent drop (`InvalidItemCount`) inside Relay.

**The replay→trace link is event‑driven, not `trace_ids`‑driven.** The replay's
Trace tab doesn't read the replay's `trace_ids` field. It searches the spans dataset
for `replay.id:<id>` inside the replay's time window. So the *transaction* has to
carry `contexts.replay.replay_id` (Relay copies it onto the spans), and the trace has
to overlap the replay's window — which is why `replay_spike.py` compresses the whole
ride into the same short window as the video and pads the window start.

## Notes

This is a hackweek toy, not a product. The "issues" are physiological heuristics, not
medical advice. Map data © OpenStreetMap contributors © CARTO.
