import os
import json
import re
import capnp
import zstandard as zstd
import io
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from collections import Counter

from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.core.paginator import Paginator

from django.views.decorators.http import require_POST
from django.contrib import messages
import shutil

from . import deletion, prepare, summary, tracks
from . import thumbnails as route_thumbnails  # drive_list has a local named thumbnails

DEFAULT_OLD_DATE = datetime(1900, 1, 1)

STITCHED_DIR = Path("/data/stitched")  # Mounted in Docker
RAW_DIR = Path("/data/raw")

SEGMENT_RE = re.compile(r"^(.*?)(--\d+)?$")

CAMERA_LABELS = {
    "fcamera.mp4": "Front Camera",
    "ecamera.mp4": "Wide Camera",
    "dcamera.mp4": "Driver Camera",
}

METADATA_DIR = Path("/data/metadata")
METADATA_DIR.mkdir(exist_ok=True)
PRESERVED_FILE = METADATA_DIR / "preserved_routes.json"


# ----------------------
# Helpers
# ----------------------

def normalize_route_id(name: str) -> str:
    """Remove --N suffix from segment folder names."""
    match = SEGMENT_RE.match(name)
    return match.group(1) if match else name


def load_preserved_routes():
    if PRESERVED_FILE.exists():
        with open(PRESERVED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_preserved_routes(preserved_set):
    with open(PRESERVED_FILE, "w") as f:
        json.dump(list(preserved_set), f)


def get_route_start_time(route_id: str) -> datetime:
    """
    Try to determine route start time.
    Priority:
      1. start_time.txt in stitched folder
      2. mtime of segment 0 folder in raw
    """
    stitched_path = STITCHED_DIR / route_id
    start_time_file = stitched_path / "start_time.txt"

    # Case 1: stitched has start_time.txt
    if start_time_file.exists():
        try:
            with open(start_time_file) as f:
                ts = f.read().strip()
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    # Case 2: use segment 0 folder mtime
    seg0 = RAW_DIR / f"{route_id}--0"
    if seg0.exists() and seg0.is_dir():
        ts = datetime.fromtimestamp(seg0.stat().st_mtime)
        return ts

    # Fallback
    return DEFAULT_OLD_DATE

# Recreate stitched video
@require_POST
def recreate_stitched(request, route_id):
    """Delete stitched folder so it can be regenerated."""
    stitched_path = STITCHED_DIR / route_id
    if stitched_path.exists() and stitched_path.is_dir():
        shutil.rmtree(stitched_path)
        messages.success(request, f"Stitched video for {route_id} will be recreated on next sync.")
    else:
        messages.warning(request, f"No stitched video exists for {route_id} to recreate.")

    return redirect("drive_detail", route_id=route_id)


# ----------------------
# Views
# ----------------------

def toggle_preserve(request, route_id):
    preserved = load_preserved_routes()
    if route_id in preserved:
        preserved.remove(route_id)
    else:
        preserved.add(route_id)
    save_preserved_routes(preserved)
    return redirect(request.META.get("HTTP_REFERER", "/"))


def drive_list(request):
    """List all drives (logs-only or stitched). Limit to 20 per page."""
    drives = []
    cameras = ["fcamera", "ecamera", "dcamera"]
    thumb_indices = [1, 2, 3]

    preserved_routes = load_preserved_routes()
    show_preserved_only = request.GET.get("preserved") == "1"

    # collect raw routes, grouped by base route_id
    raw_routes_grouped = defaultdict(list)
    for d in RAW_DIR.iterdir():
        if not d.is_dir():
            continue
        base_id = normalize_route_id(d.name)
        raw_routes_grouped[base_id].append(d)

    # collect stitched routes
    stitched_routes = {d.name: d for d in STITCHED_DIR.iterdir() if d.is_dir()}

    # union of both sets
    all_route_ids = set(raw_routes_grouped.keys()) | set(stitched_routes.keys())

    # drives marked for deletion are hidden at once; the sync removes them from disk
    marked = deletion.pending(METADATA_DIR)
    pending_deletes = sorted(all_route_ids & marked)
    all_route_ids -= marked

    for route_id in all_route_ids:
        stitched_path = stitched_routes.get(route_id)
        raw_paths = raw_routes_grouped.get(route_id, [])

        # try to determine start_time
        start_time = get_route_start_time(route_id)
        if start_time is None:
            start_time = DEFAULT_OLD_DATE

        # Only cameras that have footage in this drive get a thumbnail row.
        thumbnails = []
        if stitched_path:
            for cam in cameras:
                if not (stitched_path / f"{cam}.mp4").exists():
                    continue
                thumbnails.append({
                    "label": CAMERA_LABELS.get(f"{cam}.mp4", cam),
                    "thumbs": [
                        f"{route_id}/thumbs/{cam}/thumb_{i}.jpg"
                        for i in thumb_indices
                    ],
                })

        drive = {
            "route_id": route_id,
            "stitched": bool(stitched_path),
            "start_time": start_time,
            "thumbnails": thumbnails,
        }

        if show_preserved_only and route_id not in preserved_routes:
            continue

        drives.append(drive)

    # sort newest first
    drives.sort(
        key=lambda d: d["start_time"] if d["start_time"] != DEFAULT_OLD_DATE else datetime.min,
        reverse=True
    )

    page_size = 20
    paginator = Paginator(drives, page_size)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Route map thumbnails: show the ones that exist, make the missing ones in the background.
    thumbs_dir = METADATA_DIR / "thumbs"
    missing = []
    to_prepare = []  # drives that need a map image or a duration worked out in the background
    summaries_dir = METADATA_DIR / "summaries"
    for drive in page_obj:
        route_id = drive["route_id"]
        drive["duration_s"] = summary.cached_duration(route_id, RAW_DIR, summaries_dir)
        image = route_thumbnails.image_path(thumbs_dir, route_id)
        if image.exists():
            drive["thumbnail_url"] = f"{reverse('drive_thumbnail', args=[route_id])}?v={int(image.stat().st_mtime)}"
        if route_thumbnails.needs_work(thumbs_dir, route_id, len(raw_routes_grouped.get(route_id, []))):
            drive["thumbnail_pending"] = True
            missing.append(route_id)
        if route_id in missing or drive["duration_s"] is None:
            to_prepare.append(route_id)
    route_thumbnails.start_background(
        to_prepare, RAW_DIR, METADATA_DIR, make=prepare.prepare_route, stitched_dir=STITCHED_DIR
    )

    return render(request, "viewer/drive_list.html", {
        "page_obj": page_obj,
        "thumb_indices": thumb_indices,
        "preserved_routes": preserved_routes,
        "show_preserved_only": show_preserved_only,
        "pending_deletes": pending_deletes,
    })


def drive_detail(request, route_id):
    """Show stitched videos if available, otherwise logs-only route detail."""
    if route_id in deletion.pending(METADATA_DIR):
        messages.info(request, f"{route_id} is marked for deletion.")
        return redirect("drive_list")
    drive_path = STITCHED_DIR / route_id
    videos = []

    if drive_path.is_dir():
        for filename in sorted(os.listdir(drive_path)):
            if filename.endswith(".mp4"):
                videos.append({
                    "file": f"/media/{route_id}/{filename}",
                    "label": CAMERA_LABELS.get(filename, filename)
                })

    # check stitched first, then raw segments for start_time
    start_time = get_route_start_time(route_id)

    if start_time is None:
        start_time = DEFAULT_OLD_DATE

    drive = {
        "route_id": route_id,
        "stitched": drive_path.is_dir(),
        "videos": videos,
        "start_time": start_time,
        "duration_s": summary.cached_duration(route_id, RAW_DIR, METADATA_DIR / "summaries"),
    }

    preserved_routes = load_preserved_routes()
    return render(request, "viewer/drive_detail.html", {
        "drive": drive,
        "preserved_routes": preserved_routes
    })

@require_POST
def delete_drive(request, route_id):
    """Mark a drive for deletion; the sync deletes it (and keeps it from coming back)."""
    if not deletion.is_valid_route_id(route_id):
        raise Http404("Unknown route")
    on_disk = (STITCHED_DIR / route_id).is_dir() or any(RAW_DIR.glob(f"{route_id}--*"))
    if not on_disk:
        raise Http404("Unknown route")
    if route_id in load_preserved_routes():
        messages.error(request, f"{route_id} is starred. Unstar it first if you want to delete it.")
        return redirect("drive_detail", route_id=route_id)
    deletion.request_delete(route_id, METADATA_DIR)
    messages.success(
        request,
        f"{route_id} is marked for deletion. It is hidden now and is removed from this "
        "server by the next sync (within about 5 minutes). The copy on the device is not touched.",
    )
    return redirect("drive_list")


@require_POST
def undo_delete(request, route_id):
    """Withdraw a deletion that the sync has not carried out yet."""
    if not deletion.is_valid_route_id(route_id):
        raise Http404("Unknown route")
    if deletion.cancel(route_id, METADATA_DIR):
        messages.success(request, f"{route_id} will not be deleted.")
        return redirect("drive_detail", route_id=route_id)
    messages.warning(request, f"{route_id} was not waiting to be deleted; it may already be gone.")
    return redirect("drive_list")


def drive_track(request, route_id):
    """GPS track of a route as JSON, for the map on the drive page."""
    if not tracks.VALID_ROUTE_RE.match(route_id):
        raise Http404("Unknown route")
    return JsonResponse(tracks.route_track(route_id, RAW_DIR, METADATA_DIR / "tracks"))


def drive_summary(request, route_id):
    """Driving summary of a route as JSON, for the summary panel on the drive page."""
    if not tracks.VALID_ROUTE_RE.match(route_id):
        raise Http404("Unknown route")
    return JsonResponse(summary.route_summary(route_id, RAW_DIR, METADATA_DIR / "summaries"))


def drive_thumbnail(request, route_id):
    """The route map thumbnail (PNG) of a route, if it has been made."""
    if not tracks.VALID_ROUTE_RE.match(route_id):
        raise Http404("Unknown route")
    path = route_thumbnails.image_path(METADATA_DIR / "thumbs", route_id)
    if not path.is_file():
        raise Http404("No thumbnail")
    response = FileResponse(open(path, "rb"), content_type="image/png")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def segment_list(request, route_id):
    """Lists all available segments (minutes) for a route that have logs."""
    segments = []
    
    for segment_folder in sorted(RAW_DIR.glob(f"{route_id}--*")):
        if segment_folder.is_dir():
            qlog_path = segment_folder / "qlog.zst"
            rlog_path = segment_folder / "rlog.zst"
            
            log_file = None
            if qlog_path.exists():
                log_file = "qlog.zst"
            elif rlog_path.exists():
                log_file = "rlog.zst"
                
            if log_file:
                seg_num = segment_folder.name.split("--")[-1]
                segments.append({
                    "number": int(seg_num),
                    "folder_name": segment_folder.name,
                    "log_type": log_file
                })
    segments.sort(key=lambda x: x['number'])

    return render(request, "viewer/segment_list.html", {
        "route_id": route_id,
        "segments": segments
    })

def log_detail(request, route_id, segment_num):
    """Parses and displays a specific log file."""
    segment_folder = f"{route_id}--{segment_num}"
    log_path = RAW_DIR / segment_folder / "qlog.zst"

    if not log_path.exists():
        return render(request, "error.html", {"message": "Log file not found."})

    # Load Schema
    CURRENT_DIR = Path(__file__).resolve().parent
    CEREAL_DIR = CURRENT_DIR / "cereal"
    LOG_CAPNP_PATH = CEREAL_DIR / "log.capnp"

    if LOG_CAPNP_PATH.exists():
        log_capnp = capnp.load(str(LOG_CAPNP_PATH), imports=[str(CEREAL_DIR)])
    else:
        log_capnp = None
        print(f"WARNING: log.capnp not found at {LOG_CAPNP_PATH}")

    events = []
    unique_types = set()
    dctx = zstd.ZstdDecompressor()
    active_filter = request.GET.get('type', 'errorLogMessage')
    
    try:
        with open(log_path, 'rb') as f:
            with dctx.stream_reader(f) as reader:
                data = reader.read()
            
            log_events = log_capnp.Event.read_multiple_bytes(data)
            
            for event in log_events:
                msg_type = event.which()
                unique_types.add(msg_type)
                
                if active_filter == 'all' or msg_type == active_filter:
                    events.append({
                        "type": msg_type,
                        "time": event.logMonoTime,
                        "data": event.to_dict().get(msg_type, {})
                    })
                
    except Exception as e:
        return render(request, "viewer/error.html", {"message": f"Parsing Error: {e}"})

    sorted_types = sorted(list(unique_types))

    return render(request, "viewer/log_detail.html", {
        "route_id": route_id,
        "segment_num": segment_num,
        "events": events,
        "event_types": sorted_types,
        "active_filter": active_filter,
    })
