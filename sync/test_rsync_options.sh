#!/bin/bash
# Tests for how device_sync.sh compares log files. Run: bash sync/test_rsync_options.sh
# Uses the real rsync on local folders; the options under test are read from the script itself.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PASS=0; FAIL=0
check() { local name=$1; shift; if "$@"; then PASS=$((PASS+1)); echo "ok   $name"; else FAIL=$((FAIL+1)); echo "FAIL $name"; fi; }

SCRIPT="$HERE/device_sync.sh"
# the option string, taken from the script's own configuration block
OPTS=$(sed -n 's/^RSYNC_LOGS_COMPARE="\(.*\)"$/\1/p' "$SCRIPT")
CH='--chmod=Du=rwx,Dg=rws,Do=rx,Fu=rw,Fg=rw,Fo=r'

# --- the script uses it for the logs and not for the videos
logs_call=$(awk '/^log "Syncing logs first/{f=1} f{print} /^log "Syncing video/{exit}' "$SCRIPT")
video_call=$(awk '/^log "Syncing video segments/{f=1} f{print} /^# stitch together/{exit}' "$SCRIPT")
check "the option string is defined once" test "$(grep -c '^RSYNC_LOGS_COMPARE=' "$SCRIPT")" -eq 1
check "it compares sizes with a time tolerance" test "$OPTS" = "--size-only --modify-window=1"
check "the logs rsync uses it" bash -c "echo '$logs_call' | grep -q '\$RSYNC_LOGS_COMPARE'"
check "the video rsync does not (it already has --ignore-existing)" bash -c "! echo '$video_call' | grep -q 'RSYNC_LOGS_COMPARE' && echo '$video_call' | grep -q -- '--ignore-existing'"

# --- behaviour, with the real rsync
T=$(mktemp -d); S=$T/src; D=$T/dst; mkdir -p "$S/seg--0" "$D"
head -c 300000 /dev/urandom > "$S/seg--0/qlog.zst"
touch -d "2026-10-19 11:10:45.336861279 UTC" "$S/seg--0/qlog.zst"
sync_logs() { rsync -rtp $CH --no-i-r --partial --timeout=30 $OPTS -i "$S/" "$D/" 2>&1 | grep -E '^[<>ch.*]f'; }

out=$(sync_logs)
check "a new log is copied" bash -c "[[ '$out' == '>f+++++++++ seg--0/qlog.zst' ]] && cmp -s '$S/seg--0/qlog.zst' '$D/seg--0/qlog.zst'"

# the share keeps 100 ns: simulate it by trimming the nanoseconds of the copy
touch -d "2026-10-19 11:10:45.336861200 UTC" "$D/seg--0/qlog.zst"
sync_logs >/dev/null
out=$(sync_logs)
check "an unchanged log is left alone once its time is settled (nothing sent)" test -z "$out"

# the copy's time is wrong (the share stamped it 'now'): only the time is fixed, the data is not rewritten
inode_before=$(stat -c %i "$D/seg--0/qlog.zst")
touch "$D/seg--0/qlog.zst"
out=$(sync_logs)
check "a wrong time is fixed without sending the data (a '.f..t' update, not '>f')" bash -c "[[ '$out' == .f..t* ]]"
check "the file was not rewritten (same inode)" test "$inode_before" = "$(stat -c %i "$D/seg--0/qlog.zst")"
check "its time matches the original again (to the second)" test "$(stat -c %Y "$D/seg--0/qlog.zst")" = "$(stat -c %Y "$S/seg--0/qlog.zst")"

# a log that grew since the last run is sent again
head -c 450000 /dev/urandom > "$S/seg--0/qlog.zst"; touch -d "2026-10-19 11:20:00 UTC" "$S/seg--0/qlog.zst"
out=$(sync_logs)
check "a log that grew is sent again" bash -c "[[ '$out' == '>f.st'* ]] && cmp -s '$S/seg--0/qlog.zst' '$D/seg--0/qlog.zst'"
check "and is then settled" bash -c "[ -z \"\$($(declare -f sync_logs); S='$S' D='$D' OPTS='$OPTS' CH='$CH'; sync_logs; sync_logs)\" ]"

# the old way, for comparison: with times compared, a copy whose time differs is rewritten
touch "$D/seg--0/qlog.zst"
old_out=$(rsync -rtp $CH --no-i-r -i "$S/" "$D/" 2>&1 | grep -E '^[<>ch.*]f')
check "(for comparison) comparing times would have rewritten it: '>f..t'" bash -c "[[ '$old_out' == '>f..t'* ]]"

rm -rf "$T"
echo; echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
