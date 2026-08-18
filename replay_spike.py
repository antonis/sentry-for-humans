"""Send a mobile-format Session Replay of a whole ride to Sentry, linked to its
trace and issues.

The replay is a **whole-ride flythrough**: the entire activity is time-compressed
into ~48s of video so the position dot actually traverses the route (an 8s window
on a 4h ride moves sub-pixel — invisible). One `trace_id` and one `replay_id` tie
the ride's trace, its issues, and this replay together.

Linking gotcha (hard-won): the replay Trace tab does NOT read the replay's
`trace_ids`. It searches the SPANS dataset for `replay.id:<replay_id>` within the
replay's time window. So the transaction must carry `contexts.replay.replay_id`
(set in send_plan) and the replay window must start at the ride start.

Envelope gotcha: a mobile/"native" video replay is ONE `replay_video` item whose
payload is a msgpack map {replay_event, replay_recording, replay_video} — three
separate items are dropped by relay (InvalidItemCount) after a 200.

Usage:
    SENTRY_DSN=https://...  python replay_spike.py [path.fit]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
import zlib
from datetime import datetime, timedelta, timezone

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from activity import FILES, load
from basemap import build_basemap

W, H = 384, 832          # even/×16 dims; portrait "phone screen"
FPS = 15
VIDEO_S = 48.0           # whole ride compressed into this many seconds of video
MAP = (12, 12, W - 12, 560)   # map draw rect (l,t,r,b); HUD below

DSN = os.environ.get("SENTRY_DSN")
DEFAULT = FILES[0] if FILES else None       # first .fit in ./data


def _font(sz):
    try:
        return ImageFont.load_default(size=sz)
    except TypeError:
        return ImageFont.load_default()


def interp(samples, t):
    """Linear-interpolate a sample-like dict at datetime t."""
    if t <= samples[0].t:
        s = samples[0]
    elif t >= samples[-1].t:
        s = samples[-1]
    else:
        lo, hi = 0, len(samples) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if samples[mid].t <= t:
                lo = mid
            else:
                hi = mid
        a, b = samples[lo], samples[hi]
        span = (b.t - a.t).total_seconds() or 1
        f = (t - a.t).total_seconds() / span

        def L(x, y):
            return None if x is None or y is None else x + (y - x) * f
        return dict(lat=L(a.lat, b.lat), lng=L(a.lng, b.lng), hr=L(a.hr, b.hr),
                    speed=L(a.speed, b.speed), alt=L(a.alt, b.alt), dist=L(a.dist, b.dist))
    return dict(lat=s.lat, lng=s.lng, hr=s.hr, speed=s.speed, alt=s.alt, dist=s.dist)


def hr_color(hr):
    if hr is None:
        return (120, 120, 130)
    # blue(easy) -> green -> orange -> red(hot), rough zones
    z = max(0.0, min(1.0, (hr - 110) / (180 - 110)))
    if z < 0.5:
        f = z / 0.5
        return (int(60 + 40 * f), int(160 + 60 * f), int(230 - 130 * f))
    f = (z - 0.5) / 0.5
    return (int(230 + 25 * f), int(200 - 160 * f), int(90 - 70 * f))


def static_bg(samples, base, proj):
    """Bake the parts that never change: map, faint full route, attribution."""
    img = Image.new("RGB", (W, H), (14, 15, 22))
    img.paste(base, (MAP[0], MAP[1]))
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle(MAP, fill=(10, 11, 18, 70))       # dim the map so the trail pops
    d.line([proj(s.lat, s.lng) for s in samples], fill=(150, 155, 175, 150), width=2)
    d.text((MAP[0] + 4, MAP[1] + 2), "© OSM © CARTO", font=_font(12), fill=(130, 135, 150))
    return img.convert("RGBA")


def draw_hud(d, cur, t):
    d.rectangle([0, 588, W, H], fill=(20, 22, 32, 255))
    d.text((16, 600), "SENTRY · human telemetry", font=_font(18), fill=(150, 130, 210))
    hr = cur["hr"] or 0
    kmh = (cur["speed"] or 0) * 3.6
    alt = cur["alt"] or 0
    dist = (cur["dist"] or 0) / 1000
    d.text((16, 636), f"{hr:.0f}", font=_font(64), fill=hr_color(cur["hr"]))
    d.text((150, 664), "BPM", font=_font(20), fill=(140, 140, 155))
    d.text((16, 726), f"{kmh:4.1f} km/h", font=_font(26), fill=(225, 225, 235))
    d.text((210, 726), f"{dist:4.1f} km", font=_font(26), fill=(225, 225, 235))
    d.text((16, 768), f"elev {alt:.0f} m", font=_font(22), fill=(170, 175, 190))
    d.text((210, 768), f"{t.astimezone(timezone.utc):%H:%M:%S}Z", font=_font(22), fill=(170, 175, 190))


def build_frames(samples, base, proj):
    """Fly through the whole ride: compress ride_start→ride_end into VIDEO_S.

    The colored trail is drawn once onto an accumulating overlay (each GPS
    segment exactly once across all frames), so cost stays ~O(frames), not
    O(frames × samples)."""
    bg = static_bg(samples, base, proj)
    pts = [proj(s.lat, s.lng) for s in samples]
    trail = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(trail, "RGBA")

    ride_start, ride_end = samples[0].t, samples[-1].t
    ride_dur = (ride_end - ride_start).total_seconds() or 1
    n_frames = int(VIDEO_S * FPS)

    frames = []
    drawn = 1                                     # samples already laid into `trail`
    for k in range(n_frames):
        frac = k / (n_frames - 1) if n_frames > 1 else 1.0
        t = ride_start + timedelta(seconds=frac * ride_dur)
        # extend the trail up to the current time
        while drawn < len(samples) and samples[drawn].t <= t:
            td.line([pts[drawn - 1], pts[drawn]], fill=hr_color(samples[drawn].hr) + (255,),
                    width=3)
            drawn += 1

        frame = Image.alpha_composite(bg, trail)
        d = ImageDraw.Draw(frame, "RGBA")
        cur = interp(samples, t)
        x, y = proj(cur["lat"], cur["lng"])
        c = hr_color(cur["hr"])
        d.ellipse([x - 11, y - 11, x + 11, y + 11], fill=c + (90,))
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(255, 255, 255), outline=c, width=2)
        draw_hud(d, cur, t)
        frames.append(np.asarray(frame.convert("RGB")))
    return frames


def compress_plan(plan, seconds, end_at=None):
    """Rescale a plan's timeline into the `seconds` window ending at `end_at`.

    The replay's Trace tab only searches spans inside [started_at-1s,
    finished_at+1s]. Two problems to solve at once: (1) the ride is hours long
    but the replay video is ~48s, and (2) the ride happened days ago while the
    replay's started_at is clamped toward ingestion time — so a historical trace
    never overlaps the replay window. We fix both by squeezing the companion
    trace + issues into a fresh `seconds`-long window ending now, matching the
    replay window exactly. Span `duration_s` data and measurements keep their
    real values; only the waterfall's wall-clock positions move."""
    t0 = plan["start"]
    dur = (plan["end"] - t0).total_seconds() or 1
    scale = seconds / dur
    end_at = end_at or datetime.now(timezone.utc)
    base = end_at - timedelta(seconds=seconds)

    def rt(t):
        return base + timedelta(seconds=(t - t0).total_seconds() * scale)

    p = dict(plan)
    p["start"], p["end"] = rt(plan["start"]), rt(plan["end"])
    p["spans"] = [{**s, "start": rt(s["start"]), "end": rt(s["end"])} for s in plan["spans"]]
    p["issues"] = [{**i, "timestamp": rt(i["timestamp"])} for i in plan["issues"]]
    return p


def encode_mp4(frames) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        path = f.name
    imageio.mimwrite(path, frames, fps=FPS, codec="libx264", macro_block_size=16,
                     ffmpeg_params=["-pix_fmt", "yuv420p"])
    with open(path, "rb") as fh:
        data = fh.read()
    os.unlink(path)
    return data


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not path:
        print("no .fit file given and none found in ./data — "
              "pass a path or drop an export into data/")
        return
    sport, samples = load(path)
    samples = [s for s in samples if s.lat is not None]
    if len(samples) < 5:
        print("no GPS samples")
        return

    base, mproj, zoom = build_basemap(samples, MAP[2] - MAP[0], MAP[3] - MAP[1])
    print(f"basemap zoom {zoom}")

    def proj(lat, lng):                           # base-image coords → frame coords
        x, y = mproj(lat, lng)
        return (MAP[0] + x, MAP[1] + y)

    frames = build_frames(samples, base, proj)
    peak_hr = max((s.hr for s in samples if s.hr is not None), default=0)
    print(f"rendered {len(frames)} frames  ({W}x{H} @ {FPS}fps)  whole-ride flythrough"
          f"  peak HR {peak_hr:.0f}")

    mp4 = encode_mp4(frames)
    dur_ms = int(len(frames) / FPS * 1000)
    print(f"mp4: {len(mp4)} bytes, {dur_ms} ms")

    if not DSN:
        Image.fromarray(frames[len(frames) // 2]).save("spike_frame.png")
        with open("spike_segment.mp4", "wb") as fh:
            fh.write(mp4)
        print("no SENTRY_DSN — wrote spike_frame.png + spike_segment.mp4 (dry run)")
        return

    import sentry_sdk
    from sentry_sdk.envelope import Envelope, Item, PayloadRef
    from sentry_ingest import build_plan, send_plan

    sentry_sdk.init(dsn=DSN, default_integrations=False, traces_sample_rate=1.0,
                    debug=bool(os.environ.get("SENTRY_DEBUG")))
    client = sentry_sdk.get_client()

    # One replay_id + one trace_id tie the replay, the ride's trace, and its
    # issues together. The replay_id must exist BEFORE we send the trace, because
    # the transaction has to carry it (contexts.replay.replay_id) for the Trace
    # tab's `replay.id:<id>` span search to find it.
    replay_id = uuid.uuid4().hex
    trace_id = uuid.uuid4().hex
    error_ids: list[str] = []
    plan = None
    try:
        plan = build_plan(path)                   # real timestamps (ride is <30d old)
        plan = compress_plan(plan, VIDEO_S)       # squeeze into the replay window
        error_ids = send_plan(plan, trace_id=trace_id, replay_id=replay_id)
        print(f"sent trace {trace_id}  ({len(plan['spans'])} spans, {len(error_ids)} issues)")
    except Exception as e:
        print(f"trace send failed ({e}); replay will still send, unlinked")
        trace_id = None

    # The replay window MUST match the compressed trace window exactly, so every
    # span/issue falls inside [started_at-1s, finished_at+1s] and links in the
    # Trace tab. Anchor to the plan (build_plan may start earlier than the GPS
    # samples if HR logging preceded GPS lock); fall back to GPS start if no plan.
    # The replay's stored started_at gets pulled later than the replay_start_timestamp
    # we send (ingest-side normalization), so an exact 48s window misses the earlier
    # spans and the Trace tab (which only searches [started_at-1s, finished_at+1s])
    # comes up empty. Declaring the window to start earlier than the trace closes
    # that gap — proven at 600s. Override with REPLAY_WINDOW_PAD.
    pad = float(os.environ.get("REPLAY_WINDOW_PAD", "600"))
    if plan is not None:
        start_s = plan["start"].timestamp() - pad
        end_s = plan["end"].timestamp()
    else:
        start_s = samples[0].t.timestamp() - pad
        end_s = samples[0].t.timestamp() + VIDEO_S
    start_ms = int(start_s * 1000)

    rrweb = [
        {"type": 4, "data": {"href": "sentry-for-humans://ride", "width": W, "height": H},
         "timestamp": start_ms},
        {"type": 5, "timestamp": start_ms, "data": {"tag": "video", "payload": {
            "segmentId": 0, "size": len(mp4), "duration": dur_ms, "encoding": "h264",
            "container": "mp4", "height": H, "width": W, "frameCount": len(frames),
            "frameRate": FPS, "frameRateType": "constant", "left": 0, "top": 0}}},
    ]
    recording = json.dumps({"segment_id": 0}).encode() + b"\n" + zlib.compress(
        json.dumps(rrweb).encode())

    replay_event = {
        "type": "replay_event", "event_id": replay_id, "replay_id": replay_id,
        "segment_id": 0, "replay_type": "session", "platform": "javascript",
        "timestamp": end_s, "replay_start_timestamp": start_s,
        "urls": [f"{sport} ride"], "error_ids": error_ids,
        "trace_ids": [trace_id] if trace_id else [],
        "sdk": {"name": "sentry.javascript.react-native", "version": "5.0.0"},
        "tags": {"sport": sport, "activity": path.rsplit("/", 1)[-1].replace(".fit", "")},
        "contexts": {
            "os": {"name": "iOS", "version": "17.0"},
            "device": {"name": "Garmin", "family": "wearable", "model": "Edge"},
            "replay": {"error_sample_rate": 0, "session_sample_rate": 1.0},
        },
        "user": {"id": os.environ.get("ATHLETE_ID", "athlete-1")},
    }

    # Mobile ("native") video replay: relay expects ONE `replay_video` item whose
    # payload is a msgpack map {replay_event, replay_recording, replay_video} — NOT
    # three separate items (that path returns InvalidItemCount and is dropped after
    # a 200). Ref: getsentry/relay processing/replays/{process,mod,forward}.rs.
    import msgpack

    video_event = msgpack.packb({
        "replay_event": json.dumps(replay_event).encode(),
        "replay_recording": recording,
        "replay_video": mp4,
    }, use_bin_type=True)

    env = Envelope(headers={"event_id": replay_id})
    env.add_item(Item(type="replay_video", payload=PayloadRef(bytes=video_event),
                      content_type="application/octet-stream"))

    client.transport.capture_envelope(env)
    client.flush()
    print(f"\n✓ sent replay {replay_id}  (whole ride, {dur_ms}ms)")
    print(f"  linked trace {trace_id} + {len(error_ids)} issues via replay.id")
    print("check your Sentry project → Replays  (may take a minute)")


if __name__ == "__main__":
    main()
