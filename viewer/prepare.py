"""What the background thread does for each drive: work out its duration and make its map image."""
import os
import sys
import threading
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


# ----------------------
# The always-running worker
# ----------------------

PASS_INTERVAL_S = 5 * 60
FIRST_PASS_DELAY_S = 30


def run_pass(raw_dir, metadata_dir, stitched_dir, now=None):
    """Prepare every drive that needs it (newest first), one at a time; returns {route: result}.

    A drive needs it when its map image is missing or out of date, its duration is not known yet, or its
    diagnostics are not computed. Does nothing while another run is going (a page view starts one too) or
    after a failed tile download (see thumbnails.BACKOFF_S).
    """
    results = {}
    if time.monotonic() < thumbnails._backoff_until or not thumbnails._run_lock.acquire(blocking=False):
        return results
    try:
        thumbs_dir = Path(metadata_dir) / "thumbs"
        for route_id in thumbnails.route_ids(raw_dir):
            segments = len(list(Path(raw_dir).glob(f"{route_id}--*")))
            wanted = (
                thumbnails.needs_work(thumbs_dir, route_id, segments)
                or summary.cached_duration(route_id, raw_dir, Path(metadata_dir) / "summaries") is None
                or not (Path(metadata_dir) / "diagnostics" / f"{route_id}.json").exists()
            )
            if not wanted:
                continue
            try:
                result = prepare_route(route_id, raw_dir, metadata_dir, stitched_dir=stitched_dir, now=now)
            except Exception as exc:
                print(f"prepare: {route_id} failed: {exc!r}", file=sys.stderr)
                result = "failed"
            results[route_id] = result
            if result == "failed":
                thumbnails._backoff_until = time.monotonic() + thumbnails.BACKOFF_S
                break
    finally:
        thumbnails._run_lock.release()
    return results


def should_start_worker(environ, argv):
    """The worker runs under gunicorn (the real server) or when MAP_WORKER=1; MAP_WORKER=0 turns it off."""
    setting = str(environ.get("MAP_WORKER", "auto")).strip().lower()
    if setting in ("0", "false", "off", "no"):
        return False
    if setting in ("1", "true", "on", "yes"):
        return True
    return bool(argv) and "gunicorn" in os.path.basename(argv[0])


_worker = None


def start_worker(run_once, interval=PASS_INTERVAL_S, first_delay=FIRST_PASS_DELAY_S, sleep=time.sleep):
    """Call run_once() every `interval` seconds in a background thread (only one per process)."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker

    def loop():
        sleep(first_delay)
        while True:
            try:
                run_once()
            except Exception as exc:  # keep going: the next pass may work
                print(f"prepare: pass failed: {exc!r}", file=sys.stderr)
            sleep(interval)

    _worker = threading.Thread(target=loop, name="prepare-drives", daemon=True)
    _worker.start()
    return _worker
