"""Retention: how long drives are kept, by number of drives, by age, or by storage used.

One policy is chosen and applies; "off" (the default) changes nothing. What is removed is decided here
by plan(), a pure function that also drives the preview on the settings page. Removing is done the way
the Delete button does it: a request file in <metadata>/delete_requests/ (see deletion.py), which the sync
container carries out through its one delete routine, recording the drive so it is never downloaded from
the device again. The free-space floor in the sync script stays as the last safety net.

Rules (all policies):
  - starred drives are never removed;
  - only finished drives (the sync wrote their .done marker) are ever removed, so a drive that is still
    arriving is never touched;
  - drives whose start time is unknown are never removed and are not counted for "newest N";
  - drives already marked for removal are left out of the calculation (they are on their way out);
  - for the storage policy, every drive counts toward the total, starred ones too, so a starred drive uses
    part of the budget and the next oldest drive that is not starred goes instead.
"""
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import deletion

POLICIES = ("off", "count", "days", "storage")
GB = 10 ** 9  # decimal gigabytes, as the sizes are shown
CONF_NAME = "retention.conf"
STATUS_NAME = "retention_status.json"
DEFAULTS = {"policy": "off", "keep_drives": 50, "keep_days": 30, "keep_gb": 500}
LIMITS = {"keep_drives": (1, 100_000), "keep_days": (1, 36_500), "keep_gb": (1, 1_000_000)}
CONF_KEYS = {"POLICY": "policy", "KEEP_DRIVES": "keep_drives", "KEEP_DAYS": "keep_days", "KEEP_GB": "keep_gb"}
SEGMENT_DIR_RE = re.compile(r"^(?P<route>[A-Za-z0-9]+--[A-Za-z0-9]+)--(?P<segment>\d+)$")
ENFORCE_INTERVAL_S = 10 * 60
ENFORCE_FIRST_DELAY_S = 2 * 60
HISTORY_LENGTH = 10


# ----------------------
# Settings
# ----------------------

@dataclass(frozen=True)
class Settings:
    policy: str = "off"
    keep_drives: int = DEFAULTS["keep_drives"]
    keep_days: int = DEFAULTS["keep_days"]
    keep_gb: int = DEFAULTS["keep_gb"]

    def describe(self):
        return {
            "off": "Off: nothing is removed automatically",
            "count": f"Keep the newest {self.keep_drives} drives",
            "days": f"Keep drives from the last {self.keep_days} days",
            "storage": f"Use at most {self.keep_gb} GB for drives",
        }[self.policy]


def conf_path(metadata_dir):
    return Path(metadata_dir) / CONF_NAME


def parse_conf(text):
    """KEY=VALUE lines (the file is also read by bash); unknown or malformed lines are ignored."""
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Z_]+)=(\S*)", line.strip())
        if match and match.group(1) in CONF_KEYS:
            values[CONF_KEYS[match.group(1)]] = match.group(2)
    return values


def parse_settings(raw):
    """Settings from strings (a form or the file). Returns (Settings, errors); bad values keep their default."""
    errors = []
    policy = str(raw.get("policy", DEFAULTS["policy"])).strip().lower()
    if policy not in POLICIES:
        errors.append(f"Unknown policy '{policy}'.")
        policy = "off"
    numbers = {}
    for key, (low, high) in LIMITS.items():
        text = str(raw.get(key, "")).strip()
        if text == "":
            numbers[key] = DEFAULTS[key]
            continue
        try:
            value = int(text)
        except ValueError:
            errors.append(f"{key.replace('_', ' ')}: '{text}' is not a whole number.")
            numbers[key] = DEFAULTS[key]
            continue
        if not low <= value <= high:
            errors.append(f"{key.replace('_', ' ')} must be between {low} and {high}.")
            value = min(max(value, low), high)
        numbers[key] = value
    return Settings(policy=policy, **numbers), errors


def load_settings(metadata_dir):
    """The saved settings; off when there is no file or it is unreadable."""
    try:
        text = conf_path(metadata_dir).read_text()
    except OSError:
        return Settings()
    settings, _errors = parse_settings(parse_conf(text))  # bad or out of range values fall back / are clamped
    return settings


def save_settings(metadata_dir, settings):
    path = conf_path(metadata_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# Written by the viewer's Storage page. POLICY is off, count, days or storage;\n"
        "# only the value that belongs to the chosen policy applies.\n"
        f"POLICY={settings.policy}\nKEEP_DRIVES={settings.keep_drives}\n"
        f"KEEP_DAYS={settings.keep_days}\nKEEP_GB={settings.keep_gb}\n"
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ----------------------
# Drives and the plan
# ----------------------

@dataclass(frozen=True)
class Drive:
    route_id: str
    start: datetime | None   # naive, UTC (as the sync writes it); None if unknown
    size: int                # bytes: raw segments plus the stitched folder
    starred: bool = False
    finished: bool = False   # the sync wrote .done: fully copied and stitched

    @property
    def removable(self):
        return not self.starred and self.finished and self.start is not None


@dataclass(frozen=True)
class Plan:
    remove: tuple
    freed: int
    total_before: int
    total_after: int
    limit: int | None
    limit_reached: bool

    @property
    def count(self):
        return len(self.remove)


def plan(drives, settings, now):
    """Which drives the policy would remove, oldest first. Pure: nothing is read or changed."""
    drives = list(drives)
    total = sum(d.size for d in drives)
    chosen, limit = [], None

    def oldest_first(items):
        return sorted(items, key=lambda d: (d.start, d.route_id))

    if settings.policy == "count":
        dated = sorted((d for d in drives if d.start is not None), key=lambda d: (d.start, d.route_id), reverse=True)
        keep = {d.route_id for d in dated[:settings.keep_drives]}
        chosen = oldest_first(d for d in dated if d.removable and d.route_id not in keep)
    elif settings.policy == "days":
        cutoff = now - timedelta(days=settings.keep_days)
        chosen = oldest_first(d for d in drives if d.removable and d.start < cutoff)
    elif settings.policy == "storage":
        limit = settings.keep_gb * GB
        remaining = total
        for d in oldest_first(d for d in drives if d.removable):
            if remaining <= limit:
                break
            chosen.append(d)
            remaining -= d.size
    freed = sum(d.size for d in chosen)
    after = total - freed
    return Plan(tuple(chosen), freed, total, after, limit, limit is None or after <= limit)


def dir_size(path):
    """Total size in bytes of the files under a folder (0 if it does not exist)."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += dir_size(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def collect_drives(raw_dir, stitched_dir, preserved, start_time_of, skip=()):
    """Every drive on disk as a Drive, except the ones in skip (already marked for removal)."""
    raw_dir, stitched_dir = Path(raw_dir), Path(stitched_dir)
    segments = {}
    try:
        for entry in raw_dir.iterdir():
            match = SEGMENT_DIR_RE.match(entry.name)
            if match and entry.is_dir():
                segments.setdefault(match.group("route"), []).append(entry)
    except OSError:
        pass
    routes = set(segments)
    try:
        routes |= {p.name for p in stitched_dir.iterdir() if p.is_dir() and deletion.is_valid_route_id(p.name)}
    except OSError:
        pass
    drives = []
    for route_id in sorted(routes - set(skip)):
        size = sum(dir_size(p) for p in segments.get(route_id, [])) + dir_size(stitched_dir / route_id)
        start = start_time_of(route_id)
        drives.append(Drive(
            route_id=route_id,
            start=None if start is None or start.year <= 1900 else start,
            size=size,
            starred=route_id in preserved,
            finished=(stitched_dir / route_id / ".done").is_file(),
        ))
    return drives


# ----------------------
# Status and enforcing
# ----------------------

def status_path(metadata_dir):
    return Path(metadata_dir) / STATUS_NAME


def read_status(metadata_dir):
    try:
        return json.loads(status_path(metadata_dir).read_text())
    except (OSError, ValueError):
        return {}


def _write_status(metadata_dir, status):
    path = status_path(metadata_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status))
    os.replace(tmp, path)


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enforce_once(raw_dir, stitched_dir, metadata_dir, start_time_of, load_preserved, now=None):
    """Work out what the saved policy removes and ask for it. Returns the Plan."""
    now = utc_now() if now is None else now
    settings = load_settings(metadata_dir)
    drives = collect_drives(raw_dir, stitched_dir, load_preserved(), start_time_of, skip=deletion.pending(metadata_dir))
    result = plan(drives, settings, now)
    for drive in result.remove:
        deletion.request_delete(drive.route_id, metadata_dir, source="retention")
    status = read_status(metadata_dir)
    runs = status.get("runs", [])
    if result.count:
        runs = ([{"at": now.isoformat(timespec="seconds"), "policy": settings.policy, "count": result.count,
                  "bytes": result.freed}] + runs)[:HISTORY_LENGTH]
    _write_status(metadata_dir, {
        "last_run": now.isoformat(timespec="seconds"), "policy": settings.policy, "describe": settings.describe(),
        "last_count": result.count, "last_bytes": result.freed, "limit_reached": result.limit_reached,
        "last_ids": [d.route_id for d in result.remove][:50], "runs": runs,
    })
    return result


def should_start_enforcer(environ, argv):
    """The background thread runs under gunicorn (the real server) or when RETENTION_ENFORCER=1."""
    setting = str(environ.get("RETENTION_ENFORCER", "auto")).strip().lower()
    if setting in ("0", "false", "off", "no"):
        return False
    if setting in ("1", "true", "on", "yes"):
        return True
    return bool(argv) and "gunicorn" in os.path.basename(argv[0])


_enforcer = None


def start_enforcer(run_once, interval=ENFORCE_INTERVAL_S, first_delay=ENFORCE_FIRST_DELAY_S, sleep=time.sleep):
    """Run run_once() every `interval` seconds in a background thread (only one per process)."""
    global _enforcer
    if _enforcer is not None and _enforcer.is_alive():
        return _enforcer

    def loop():
        sleep(first_delay)
        while True:
            try:
                run_once()
            except Exception as exc:  # keep going: the next run may work
                print(f"retention: run failed: {exc!r}", file=sys.stderr)
            sleep(interval)

    _enforcer = threading.Thread(target=loop, name="retention", daemon=True)
    _enforcer.start()
    return _enforcer
