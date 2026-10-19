#!/bin/sh
# Run the sync every 5 minutes, never overlapping (same as the README's flock cron).
while true; do
  flock -n /tmp/c3x_sync.lock /app/c3x_sync.sh || echo "$(date '+%F %T') sync run exited $?"
  sleep 300
done
