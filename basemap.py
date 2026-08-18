"""Fetch a real map background for a GPS track — sport-agnostic, network-cached.

Stitches CartoDB *dark* basemap tiles under the route and returns a projector
that maps lat/lng → pixel in that image, using Web Mercator (the projection
slippy-map tiles actually use — so the route lines up with the streets).

Tiles are fetched once per bbox and cached on disk (.tiles/), then reused for
every frame of a replay. Falls back to a flat dark canvas if offline.

Attribution: © OpenStreetMap contributors © CARTO.
"""

from __future__ import annotations

import io
import math
import os
import urllib.request

from PIL import Image

TILE = 256
# dark_all = Carto's dark basemap; pairs well with the neon HR trail + dark HUD
TILE_URL = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
CACHE = os.path.join(os.path.dirname(__file__), ".tiles")
UA = "sentry-for-humans/0.1 (hackweek human-telemetry replay)"
VOID = (14, 15, 22)


def _world(lat: float, lng: float, z: int) -> tuple[float, float]:
    """lat/lng → global pixel coords at zoom z (Web Mercator)."""
    n = 2 ** z
    x = (lng + 180.0) / 360.0 * n
    latr = math.radians(lat)
    y = (1 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2 * n
    return x * TILE, y * TILE


def _fetch_tile(z: int, x: int, y: int) -> Image.Image | None:
    path = os.path.join(CACHE, f"{z}_{x}_{y}.png")
    if os.path.exists(path):
        return Image.open(path).convert("RGB")
    url = TILE_URL.format(z=z, x=x, y=y)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        return None
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return Image.open(io.BytesIO(data)).convert("RGB")


def build_basemap(samples, out_w: int, out_h: int, pad: float = 0.12):
    """Return (base_image out_w×out_h, proj(lat,lng)->(x,y) in that image, zoom)."""
    lats = [s.lat for s in samples if s.lat is not None]
    lngs = [s.lng for s in samples if s.lng is not None]
    minlat, maxlat, minlng, maxlng = min(lats), max(lats), min(lngs), max(lngs)

    # largest zoom at which the route bbox still fits inside the map rect
    zoom = 3
    for z in range(19, 2, -1):
        x0, y0 = _world(maxlat, minlng, z)   # north-west corner (max lat = top)
        x1, y1 = _world(minlat, maxlng, z)   # south-east corner
        if (x1 - x0) <= out_w * (1 - 2 * pad) and (y1 - y0) <= out_h * (1 - 2 * pad):
            zoom = z
            break

    x0, y0 = _world(maxlat, minlng, zoom)
    x1, y1 = _world(minlat, maxlng, zoom)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left, top = cx - out_w / 2, cy - out_h / 2   # crop window (global px)

    tx0, tx1 = int(left // TILE), int((left + out_w) // TILE)
    ty0, ty1 = int(top // TILE), int((top + out_h) // TILE)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), VOID)
    got = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = _fetch_tile(zoom, tx, ty)
            if t is not None:
                canvas.paste(t, ((tx - tx0) * TILE, (ty - ty0) * TILE))
                got += 1

    ox, oy = left - tx0 * TILE, top - ty0 * TILE
    base = canvas.crop((int(ox), int(oy), int(ox) + out_w, int(oy) + out_h))
    if got == 0:
        base = Image.new("RGB", (out_w, out_h), VOID)   # offline fallback

    def proj(lat: float, lng: float) -> tuple[float, float]:
        wx, wy = _world(lat, lng, zoom)
        return (wx - left, wy - top)

    return base, proj, zoom
