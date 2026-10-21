# The image `lettucectl backup` copies a stack with. The state is root-owned, so
# it is read as root here, in a container with no network and read-only mounts
# (cmd_backup), instead of with sudo on the host. tar and gzip come with the base.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync \
 && rm -rf /var/lib/apt/lists/*
