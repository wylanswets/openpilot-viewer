#!/bin/bash
# Adapted from bryangerlach/openpilot-viewer c3x_sync.sh (MIT). Paths and key
# point at the container mounts. Changes: config block, known_hosts, ssh options.
set -euo pipefail

# Configuration
# Every setting can be overridden with an environment variable of the same name; the
# defaults are the container's mounts. DEVICE_IP has no default: set it to the address of
# the device (see sync/README.md).
DEVICE_IP="${DEVICE_IP:?set DEVICE_IP to the address of the device}"
DEVICE_USER="${DEVICE_USER:-comma}"
SSH_KEY="${SSH_KEY:-/keys/openpilot_sync_ed25519}"
KNOWN_HOSTS="${KNOWN_HOSTS:-/app/ssh/known_hosts}"
REMOTE_DRIVES="${REMOTE_DRIVES:-/data/media/0/realdata}"
LOCAL_RAW="${LOCAL_RAW:-/data/raw}"
LOCAL_STITCHED="${LOCAL_STITCHED:-/data/stitched}"
LOG_FILE="${LOG_FILE:-/data/logs/device_sync.log}"
DELETED_FILE="${DELETED_FILE:-/data/logs/deleted_routes.txt}"
METADATA_DIR="${METADATA_DIR:-/data/metadata}"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS -o BatchMode=yes"
# Keep partial files so a dropped connection resumes instead of restarting big files.
RSYNC_RESUME="--partial --timeout=120"
# How the log files are compared. The device keeps file times to the nanosecond and the NAS
# share only to 100 ns, so a copy never has exactly the original's time. With times compared,
# rsync saw every log file the device holds as changed on every run and rewrote all of them
# (about 1.5 GB each run, little of it over the network), and each rewrite left the share
# stamping the file with the current time. The logs never change once written, and a log that is
# still growing differs in size, so only the size is compared. rsync still sets the times,
# within the tolerance of --modify-window.
RSYNC_LOGS_COMPARE="--size-only --modify-window=1"
touch "$DELETED_FILE"

MIN_FREE_GB="${MIN_FREE_GB:-75}"
# End Configuration

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE" || true; }

MAX_LOG_SIZE=$((50*1024*1024)) # 50 MB
if [ -f "$LOG_FILE" ]; then
    size=$(stat -c%s "$LOG_FILE")
    if [ "$size" -gt "$MAX_LOG_SIZE" ]; then
        mv "$LOG_FILE" "$LOG_FILE.old"
        touch "$LOG_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') - Log rotated (old saved as .old)" >> "$LOG_FILE"
    fi
fi

# route deletion (shared by the free-space cleanup and the viewer's delete requests)
source "$(dirname "$0")/delete_lib.sh"

# carry out drives the viewer marked for deletion; no device needed for that
process_delete_requests

# ping device ip to check if online or not
if ! ping -c 1 -W 2 "$DEVICE_IP" >/dev/null 2>&1; then
    log "Device not reachable at $DEVICE_IP"
    exit 0
fi

# FUNCTION: check free space
check_space() {
    avail_gb=$(df -BG "$LOCAL_RAW" | awk 'NR==2 {gsub("G","",$4); print $4}')
    echo "$avail_gb"
}

# check free space before syncing
free_before=$(check_space)
log "Available space: ${free_before}GB"
if [ "$free_before" -lt "$MIN_FREE_GB" ]; then
    cleanup_routes
fi

# build rsync excludes file from deleted routes
RSYNC_EXCLUDES="/tmp/rsync_excludes.txt"
> "$RSYNC_EXCLUDES"

if [ -f "$DELETED_FILE" ]; then
    while read -r route; do
        [ -z "$route" ] && continue

        echo "/${route}--*/" >> "$RSYNC_EXCLUDES"
        echo "/${route}--*/**" >> "$RSYNC_EXCLUDES"

    done < "$DELETED_FILE"
fi

log "Generated rsync excludes:"
cat "$RSYNC_EXCLUDES" | tee -a "$LOG_FILE"

log "Syncing logs first..."
rsync -rtp \
  --chmod=Du=rwx,Dg=rws,Do=rx,Fu=rw,Fg=rw,Fo=r \
  --info=stats2,flist0,progress2 \
  --no-i-r $RSYNC_RESUME $RSYNC_LOGS_COMPARE \
  --prune-empty-dirs \
  -e "ssh $SSH_OPTS" \
  --exclude-from="$RSYNC_EXCLUDES" \
  --include="*/" \
  --include="*.zst" \
  --include="*.ts" \
  --exclude="*.hevc" \
  --exclude="*" \
  "$DEVICE_USER@$DEVICE_IP:$REMOTE_DRIVES/" "$LOCAL_RAW/" | tee -a "$LOG_FILE"

log "Syncing video segments..."
rsync -rtp \
  --chmod=Du=rwx,Dg=rws,Do=rx,Fu=rw,Fg=rw,Fo=r \
  --info=stats2,flist0,progress2 \
  --no-i-r $RSYNC_RESUME \
  --ignore-existing \
  --prune-empty-dirs \
  -e "ssh $SSH_OPTS" \
  --exclude-from="$RSYNC_EXCLUDES" \
  "$DEVICE_USER@$DEVICE_IP:$REMOTE_DRIVES/" "$LOCAL_RAW/" | tee -a "$LOG_FILE"

# stitch together the drive videos into one video file per camera
routes=$(find "$LOCAL_RAW" -mindepth 1 -maxdepth 1 -type d \
           -not -name boot \
           -name "*--*" \
           -printf "%f\n" \
         | awk -F'--' '{print $1"--"$2}' \
         | sort -u)

# ask the device whether it is driving; if we can't tell, assume it is (safer)
device_onroad=$(ssh $SSH_OPTS "$DEVICE_USER@$DEVICE_IP" 'cat /data/params/d/IsOnroad' 2>/dev/null || echo 1)
newest_route=$(echo "$routes" | tail -n1)
for route_id in $routes; do
    stitched_path="$LOCAL_STITCHED/$route_id"
    # .done is written only after every camera succeeds, so a failed route is retried
    [ -f "$stitched_path/.done" ] && continue
    # the device numbers routes in order; the last one is only held back while the device is driving
    if [ "$route_id" = "$newest_route" ] && [ "$device_onroad" != "0" ]; then
        log "Route $route_id is the newest on the device, stitching later"
        continue
    fi
    mkdir -p -m 2775 "$stitched_path"
    log "Stitching route $route_id..."
    route_ok=true

    # get the start time and save it to a text file
    first_seg=$(find "$LOCAL_RAW" -type d -name "$route_id--0" | sort -V | head -n1)
    if [ -n "$first_seg" ]; then
        route_time=$(stat -c %y "$first_seg" | cut -d'.' -f1)
        echo "$route_time" > "$stitched_path/start_time.txt"
        log "Route $route_id start time: $route_time"
    else
        log "No segment 0 found for route $route_id, skipping start_time metadata"
    fi

    # stitch the videos together for each camera
    for cam in fcamera ecamera dcamera; do
        output="$stitched_path/${cam}.mp4"
        filelist=$(mktemp)

        # find all segment folders for this route, sorted by segment number
        find "$LOCAL_RAW" -type d -name "$route_id--*" \
            | sort -V \
            | while read segdir; do
                hevc_file="$segdir/${cam}.hevc"
                if [ -f "$hevc_file" ]; then echo "file '$hevc_file'" >> "$filelist"; fi
            done

        if [ -s "$filelist" ]; then
            log "Stitching $cam -> $output"
            # -tag:v hvc1: Safari and iPhones only play HEVC in an MP4 when it is tagged hvc1 (ffmpeg
            # writes hev1 by default, which Chrome accepts). -movflags +faststart puts the index at the
            # start of the file so playback can begin without reading to the end first. Nothing is
            # re-encoded.
            if ! ffmpeg -y -loglevel error -f concat -safe 0 -i "$filelist" -c copy -tag:v hvc1 -movflags +faststart "$output"; then
                log "ERROR: stitching $cam failed for $route_id"
                rm -f "$output"
                route_ok=false
            else
                # generate thumbnails; a failed thumbnail is a warning, not a failed route
                thumbs_dir="$stitched_path/thumbs/$cam"
                mkdir -p -m 2775 "$thumbs_dir"
                duration=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$output" || true)
                if [ -n "$duration" ]; then
                    # 3 thumbnails evenly spaced
                    for i in 1 2 3; do
                        ts=$(awk "BEGIN {print $duration*$i/4}")
                        ffmpeg -y -loglevel error -ss "$ts" -i "$output" -frames:v 1 -update 1 -s 320x180 "$thumbs_dir/thumb_$i.jpg" \
                            || log "WARN: thumbnail $i failed for $cam in $route_id"
                    done
                else
                    log "WARN: no duration for $output, thumbnails skipped"
                fi
            fi
        else
            log "No $cam files found in route $route_id"
        fi

        rm -f "$filelist"
    done

    if $route_ok; then
        touch "$stitched_path/.done"
        log "Finished stitching route $route_id"
    else
        log "Route $route_id incomplete, will retry on next run"
    fi
done
