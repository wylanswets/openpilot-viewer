#!/bin/bash
# Repackage stitched videos so that Safari and iPhones can play them.
#
# Older stitched videos are HEVC tagged hev1 with the index at the end of the file. Safari only
# plays HEVC in an MP4 when it is tagged hvc1 (Chrome accepts both), and it starts faster with the
# index at the front. device_sync.sh now writes videos that way; this fixes the ones made before.
#
# Stream copy only: nothing is re-encoded, the picture is identical. Each file is written next to
# the original under a temporary name, checked (tag, duration, size), and only then moved over the
# original, so a failed or interrupted run never leaves a broken video behind. Safe to run again:
# files that are already hvc1 are skipped. Routes without a .done marker (still being stitched) are
# skipped.
#
# Usage: fix_video_tags.sh [--dry-run] [stitched folder]      (default /data/stitched)
#   in the sync container:  docker exec -d openpilot-sync /app/fix_video_tags.sh
set -u

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && { DRY_RUN=1; shift; }
STITCHED="${1:-${LOCAL_STITCHED:-/data/stitched}}"
LOG_FILE="${LOG_FILE:-/data/logs/fix_video_tags.log}"
LOCK_FILE="${LOCK_FILE:-/tmp/fix_video_tags.lock}"
DURATION_TOLERANCE=0.5   # seconds
SIZE_TOLERANCE_PCT=5

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"; }

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "already running"; exit 1; }

video_tag() { ffprobe -v error -select_streams v:0 -show_entries stream=codec_tag_string -of csv=p=0 "$1" 2>/dev/null; }
duration()  { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null; }
free_bytes() { df -B1 --output=avail "$1" | tail -1 | tr -d ' '; }

fixed=0; skipped=0; errors=0; unfinished=0
for dir in "$STITCHED"/*/; do
    [ -d "$dir" ] || continue
    route=$(basename "$dir")
    if [ ! -f "$dir/.done" ]; then
        unfinished=$((unfinished + 1))
        continue
    fi
    for f in "$dir"*.mp4; do
        [ -f "$f" ] || continue
        case "$f" in *.hvc1-tmp.mp4) continue ;; esac
        tag=$(video_tag "$f")
        if [ "$tag" != "hev1" ]; then
            skipped=$((skipped + 1))
            continue
        fi
        size=$(stat -c %s "$f")
        if [ "$DRY_RUN" -eq 1 ]; then
            log "DRY RUN: would repackage $route/$(basename "$f") ($((size / 1000000)) MB)"
            fixed=$((fixed + 1))
            continue
        fi
        tmp="${f%.mp4}.hvc1-tmp.mp4"
        rm -f "$tmp"
        if [ "$(free_bytes "$dir")" -lt $((size * 2)) ]; then
            log "ERROR: not enough free space to repackage $route/$(basename "$f"), skipping"
            errors=$((errors + 1))
            continue
        fi
        log "Repackaging $route/$(basename "$f") ($((size / 1000000)) MB)..."
        if ! ffmpeg -y -loglevel error -i "$f" -c copy -tag:v hvc1 -movflags +faststart "$tmp"; then
            log "ERROR: ffmpeg failed for $route/$(basename "$f"); original left as it was"
            rm -f "$tmp"
            errors=$((errors + 1))
            continue
        fi
        new_size=$(stat -c %s "$tmp" 2>/dev/null || echo 0)
        old_dur=$(duration "$f"); new_dur=$(duration "$tmp")
        if [ "$(video_tag "$tmp")" != "hvc1" ] \
           || ! awk -v a="${old_dur:-0}" -v b="${new_dur:-0}" -v t="$DURATION_TOLERANCE" 'BEGIN { d = a - b; if (d < 0) d = -d; exit !(a > 0 && d <= t) }' \
           || ! awk -v a="$size" -v b="$new_size" -v p="$SIZE_TOLERANCE_PCT" 'BEGIN { d = a - b; if (d < 0) d = -d; exit !(b > 0 && d * 100 <= a * p) }'; then
            log "ERROR: check failed for $route/$(basename "$f") (tag $(video_tag "$tmp"), duration $old_dur -> $new_dur, size $size -> $new_size); original left as it was"
            rm -f "$tmp"
            errors=$((errors + 1))
            continue
        fi
        touch -r "$f" "$tmp"
        mv -f "$tmp" "$f"
        log "Done $route/$(basename "$f"): hvc1, ${new_dur}s"
        fixed=$((fixed + 1))
    done
done

log "Finished: $fixed $([ "$DRY_RUN" -eq 1 ] && echo 'to repackage' || echo 'repackaged'), $skipped already fine, $errors errors, $unfinished routes skipped (not finished)"
[ "$errors" -eq 0 ]
