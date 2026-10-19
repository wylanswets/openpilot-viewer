#!/bin/bash
# Tests for delete_lib.sh, on throwaway folders. Run: bash sync/test_delete_lib.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "ok   $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL $1"; }
check() { local name=$1; shift; if "$@"; then ok "$name"; else fail "$name"; fi; }
gone()  { [ ! -e "$1" ]; }
there() { [ -e "$1" ]; }

setup() {
    T=$(mktemp -d)
    LOCAL_RAW=$T/raw; LOCAL_STITCHED=$T/stitched; METADATA_DIR=$T/metadata
    LOG_FILE=$T/sync.log; DELETED_FILE=$T/deleted_routes.txt; MIN_FREE_GB=75
    mkdir -p "$LOCAL_RAW" "$LOCAL_STITCHED" "$METADATA_DIR/thumbs" "$METADATA_DIR/tracks" "$METADATA_DIR/summaries"
    : > "$LOG_FILE"; : > "$DELETED_FILE"
    export LOCAL_RAW LOCAL_STITCHED METADATA_DIR LOG_FILE DELETED_FILE MIN_FREE_GB
}
log() { echo "$1" >> "$LOG_FILE"; }
check_space() { echo "${FAKE_SPACE:-100}"; }
# shellcheck source=delete_lib.sh
source "$HERE/delete_lib.sh"

# route R: two raw segments, a stitched folder, and the viewer's files
make_route() {
    local r=$1
    mkdir -p "$LOCAL_RAW/$r--0" "$LOCAL_RAW/$r--1" "$LOCAL_STITCHED/$r"
    echo x > "$LOCAL_RAW/$r--0/qlog.zst"; echo x > "$LOCAL_STITCHED/$r/fcamera.mp4"
    echo x > "$METADATA_DIR/thumbs/$r.png"; echo '{}' > "$METADATA_DIR/thumbs/$r.json"
    echo x > "$METADATA_DIR/tracks/$r.json"; echo x > "$METADATA_DIR/summaries/$r.json"
}

# --- delete_route
setup; make_route aaaa--1111; make_route aaaa--11112222
delete_route aaaa--1111; rc=$?
check "delete_route returns 0" test $rc -eq 0
check "stitched folder removed" gone "$LOCAL_STITCHED/aaaa--1111"
check "raw segments removed" gone "$LOCAL_RAW/aaaa--1111--0"
check "second raw segment removed" gone "$LOCAL_RAW/aaaa--1111--1"
check "viewer files removed (image, state, track, summary)" bash -c "[ ! -e '$METADATA_DIR/thumbs/aaaa--1111.png' ] && [ ! -e '$METADATA_DIR/thumbs/aaaa--1111.json' ] && [ ! -e '$METADATA_DIR/tracks/aaaa--1111.json' ] && [ ! -e '$METADATA_DIR/summaries/aaaa--1111.json' ]"
check "recorded in the deletion record" grep -qxF aaaa--1111 "$DELETED_FILE"
check "a route whose id starts the same is untouched (raw)" there "$LOCAL_RAW/aaaa--11112222--0"
check "a route whose id starts the same is untouched (stitched)" there "$LOCAL_STITCHED/aaaa--11112222"
check "the other route's viewer files are untouched" there "$METADATA_DIR/thumbs/aaaa--11112222.png"
delete_route aaaa--1111; rc=$?
check "deleting an already deleted route is fine" test $rc -eq 0
check "recorded only once" test "$(grep -cxF aaaa--1111 "$DELETED_FILE")" -eq 1

# raw-only route (never stitched)
setup; mkdir -p "$LOCAL_RAW/bbbb--2222--0"
delete_route bbbb--2222; rc=$?
check "a raw-only route can be deleted" bash -c "[ $rc -eq 0 ] && [ ! -e '$LOCAL_RAW/bbbb--2222--0' ] && grep -qxF bbbb--2222 '$DELETED_FILE'"

# --- preserved routes
setup; make_route cccc--3333; touch "$LOCAL_STITCHED/cccc--3333/.preserve"
delete_route cccc--3333; rc=$?
check ".preserve file protects a route (returns 2)" test $rc -eq 2
check ".preserve route keeps its data and is not recorded" bash -c "[ -e '$LOCAL_RAW/cccc--3333--0' ] && [ ! -s '$DELETED_FILE' ]"

setup; make_route dddd--4444; make_route dddd--44445555
echo '["dddd--4444", "zzzz--0000"]' > "$METADATA_DIR/preserved_routes.json"
delete_route dddd--4444; rc=$?
check "a route starred in the viewer is protected (returns 2)" test $rc -eq 2
check "starred route keeps all its data" bash -c "[ -e '$LOCAL_RAW/dddd--4444--0' ] && [ -e '$LOCAL_STITCHED/dddd--4444' ] && [ ! -s '$DELETED_FILE' ]"
delete_route dddd--44445555; rc=$?
check "a route that only shares an id prefix with a starred one is not protected" test $rc -eq 0

# --- invalid ids
setup; make_route eeee--5555; mkdir -p "$T/outside"
for bad in "" "../outside" "eeee--5555/../.." "a b--c" "--x" "x--" "x" "eeee--5555--0" '$(touch /tmp/pwned)--a'; do
    delete_route "$bad" 2>/dev/null; rc=$?
    check "invalid id '$bad' is refused (returns 3)" test $rc -eq 3
done
check "nothing was deleted by the invalid ids" bash -c "[ -e '$LOCAL_RAW/eeee--5555--0' ] && [ -e '$T/outside' ] && [ ! -s '$DELETED_FILE' ]"

# --- failure keeps the request and does not record the route
setup; make_route ffff--6666; mkdir -p "$METADATA_DIR/delete_requests"; touch "$METADATA_DIR/delete_requests/ffff--6666"
rm() { return 1; }
process_delete_requests
unset -f rm
check "a failed deletion keeps the request for the next run" there "$METADATA_DIR/delete_requests/ffff--6666"
check "a failed deletion is not recorded as deleted" bash -c "[ ! -s '$DELETED_FILE' ]"
process_delete_requests
check "the retry deletes it and clears the request" bash -c "[ ! -e '$METADATA_DIR/delete_requests/ffff--6666' ] && [ ! -e '$LOCAL_RAW/ffff--6666--0' ] && grep -qxF ffff--6666 '$DELETED_FILE'"

# --- process_delete_requests
setup; make_route gggg--7777; make_route hhhh--8888; make_route iiii--9999
echo '["hhhh--8888"]' > "$METADATA_DIR/preserved_routes.json"
mkdir -p "$METADATA_DIR/delete_requests"
touch "$METADATA_DIR/delete_requests/gggg--7777" "$METADATA_DIR/delete_requests/hhhh--8888" "$METADATA_DIR/delete_requests/..bad" "$METADATA_DIR/delete_requests/not a route"
process_delete_requests
check "requested route is deleted and its request cleared" bash -c "[ ! -e '$LOCAL_RAW/gggg--7777--0' ] && [ ! -e '$METADATA_DIR/delete_requests/gggg--7777' ]"
check "starred route in a request is kept and the request dropped" bash -c "[ -e '$LOCAL_RAW/hhhh--8888--0' ] && [ ! -e '$METADATA_DIR/delete_requests/hhhh--8888' ]"
check "requests with invalid names are dropped" bash -c "[ ! -e '$METADATA_DIR/delete_requests/..bad' ] && [ ! -e '$METADATA_DIR/delete_requests/not a route' ]"
check "routes nobody asked about are untouched" there "$LOCAL_RAW/iiii--9999--0"
check "only the deleted route is recorded" bash -c "[ \"\$(cat '$DELETED_FILE')\" = gggg--7777 ]"
setup; process_delete_requests; rc=$?
check "no request folder is fine" test $rc -eq 0

# --- free-space cleanup uses the same rules
setup; make_route jjjj--0001; make_route jjjj--0002; make_route jjjj--0003; make_route jjjj--0004
touch "$LOCAL_STITCHED/jjjj--0001/.preserve"
# free space is low (10 GB) after the first deletion and enough (80 GB >= 75) after the second
check_space() { local n; n=$(cat "$T/calls" 2>/dev/null || echo 0); echo $((n+1)) > "$T/calls"; [ "$n" -ge 1 ] && echo 80 || echo 10; }
cleanup_routes
check "cleanup skips the starred (oldest) route" there "$LOCAL_RAW/jjjj--0001--0"
check "cleanup deletes the oldest routes that are not starred" bash -c "[ ! -e '$LOCAL_RAW/jjjj--0002--0' ] && [ ! -e '$LOCAL_RAW/jjjj--0003--0' ]"
check "cleanup stops once there is enough space" there "$LOCAL_RAW/jjjj--0004--0"
check "cleanup records what it deleted" bash -c "[ \"\$(cat '$DELETED_FILE')\" = \"\$(printf 'jjjj--0002\\njjjj--0003')\" ]"

echo; echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
