"""Extract a route's GPS track from its qlogs, cached per segment as JSON."""
import re
from pathlib import Path

from .qlogs import cached_segments, read_events, schema as _schema

VALID_ROUTE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
GPS_VALID_FLAG = 1  # bit 0 of gpsLocationExternal.flags: the receiver has a fix


def parse_segment(qlog_path):
    """Return [[lat, lon, speed_m_s], ...] for the fixes in one qlog.zst."""
    points = []
    for event in read_events(qlog_path):
        if event.which() != "gpsLocationExternal":
            continue
        gps = event.gpsLocationExternal
        if not gps.flags & GPS_VALID_FLAG or (gps.latitude == 0 and gps.longitude == 0):
            continue
        points.append([round(gps.latitude, 6), round(gps.longitude, 6), round(gps.speed, 1)])
    return points


def route_track(route_id, raw_dir, cache_dir):
    """Return {"lines": [[[lat, lon], ...], ...], "points": N} for a route.

    A line is broken wherever segments are missing, so gaps are not drawn as
    straight lines.
    """
    segments = cached_segments(
        route_id, raw_dir, Path(cache_dir) / f"{route_id}.json", parse_segment, version=2
    )
    lines, previous = [], None
    for number in sorted(segments):
        points = [[lat, lon] for lat, lon, _ in segments[number]]
        if not points:
            previous = None
            continue
        if previous is not None and number == previous + 1 and lines:
            lines[-1].extend(points)
        else:
            lines.append(points)
        previous = number
    return {"lines": lines, "points": sum(len(line) for line in lines)}
