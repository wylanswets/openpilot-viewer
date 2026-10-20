"""Asking for a drive to be deleted.

The viewer does not delete drives itself. It marks a drive for deletion by creating an
empty file named after the route in <metadata>/delete_requests/ and hides the drive
straight away. The sync container (sync/delete_lib.sh) deletes the drive at the start of
its next run, records it so it is not downloaded from the device again, and removes the
marker. Starred drives are never deleted, by either side.
"""
import re
from pathlib import Path

# Keep in step with ROUTE_ID_RE in sync/delete_lib.sh: the id ends up in paths and globs.
ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9]+--[A-Za-z0-9]+$")


def is_valid_route_id(route_id):
    return bool(ROUTE_ID_RE.match(route_id))


def requests_dir(metadata_dir):
    return Path(metadata_dir) / "delete_requests"


def pending(metadata_dir):
    """Route ids that are marked for deletion and not yet deleted by the sync."""
    try:
        return {
            p.name for p in requests_dir(metadata_dir).iterdir()
            if p.is_file() and is_valid_route_id(p.name)
        }
    except OSError:
        return set()


def request_delete(route_id, metadata_dir):
    directory = requests_dir(metadata_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / route_id).touch()


def cancel(route_id, metadata_dir):
    """Withdraw a request. False if there was none (already deleted, or never asked)."""
    try:
        (requests_dir(metadata_dir) / route_id).unlink()
    except FileNotFoundError:
        return False
    return True
