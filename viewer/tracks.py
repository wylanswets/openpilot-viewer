"""Extract a route's GPS track from its qlogs, cached per segment as JSON."""
import json
import os
import re
from pathlib import Path

import capnp
import zstandard as zstd

CEREAL_DIR = Path(__file__).resolve().parent / "cereal"
VALID_ROUTE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
GPS_VALID_FLAG = 1  # bit 0 of gpsLocationExternal.flags: the receiver has a fix

_log_capnp = None


def _schema():
    global _log_capnp
    if _log_capnp is None:
        _log_capnp = capnp.load(str(CEREAL_DIR / "log.capnp"), imports=[str(CEREAL_DIR)])
    return _log_capnp


def parse_segment(qlog_path):
    """Return [[lat, lon, speed_m_s], ...] for the fixes in one qlog.zst."""
    with open(qlog_path, "rb") as f:
        data = zstd.ZstdDecompressor().stream_reader(f).read()
    points = []
    for event in _schema().Event.read_multiple_bytes(data):
        if event.which() != "gpsLocationExternal":
            continue
        gps = event.gpsLocationExternal
        if not gps.flags & GPS_VALID_FLAG or (gps.latitude == 0 and gps.longitude == 0):
            continue
        points.append([round(gps.latitude, 6), round(gps.longitude, 6), round(gps.speed, 1)])
    return points


def _load_cache(cache_file):
    try:
        with open(cache_file) as f:
            return json.load(f).get("segments", {})
    except (OSError, ValueError):
        return {}


def _save_cache(cache_file, segments):
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"segments": segments}, f)
        os.replace(tmp, cache_file)
    except OSError:
        pass  # the cache is an optimisation; a read-only metadata dir is fine


def route_track(route_id, raw_dir, cache_dir):
    """Return {"lines": [[[lat, lon], ...], ...], "points": N} for a route.

    Each segment is parsed once and cached with its qlog size and mtime, so a
    route that is still syncing only parses the new segments. A line is broken
    wherever segments are missing, so gaps are not drawn as straight lines.
    """
    cache_file = Path(cache_dir) / f"{route_id}.json"
    cached = _load_cache(cache_file)
    segments = {}
    changed = False

    found = []
    for folder in Path(raw_dir).glob(f"{route_id}--*"):
        suffix = folder.name.rsplit("--", 1)[-1]
        qlog = folder / "qlog.zst"
        if suffix.isdigit() and qlog.is_file():
            found.append((int(suffix), qlog))

    for number, qlog in sorted(found):
        st = qlog.stat()
        entry = cached.get(str(number))
        if not entry or entry.get("size") != st.st_size or entry.get("mtime") != int(st.st_mtime):
            try:
                entry = {
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "points": parse_segment(qlog),
                }
            except Exception:
                continue  # unreadable or half-synced qlog: skip, do not cache
            changed = True
        segments[str(number)] = entry

    if changed or set(segments) != set(cached):
        _save_cache(cache_file, segments)

    lines, previous = [], None
    for number in sorted(int(n) for n in segments):
        points = [[lat, lon] for lat, lon, _ in segments[str(number)]["points"]]
        if not points:
            previous = None
            continue
        if previous is not None and number == previous + 1 and lines:
            lines[-1].extend(points)
        else:
            lines.append(points)
        previous = number
    return {"lines": lines, "points": sum(len(line) for line in lines)}
