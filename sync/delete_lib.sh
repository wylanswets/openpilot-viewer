#!/bin/bash
# Route deletion for the sync. Sourced by c3x_sync.sh (and by test_delete_lib.sh).
#
# Needs: LOCAL_RAW LOCAL_STITCHED DELETED_FILE METADATA_DIR LOG_FILE MIN_FREE_GB,
# and the functions log() and check_space().
#
# Everything that deletes a route goes through delete_route(), so the rules are in one place:
#   - starred (preserved) routes are never deleted,
#   - the route is added to DELETED_FILE, which becomes rsync excludes, so it is not
#     downloaded from the device again,
#   - the viewer's files for the route (map image, cached track and summary) go too.
# The viewer asks for a deletion by creating an empty file named after the route in
# $METADATA_DIR/delete_requests/ ; process_delete_requests() carries those out.

ROUTE_ID_RE='^[A-Za-z0-9]+--[A-Za-z0-9]+$'

# A route id is used in paths and globs, so only this shape is accepted.
valid_route_id() { [[ "$1" =~ $ROUTE_ID_RE ]]; }

# Starred in the viewer (metadata/preserved_routes.json) or marked with a .preserve file.
route_is_preserved() {
    local route=$1 list="$METADATA_DIR/preserved_routes.json"
    [ -f "$LOCAL_STITCHED/$route/.preserve" ] && return 0
    [ -f "$list" ] && grep -qF "\"$route\"" "$list"
}

# The viewer's own files for a route (see viewer/thumbnails.py, tracks.py, summary.py).
remove_viewer_files() {
    local route=$1
    rm -f -- "$METADATA_DIR/thumbs/$route.png" "$METADATA_DIR/thumbs/$route.json" \
        "$METADATA_DIR/tracks/$route.json" "$METADATA_DIR/summaries/$route.json" 2>>"$LOG_FILE" || true
}

# Delete one route. Returns 0 deleted, 1 failed (try again later), 2 preserved, 3 invalid id.
delete_route() {
    local route=$1 remaining
    if ! valid_route_id "$route"; then
        log "ERROR: refusing to delete invalid route id '$route'"
        return 3
    fi
    if route_is_preserved "$route"; then
        log "Skipping preserved route $route"
        return 2
    fi

    log "Deleting route $route..."
    if ! rm -rf -- "$LOCAL_STITCHED/$route" 2>>"$LOG_FILE"; then
        log "ERROR: Failed to delete stitched route $route"
        return 1
    fi
    if ! find "$LOCAL_RAW" -maxdepth 1 -type d -name "${route}--*" -print0 \
            | xargs -0 -r rm -rf -- 2>>"$LOG_FILE"; then
        log "ERROR: Failed deleting raw segments for $route"
        return 1
    fi

    # verify the deletion actually succeeded
    remaining=$(find "$LOCAL_RAW" -maxdepth 1 -type d -name "${route}--*" | wc -l)
    if [ -d "$LOCAL_STITCHED/$route" ] || [ "$remaining" -gt 0 ]; then
        log "ERROR: Route $route still exists after deletion attempt"
        return 1
    fi

    # record it only after a successful deletion, so the sync does not fetch it again
    grep -qxF "$route" "$DELETED_FILE" 2>/dev/null || echo "$route" >> "$DELETED_FILE"
    remove_viewer_files "$route"
    log "Successfully deleted route $route"
    return 0
}

# Carry out the deletions the viewer asked for.
process_delete_requests() {
    local dir="$METADATA_DIR/delete_requests" marker route rc
    [ -d "$dir" ] || return 0
    # find, not a glob, so that files whose names start with a dot are seen too
    while IFS= read -r -d '' marker; do
        route=$(basename "$marker")
        rc=0
        delete_route "$route" || rc=$?
        case $rc in
            0) rm -f -- "$marker" ;;
            2) log "Delete request for preserved route $route dropped"; rm -f -- "$marker" ;;
            3) rm -f -- "$marker" ;;
            *) log "Delete request for $route failed, will retry on the next run" ;;
        esac
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -type f -print0)
    return 0
}

# Free space: delete the oldest routes (never starred ones) until MIN_FREE_GB is available.
cleanup_routes() {
    local route_dir route_id avail
    log "Free space below ${MIN_FREE_GB}GB, cleaning up old routes..."
    find "$LOCAL_STITCHED" -mindepth 1 -maxdepth 1 -type d | sort | while read -r route_dir; do
        route_id=$(basename "$route_dir")
        delete_route "$route_id" || continue

        avail=$(check_space)
        if [ "$avail" -ge "$MIN_FREE_GB" ]; then
            log "Cleanup finished, free space now ${avail}GB"
            return
        fi
    done
}
