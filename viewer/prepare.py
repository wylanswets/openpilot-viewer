"""What the background thread does for each drive: work out its duration and make its map image."""
import time
from pathlib import Path

from . import diagnostics, summary, thumbnails


def prepare_route(route_id, raw_dir, metadata_dir, stitched_dir=None, now=None, **kwargs):
    """Warm the drive's summary (so its duration is known) and diagnostics, and make its map image.

    Nothing is done while the drive is still arriving. The summary comes first and needs no
    internet, so a failing tile download does not hold it back. Returns the map image result.
    """
    now = time.time() if now is None else now
    if thumbnails.still_arriving(raw_dir, stitched_dir, route_id, now):
        return "recording"
    try:
        summary.route_summary(route_id, raw_dir, Path(metadata_dir) / "summaries")
    except Exception:
        pass  # the duration is a nicety; the map image is still worth making
    try:
        diagnostics.route_diagnostics(route_id, raw_dir, Path(metadata_dir) / "diagnostics")
    except Exception:
        pass  # the diagnostics panel computes them on demand if this fails
    return thumbnails.make_thumbnail(
        route_id, raw_dir, metadata_dir, stitched_dir=stitched_dir, now=now, **kwargs
    )
