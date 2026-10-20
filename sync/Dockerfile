FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg rsync openssh-client iputils-ping util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# uid/gid 1000 matches the CIFS mount owner and the compose user, and gives ssh a passwd entry.
RUN G=$(getent group 1000 | cut -d: -f1); [ -n "$G" ] || { groupadd -g 1000 opsync; G=opsync; }; getent passwd 1000 >/dev/null || useradd -u 1000 -g "$G" -d /home/opsync -m opsync
ENV HOME=/home/opsync
WORKDIR /app
COPY device_sync.sh delete_lib.sh fix_video_tags.sh entrypoint.sh ./
RUN chmod +x /app/device_sync.sh /app/fix_video_tags.sh /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
