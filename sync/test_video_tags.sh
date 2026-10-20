#!/bin/bash
# Tests for the iPhone-friendly video packaging. Run: bash sync/test_video_tags.sh
# The first group needs nothing; the rest need ffmpeg with libx265 (it is in the sync image:
#   docker run --rm -v "$PWD/sync:/t:ro" --entrypoint bash <sync image> /t/test_video_tags.sh ) and is skipped without it.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PASS=0; FAIL=0; SKIP=0
check() { local name=$1; shift; if "$@"; then PASS=$((PASS+1)); echo "ok   $name"; else FAIL=$((FAIL+1)); echo "FAIL $name"; fi; }

# --- the sync script writes new videos in the Safari-friendly way
stitch_line=$(grep -n 'concat -safe 0' "$HERE/device_sync.sh")
check "the stitching ffmpeg call tags the video hvc1" bash -c "echo '$stitch_line' | grep -q -- '-tag:v hvc1'"
check "...and puts the index first (faststart)" bash -c "echo '$stitch_line' | grep -q -- '-movflags +faststart'"
check "...and still copies the stream (no re-encode)" bash -c "echo '$stitch_line' | grep -q -- '-c copy'"
check "the thumbnail ffmpeg calls are unchanged (no tag flags)" bash -c "! grep -- '-frames:v 1' '$HERE/device_sync.sh' | grep -q -- 'hvc1'"
check "the Dockerfile copies the repackaging script" grep -q 'fix_video_tags.sh' "$HERE/Dockerfile"
check "fix_video_tags.sh parses" bash -n "$HERE/fix_video_tags.sh"

if ! command -v ffmpeg >/dev/null || ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx265; then
    echo "SKIP the video tests: ffmpeg with libx265 is not available here"
    echo; echo "$PASS passed, $FAIL failed, video tests skipped"; [ "$FAIL" -eq 0 ]; exit
fi

T=$(mktemp -d); S=$T/stitched; export LOG_FILE=$T/fix.log LOCK_FILE=$T/lock
tag()  { ffprobe -v error -select_streams v:0 -show_entries stream=codec_tag_string -of csv=p=0 "$1"; }
dur()  { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
frames() { ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 "$1"; }
moov_first() { head -c 65536 "$1" | grep -c -a moov; }
make_video() { # make_video file tag
    ffmpeg -y -loglevel error -f lavfi -i "testsrc=size=320x240:rate=20:duration=3" -c:v libx265 -pix_fmt yuv420p \
        -x265-params log-level=none -tag:v "$2" "$1"; }
run() { bash "$HERE/fix_video_tags.sh" "$@" >/dev/null 2>&1; }

mkdir -p "$S/A" "$S/B" "$S/C" "$S/D"
make_video "$S/A/fcamera.mp4" hev1; touch "$S/A/.done"; touch -d "2026-10-01 12:00:00" "$S/A/fcamera.mp4"
make_video "$S/B/fcamera.mp4" hev1                                   # no .done: still being stitched
make_video "$S/C/fcamera.mp4" hvc1; touch "$S/C/.done"               # already fine
make_video "$S/D/fcamera.mp4" hev1; touch "$S/D/.done"
check "(setup) the test video starts as hev1 with the index at the end" bash -c "[ '$(tag $S/A/fcamera.mp4)' = hev1 ] && [ $(moov_first $S/A/fcamera.mp4) -eq 0 ]"
frames_before=$(frames "$S/A/fcamera.mp4"); dur_before=$(dur "$S/A/fcamera.mp4")
b_hash=$(sha256sum "$S/B/fcamera.mp4" | cut -d' ' -f1); c_inode=$(stat -c %i "$S/C/fcamera.mp4"); d_hash=$(sha256sum "$S/D/fcamera.mp4" | cut -d' ' -f1)

# dry run changes nothing
run --dry-run "$S"; rc=$?
check "a dry run changes nothing" bash -c "[ '$(sha256sum $S/D/fcamera.mp4 | cut -d' ' -f1)' = '$d_hash' ] && [ '$(tag $S/D/fcamera.mp4)' = hev1 ]"
check "...and says what it would do" grep -q "DRY RUN: would repackage A/fcamera.mp4" "$LOG_FILE"

# the real run
run "$S"; rc=$?
check "the run succeeds" test $rc -eq 0
check "a finished route's video is now hvc1" test "$(tag "$S/A/fcamera.mp4")" = hvc1
check "its index is at the front" test "$(moov_first "$S/A/fcamera.mp4")" -eq 1
check "same duration" bash -c "awk -v a=$dur_before -v b=$(dur $S/A/fcamera.mp4) 'BEGIN{d=a-b; if(d<0)d=-d; exit !(d<=0.1)}'"
check "same number of frames (nothing re-encoded or dropped)" test "$(frames "$S/A/fcamera.mp4")" = "$frames_before"
check "the file time is kept" test "$(stat -c %Y "$S/A/fcamera.mp4")" = "$(date -d '2026-10-01 12:00:00' +%s)"
check "no temporary file is left" bash -c "[ -z \"\$(find $S -name '*hvc1-tmp*')\" ]"
check "a route without .done is not touched" test "$(sha256sum "$S/B/fcamera.mp4" | cut -d' ' -f1)" = "$b_hash"
check "a video that is already hvc1 is not rewritten (same inode)" test "$(stat -c %i "$S/C/fcamera.mp4")" = "$c_inode"
check "the other finished route was fixed too" test "$(tag "$S/D/fcamera.mp4")" = hvc1
check "the log summarises the run" grep -q "Finished: 2 repackaged, 1 already fine, 0 errors, 1 routes skipped" "$LOG_FILE"

a_inode=$(stat -c %i "$S/A/fcamera.mp4"); run "$S"; rc=$?
check "running again changes nothing (same inode) and succeeds" bash -c "[ $rc -eq 0 ] && [ '$a_inode' = '$(stat -c %i $S/A/fcamera.mp4)' ]"

# failures never damage the original
make_video "$S/D/ecamera.mp4" hev1; e_hash=$(sha256sum "$S/D/ecamera.mp4" | cut -d' ' -f1)
mkdir -p "$T/stub1" "$T/stub2"
printf '#!/bin/bash\nfor a; do last=$a; done; echo garbage > "$last"; exit 0\n' > "$T/stub1/ffmpeg"   # "succeeds" but writes junk
printf '#!/bin/bash\nexit 1\n' > "$T/stub2/ffmpeg"                                                   # fails
chmod +x "$T/stub1/ffmpeg" "$T/stub2/ffmpeg"
PATH="$T/stub1:$PATH" run "$S"; rc=$?
check "a result that fails the checks leaves the original alone, and the run reports an error" bash -c "[ $rc -ne 0 ] && [ '$(sha256sum $S/D/ecamera.mp4 | cut -d' ' -f1)' = '$e_hash' ]"
check "...with no temporary file left" bash -c "[ -z \"\$(find $S -name '*hvc1-tmp*')\" ]"
check "...and an ERROR in the log" grep -q "ERROR: check failed for D/ecamera.mp4" "$LOG_FILE"
PATH="$T/stub2:$PATH" run "$S"; rc=$?
check "an ffmpeg failure leaves the original alone and reports an error" bash -c "[ $rc -ne 0 ] && [ '$(sha256sum $S/D/ecamera.mp4 | cut -d' ' -f1)' = '$e_hash' ]"
run "$S"; rc=$?
check "a later normal run fixes it" bash -c "[ $rc -eq 0 ] && [ '$(tag $S/D/ecamera.mp4)' = hvc1 ]"

# only one run at a time
( exec 9>"$LOCK_FILE"; flock -n 9; sleep 3 ) &
sleep 1; run "$S"; rc=$?; wait
check "a second run while one is going refuses to start" test $rc -eq 1

rm -rf "$T"
echo; echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
