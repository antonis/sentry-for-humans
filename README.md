# Sentry for Humans

> AI writes the code now, so Sentry pivots to monitoring the flaky dependency that's
> left — the human.

A hackweek experiment that ingests fitness‑workout telemetry (FIT files from Garmin
or Strava) into Sentry's backend and treats your body like a monitored application.
A workout becomes a **trace**, its segments become **spans**, your heart rate and
speed become **measurements**, and the moments you blow past a physiological limit
become **issues**. There's even a synthesized mobile **Session Replay** that flies
through the route on a map while HR and pace tick along the bottom.

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

<img width="1240" height="720" alt="replay" src="https://github.com/user-attachments/assets/f81d8231-4fa1-4e0b-af15-97f2de2355b4" />
<img width="1228" height="682" alt="errors" src="https://github.com/user-attachments/assets/8c1d9374-d877-49ad-9571-5b7a33e56478" />


## Notes

This is a hackweek toy, not a product. The "issues" are physiological heuristics, not
medical advice. Map data © OpenStreetMap contributors © CARTO.
