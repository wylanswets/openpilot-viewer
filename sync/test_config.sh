#!/bin/bash
# Tests for the configuration block of device_sync.sh. Run: bash sync/test_config.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PASS=0; FAIL=0
check() { local name=$1; shift; if "$@"; then PASS=$((PASS+1)); echo "ok   $name"; else FAIL=$((FAIL+1)); echo "FAIL $name"; fi; }

T=$(mktemp -d)
# only the configuration block, run in a clean environment (DELETED_FILE moved to a temp
# folder because the block touches it)
block() { sed -n '/^# Configuration$/,/^# End Configuration$/p' "$HERE/device_sync.sh"; }
run() { # run VAR=value ... -- then print the variables named in $PRINT
    env -i PATH="$PATH" DELETED_FILE="$T/deleted.txt" "$@" bash -c "set -euo pipefail; $(block)
        for v in \$PRINT; do echo \"\$v=\${!v}\"; done" 2>&1
}

out=$(PRINT="C3X_IP" run C3X_IP=10.1.2.3 PRINT=C3X_IP) ; rc=$?
check "the address comes from C3X_IP" test "$out" = "C3X_IP=10.1.2.3"

out=$(run PRINT=C3X_IP); rc=$?
check "without C3X_IP the script stops" test $rc -ne 0
check "and says what to set" bash -c "echo '$out' | grep -q 'set C3X_IP to the address of the device'"

out=$(run C3X_IP=10.1.2.3 PRINT="C3X_USER SSH_KEY KNOWN_HOSTS REMOTE_DRIVES LOCAL_RAW LOCAL_STITCHED LOG_FILE MIN_FREE_GB")
expected=$(printf '%s\n' C3X_USER=comma SSH_KEY=/keys/openpilot_sync_ed25519 KNOWN_HOSTS=/app/ssh/known_hosts REMOTE_DRIVES=/data/media/0/realdata LOCAL_RAW=/data/raw LOCAL_STITCHED=/data/stitched LOG_FILE=/data/logs/c3x_sync.log MIN_FREE_GB=75)
check "the other settings default to the container's values (what was hard-coded before)" test "$out" = "$expected"

out=$(run C3X_IP=10.1.2.3 C3X_USER=pilot MIN_FREE_GB=200 LOCAL_RAW=/srv/raw PRINT="C3X_USER MIN_FREE_GB LOCAL_RAW LOCAL_STITCHED")
check "settings can be overridden" test "$out" = "$(printf '%s\n' C3X_USER=pilot MIN_FREE_GB=200 LOCAL_RAW=/srv/raw LOCAL_STITCHED=/data/stitched)"

out=$(run C3X_IP=10.1.2.3 PRINT=SSH_OPTS)
check "ssh options use the key and known_hosts settings" test "$out" = "SSH_OPTS=-i /keys/openpilot_sync_ed25519 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/app/ssh/known_hosts -o BatchMode=yes"

check "no device address is left in the script" bash -c "! grep -nE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' '$HERE/device_sync.sh'"

echo; echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
