# Sync container

Copies drives from the device (comma 3/3X) to the server every 5 minutes (`entrypoint.sh`
runs `device_sync.sh` under `flock`, so runs never overlap) and stitches the videos.

## Settings

`device_sync.sh` reads its settings from environment variables, so the repository does not
need to contain the address of any particular device. Only `C3X_IP` is required; the
others default to the container's mounts.

| Variable | Default | Meaning |
|---|---|---|
| `C3X_IP` | none, required | address of the device |
| `C3X_USER` | `comma` | ssh user on the device |
| `SSH_KEY` | `/keys/openpilot_sync_ed25519` | private key for that user |
| `KNOWN_HOSTS` | `/app/ssh/known_hosts` | where ssh records the device's host key |
| `REMOTE_DRIVES` | `/data/media/0/realdata` | drives folder on the device |
| `LOCAL_RAW` | `/data/raw` | where raw segments are copied |
| `LOCAL_STITCHED` | `/data/stitched` | where stitched videos go |
| `LOG_FILE` | `/data/logs/c3x_sync.log` | log |
| `DELETED_FILE` | `/data/logs/deleted_routes.txt` | routes that were deleted here and must not be fetched again |
| `MIN_FREE_GB` | `75` | free space below which the oldest drives are removed |

With docker compose, put the address in the `.env` file next to the compose file and pass it on:

```
# .env
C3X_IP=192.168.1.50
```

```yaml
# compose.yaml
  sync:
    build: ./sync
    environment:
      C3X_IP: ${C3X_IP}
```

Without `C3X_IP` the script stops straight away with `set C3X_IP to the address of the device`.
