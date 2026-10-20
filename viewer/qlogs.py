"""Shared helpers for reading a route's qlogs, with a per-segment JSON cache."""
import json
import os
from pathlib import Path

import capnp
import zstandard as zstd

CEREAL_DIR = Path(__file__).resolve().parent / "cereal"

_log_capnp = None


def schema():
    global _log_capnp
    if _log_capnp is None:
        _log_capnp = capnp.load(str(CEREAL_DIR / "log.capnp"), imports=[str(CEREAL_DIR)])
    return _log_capnp


def read_events(qlog_path):
    """Decompress one qlog.zst and return its capnp events."""
    with open(qlog_path, "rb") as f:
        data = zstd.ZstdDecompressor().stream_reader(f).read()
    return schema().Event.read_multiple_bytes(data)


def _load_cache(cache_file, version):
    try:
        with open(cache_file) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return {}
    if cached.get("version") != version:
        return {}
    return cached.get("segments", {})


def _save_cache(cache_file, version, segments):
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"version": version, "segments": segments}, f)
        os.replace(tmp, cache_file)
    except OSError:
        pass  # the cache is an optimisation; a read-only metadata dir is fine


def _found_segments(route_id, raw_dir):
    """[(segment number, qlog path)] for the segments of a route that have a qlog, in order."""
    found = []
    for folder in Path(raw_dir).glob(f"{route_id}--*"):
        suffix = folder.name.rsplit("--", 1)[-1]
        qlog = folder / "qlog.zst"
        if suffix.isdigit() and qlog.is_file():
            found.append((int(suffix), qlog))
    return sorted(found)


def _is_current(entry, stat_result):
    """Is this cache entry for the qlog as it is now?"""
    return bool(
        entry
        and "data" in entry
        and entry.get("size") == stat_result.st_size
        and entry.get("mtime") == int(stat_result.st_mtime)
    )


def cached_only(route_id, raw_dir, cache_file, version=1):
    """What is cached for a route, without parsing anything: ({segment: data}, complete).

    `complete` is True when the route has qlogs and every one of them has an up to date
    cache entry. Cheap enough to call while a page is being built.
    """
    cached = _load_cache(Path(cache_file), version)
    data, complete, any_found = {}, True, False
    for number, qlog in _found_segments(route_id, raw_dir):
        any_found = True
        entry = cached.get(str(number))
        if _is_current(entry, qlog.stat()):
            data[number] = entry["data"]
        else:
            complete = False
    return data, complete and any_found


def cached_segments(route_id, raw_dir, cache_file, parse, version=1):
    """Return {segment number: parse(qlog path)} for every qlog of a route.

    Each segment is parsed once and cached with its qlog size and mtime, so a
    route that is still syncing only parses its new segments. Bump `version`
    when `parse` changes what it returns. A corrupt or half-synced qlog is
    skipped and not cached.
    """
    cache_file = Path(cache_file)
    cached = _load_cache(cache_file, version)
    segments = {}
    changed = False

    for number, qlog in _found_segments(route_id, raw_dir):
        st = qlog.stat()
        entry = cached.get(str(number))
        if not _is_current(entry, st):
            try:
                entry = {"size": st.st_size, "mtime": int(st.st_mtime), "data": parse(qlog)}
            except Exception:
                continue
            changed = True
        segments[str(number)] = entry

    if changed or set(segments) != set(cached):
        _save_cache(cache_file, version, segments)

    return {int(n): entry["data"] for n, entry in segments.items()}
