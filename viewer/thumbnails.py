"""Route thumbnails: the route drawn over OpenStreetMap tiles, rendered once to a PNG.

Tiles are fetched politely (identifying User-Agent, cached on disk for a week, a pause
between requests) and the image carries the OpenStreetMap credit, as the tile usage
policy asks. A route is only rendered when every tile it needs is available.
"""
import json
import math
import os
import threading
import time
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import tracks
from .qlogs import _found_segments

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "openpilot-viewer/1.0 (+https://github.com/wylanswets/openpilot-viewer)"
ATTRIBUTION = "© OpenStreetMap contributors"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

TILE = 256
WIDTH, HEIGHT = 640, 260
PADDING = 32  # pixels kept free around the route
MIN_ZOOM, MAX_ZOOM = 3, 17
TILE_MAX_AGE_S = 7 * 24 * 3600
MAX_TILE_BYTES = 300_000
REQUEST_INTERVAL_S = 0.5
RECORDING_AGE_S = 10 * 60  # without a .done marker, a newer qlog means the route is still arriving
BACKOFF_S = 10 * 60  # after a failed attempt, wait before trying again

LINE_OUTLINE, LINE_COLOR = (255, 255, 255, 255), (37, 99, 235, 255)
START_COLOR, END_COLOR = (34, 197, 94, 255), (239, 68, 68, 255)
BLANK = (229, 227, 223)


# ----------------------
# Geometry
# ----------------------

def world_xy(lat, lon, zoom):
    """Pixel position of a coordinate in the whole-world map at a zoom level (Web Mercator)."""
    scale = TILE * 2 ** zoom
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * scale
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * scale
    return x, y


def layout(points):
    """Pick the zoom and the top-left corner (world pixels) so the route fits the image."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    box = (min(lats), min(lons), max(lats), max(lons))
    zoom = MIN_ZOOM
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        x0, y1 = world_xy(box[0], box[1], z)
        x1, y0 = world_xy(box[2], box[3], z)
        if x1 - x0 <= WIDTH - 2 * PADDING and y1 - y0 <= HEIGHT - 2 * PADDING:
            zoom = z
            break
    x0, y1 = world_xy(box[0], box[1], zoom)
    x1, y0 = world_xy(box[2], box[3], zoom)
    return zoom, (x0 + x1) / 2 - WIDTH / 2, (y0 + y1) / 2 - HEIGHT / 2


def to_pixel(lat, lon, zoom, left, top):
    x, y = world_xy(lat, lon, zoom)
    return x - left, y - top


# ----------------------
# Tiles
# ----------------------

class _Throttle:
    """Keep at least REQUEST_INTERVAL_S between requests to the tile server."""

    def __init__(self):
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            pause = self.last + REQUEST_INTERVAL_S - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self.last = time.monotonic()


_throttle = _Throttle()


def fetch_tile_from_osm(z, x, y):
    _throttle.wait()
    request = urllib.request.Request(
        TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(MAX_TILE_BYTES + 1)


def get_tile(z, x, y, cache_dir, fetch=fetch_tile_from_osm, now=None):
    """PNG bytes of a tile from the disk cache or the tile server; None if unavailable.

    A cached tile is used for a week. If a refetch fails, an older cached copy is still
    used. Replies that are not a PNG are never cached.
    """
    now = time.time() if now is None else now
    path = Path(cache_dir) / str(z) / str(x) / f"{y}.png"
    cached = None
    try:
        age = now - path.stat().st_mtime
        cached = path.read_bytes()
        if age < TILE_MAX_AGE_S:
            return cached
    except OSError:
        pass
    try:
        body = fetch(z, x, y)
        if not body.startswith(PNG_MAGIC) or len(body) > MAX_TILE_BYTES:
            raise ValueError("not a tile")
    except Exception:
        return cached
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)
    except OSError:
        pass  # the cache is an optimisation
    return body


# ----------------------
# Rendering
# ----------------------

def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def render(lines, tile_source):
    """Draw the route over map tiles. Returns a PIL image, or None if a tile is missing.

    lines: [[[lat, lon], ...], ...]; tile_source(z, x, y) -> PNG bytes or None.
    """
    points = [p for line in lines for p in line]
    if not points:
        return None
    zoom, left, top = layout(points)
    count = 2 ** zoom

    base = Image.new("RGB", (WIDTH, HEIGHT), BLANK)
    for ty in range(math.floor(top / TILE), math.floor((top + HEIGHT - 1) / TILE) + 1):
        if not 0 <= ty < count:
            continue  # beyond the poles
        for tx in range(math.floor(left / TILE), math.floor((left + WIDTH - 1) / TILE) + 1):
            data = tile_source(zoom, tx % count, ty)
            if data is None:
                return None
            tile = Image.open(BytesIO(data)).convert("RGB")
            base.paste(tile, (round(tx * TILE - left), round(ty * TILE - top)))

    # Draw at twice the size and shrink, for smooth lines.
    k = 2
    overlay = Image.new("RGBA", (WIDTH * k, HEIGHT * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def pixel(p):
        x, y = to_pixel(p[0], p[1], zoom, left, top)
        return x * k, y * k

    for line in lines:
        xy = [pixel(p) for p in line]
        if len(xy) > 1:
            draw.line(xy, fill=LINE_OUTLINE, width=9 * k // 2 + 2, joint="curve")
            draw.line(xy, fill=LINE_COLOR, width=5 * k // 2 + 1, joint="curve")

    def dot(p, color):
        x, y = pixel(p)
        r = 8 * k // 2
        draw.ellipse((x - r - k, y - r - k, x + r + k, y + r + k), fill=LINE_OUTLINE)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    dot(lines[0][0], START_COLOR)
    dot(lines[-1][-1], END_COLOR)

    overlay = overlay.resize((WIDTH, HEIGHT), Image.LANCZOS)
    image = base.convert("RGBA")
    image.alpha_composite(overlay)

    credit = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    credit_draw = ImageDraw.Draw(credit)
    font = _font(11)
    box = credit_draw.textbbox((0, 0), ATTRIBUTION, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    credit_draw.rectangle((WIDTH - w - 10, HEIGHT - h - 8, WIDTH, HEIGHT), fill=(255, 255, 255, 190))
    credit_draw.text((WIDTH - w - 5, HEIGHT - h - 5 - box[1]), ATTRIBUTION, fill=(60, 60, 60, 255), font=font)
    image.alpha_composite(credit)
    return image.convert("RGB")


# ----------------------
# Per-route images
# ----------------------

def image_path(thumbs_dir, route_id):
    return Path(thumbs_dir) / f"{route_id}.png"


def _state_path(thumbs_dir, route_id):
    return Path(thumbs_dir) / f"{route_id}.json"


def read_state(thumbs_dir, route_id):
    try:
        with open(_state_path(thumbs_dir, route_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def needs_work(thumbs_dir, route_id, segment_count):
    """Cheap check (no log parsing): has this route not been looked at since it last grew?"""
    state = read_state(thumbs_dir, route_id)
    if not state or state.get("segments") != segment_count or state.get("status") == "failed":
        return True
    # "no GPS" written before the logs were counted may have been judged on a few logs only
    if state.get("status") == "no_gps":
        return "logs" not in state and segment_count > 0  # nothing to judge again in a folder without segments
    return not image_path(thumbs_dir, route_id).exists()  # an image that was made but is gone is made again


def display_state(thumbs_dir, route_id, segment_count):
    """What the drive list shows for a route: "image", "none" (no GPS), "failed" (tiles unavailable) or "coming"."""
    if image_path(thumbs_dir, route_id).exists():
        return "image"
    if not needs_work(thumbs_dir, route_id, segment_count):
        return "none"  # only a "no GPS" answer gets here: every other case either has an image or needs work
    state = read_state(thumbs_dir, route_id)
    return "failed" if state and state.get("status") == "failed" and state.get("segments") == segment_count else "coming"


def route_ids(raw_dir):
    """The routes that have folders in the raw directory, newest first."""
    found = {folder.name.rsplit("--", 1)[0] for folder in Path(raw_dir).glob("*--*") if folder.name.rsplit("--", 1)[-1].isdigit()}
    return sorted(found, reverse=True)


def _newest_qlog_mtime(raw_dir, route_id):
    newest = 0.0
    for folder in Path(raw_dir).glob(f"{route_id}--*"):
        try:
            newest = max(newest, (folder / "qlog.zst").stat().st_mtime)
        except OSError:
            pass
    return newest


def _write_state(thumbs_dir, route_id, state):
    path = _state_path(thumbs_dir, route_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def still_arriving(raw_dir, stitched_dir, route_id, now):
    """True while a route may still be being recorded or synced, so its image would be made too early.

    The sync writes stitched/<route>/.done only after the whole route was copied and stitched (and
    it holds back the newest route while the device is driving), so with the stitched folder known
    that is the only thing that counts. Log file times are not a safe sign: the device can drop off
    the network part way through a copy and leave a route with most of its logs missing, and the sync
    used to rewrite logs on every run. Without a stitched folder to look at (tests, the command line
    without one) the age of the newest log is used instead.
    """
    if stitched_dir is not None:
        return not (Path(stitched_dir) / route_id / ".done").is_file()
    return now - _newest_qlog_mtime(raw_dir, route_id) < RECORDING_AGE_S


def make_thumbnail(route_id, raw_dir, metadata_dir, fetch=fetch_tile_from_osm, now=None, force=False,
                   stitched_dir=None):
    """Make (or refresh) the thumbnail of a route.

    Returns "made", "fresh" (already up to date), "no_gps", "recording" (still being
    recorded or synced, try later) or "failed" (a tile was unavailable, try later).
    """
    now = time.time() if now is None else now
    metadata_dir = Path(metadata_dir)
    thumbs_dir = metadata_dir / "thumbs"
    segments = len(list(Path(raw_dir).glob(f"{route_id}--*")))
    logs = len(_found_segments(route_id, raw_dir))  # folders show up before their logs have arrived

    if still_arriving(raw_dir, stitched_dir, route_id, now):
        return "recording"

    state = read_state(thumbs_dir, route_id)
    if state and state.get("segments") == segments and not force:
        if state["status"] == "no_gps" and state.get("logs") == logs:
            return "fresh"
        if state["status"] != "no_gps" and image_path(thumbs_dir, route_id).exists():
            return "fresh"

    track = tracks.route_track(route_id, raw_dir, metadata_dir / "tracks")
    if not track["points"]:
        _write_state(thumbs_dir, route_id, {"status": "no_gps", "segments": segments, "logs": logs})
        return "no_gps"

    cache_dir = metadata_dir / "tiles"
    image = render(track["lines"], lambda z, x, y: get_tile(z, x, y, cache_dir, fetch, now))
    if image is None:
        _write_state(thumbs_dir, route_id, {"status": "failed", "segments": segments, "logs": logs})
        return "failed"

    thumbs_dir.mkdir(parents=True, exist_ok=True)
    path = image_path(thumbs_dir, route_id)
    tmp = path.with_suffix(".tmp")
    image.save(tmp, format="PNG", optimize=True)
    os.replace(tmp, path)
    _write_state(thumbs_dir, route_id, {"status": "ok", "segments": segments})
    return "made"


# ----------------------
# Background generation
# ----------------------

_run_lock = threading.Lock()
_backoff_until = 0.0


def start_background(route_ids, raw_dir, metadata_dir, make=make_thumbnail, stitched_dir=None):
    """Make thumbnails for these routes in a background thread, one run at a time.

    Returns the thread, or None if there is nothing to do, a run is already going, or
    a recent attempt failed (so a missing internet connection is not retried on every view).
    """
    global _backoff_until
    route_ids = list(route_ids)
    if not route_ids or time.monotonic() < _backoff_until:
        return None
    if not _run_lock.acquire(blocking=False):
        return None

    def run():
        global _backoff_until
        try:
            for route_id in route_ids:
                try:
                    extra = {"stitched_dir": stitched_dir} if stitched_dir is not None else {}
                    result = make(route_id, raw_dir, metadata_dir, **extra)
                except Exception:
                    result = "failed"
                if result == "failed":
                    _backoff_until = time.monotonic() + BACKOFF_S
                    break
        finally:
            _run_lock.release()

    thread = threading.Thread(target=run, name="route-thumbnails", daemon=True)
    thread.start()
    return thread
