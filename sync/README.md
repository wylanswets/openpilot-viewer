# Sync container

Copies drives from the device (a comma 3, 3X or 4) to the server every 5 minutes (`entrypoint.sh`
runs `device_sync.sh` under `flock`, so runs never overlap) and stitches the videos.

## Settings

`device_sync.sh` reads its settings from environment variables, so the repository does not
need to contain the address of any particular device. Only `DEVICE_IP` is required; the
others default to the container's mounts.

| Variable | Default | Meaning |
|---|---|---|
| `DEVICE_IP` | none, required | address of the device |
| `DEVICE_USER` | `comma` | ssh user on the device |
| `SSH_KEY` | `/keys/openpilot_sync_ed25519` | private key for that user |
| `KNOWN_HOSTS` | `/app/ssh/known_hosts` | where ssh records the device's host key |
| `REMOTE_DRIVES` | `/data/media/0/realdata` | drives folder on the device |
| `LOCAL_RAW` | `/data/raw` | where raw segments are copied |
| `LOCAL_STITCHED` | `/data/stitched` | where stitched videos go |
| `LOG_FILE` | `/data/logs/device_sync.log` | log |
| `DELETED_FILE` | `/data/logs/deleted_routes.txt` | routes that were deleted here and must not be fetched again |
| `METADATA_DIR` | `/data/metadata` | the viewer's folder: its star list and the delete requests the sync carries out |
| `MIN_FREE_GB` | `75` | free space below which the oldest drives are removed |

With docker compose, put the address in the `.env` file next to the compose file and pass it on:

```
# .env
DEVICE_IP=192.168.1.50
```

```yaml
# compose.yaml
  sync:
    build: ./sync
    environment:
      DEVICE_IP: ${DEVICE_IP}
```

## Videos that play on iPhones

Stitched videos are written with `-tag:v hvc1 -movflags +faststart` (stream copy, no re-encoding): Safari
and iPhones only play HEVC in an MP4 tagged `hvc1`, and the index at the front lets playback start
sooner. Videos stitched before this change are tagged `hev1`; `fix_video_tags.sh` repackages them the
same way, without re-encoding. It writes each file next to the original, checks it (tag, duration, size),
and only then replaces the original; it is safe to run again and skips routes that are not finished.

```
docker exec openpilot-sync /app/fix_video_tags.sh --dry-run      # list what it would do
docker exec -d openpilot-sync /app/fix_video_tags.sh             # do it in the background
tail -f <logs folder>/fix_video_tags.log
```

Tests: `bash sync/test_video_tags.sh` (the video part needs ffmpeg with libx265, as in the sync image).

## Deleting drives

The viewer's Delete button does not delete anything itself: it creates an empty file named after
the drive in `$METADATA_DIR/delete_requests/`. At the start of every run, before it checks that the
device is reachable, the script (`delete_lib.sh`) deletes those drives, records them in
`DELETED_FILE` so they are not fetched again, and removes the request. Drives starred in the viewer
(`$METADATA_DIR/preserved_routes.json`) or with a `.preserve` file are never deleted, by a request or by
the free-space cleanup. The device's own copy is never touched. Tests: `bash sync/test_delete_lib.sh`.

Without `DEVICE_IP` the script stops straight away with `set DEVICE_IP to the address of the device`.

## How the logs are compared

The log pass compares file **sizes** only (`--size-only --modify-window=1`, `RSYNC_LOGS_COMPARE` in the
script). The device keeps times to the nanosecond and a NAS share often only to 100 ns, so with times
compared rsync sees every log as changed and rewrites it on every run. A log that is still growing differs
in size, so it is still picked up. Test: `bash sync/test_rsync_options.sh`.

## Deleting drives