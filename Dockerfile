# syntax=docker/dockerfile:1.7
#
# rustuya-manager — single-container deploy targeting light /
# container-first users (HA OS, unraid, CasaOS, …). Distinct from the
# pipx + systemd track documented in the README: this image runs the
# manager with `--embed-bridge` so manager + bridge live in one process,
# and only an external MQTT broker is required from the host side.
#
# Multi-stage build keeps the runtime image lean — the wheel is built
# under `python -m build` in the `builder` stage, then only the wheel
# (and its installed deps) lands in the final stage.

FROM python:3.12-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml ./
COPY src/ src/
RUN python -m build --wheel


FROM python:3.12-slim

# gosu is a tiny (~1.2MB) setuid-safe replacement for `su` / `sudo`,
# used by the entrypoint to drop from root to the `manager` user once
# UID/GID renumbering and /data chown are done.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# pyrustuyabridge ships a manylinux wheel that bundles the Rust binary,
# so the runtime image needs no compilers — just the Python deps.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Pre-create the manager user. The actual UID/GID is reset by the
# entrypoint from PUID/PGID env vars so that bind-mounts owned by any
# host UID work without pre-chowning the directory; the user name is
# the only stable handle.
RUN useradd --create-home --shell /usr/sbin/nologin manager \
    && mkdir -p /data /data/plugins

WORKDIR /data

# Defaults tuned for the container-first persona:
#   * `0.0.0.0` because reaching the UI from the host requires a
#     published port — loopback inside the container is useless.
#   * BROKER / ROOT / BRIDGE_STATE intentionally have NO Dockerfile ENV
#     default. The entrypoint passes the corresponding CLI flag only
#     when the env var is set, so an unset env lets manager's own
#     default (or, when --bridge-config is in play, the value from that
#     bridge-config file) become the source of truth. The alternative —
#     baking the manager default into the Dockerfile ENV — masks user
#     edits to /data/config.json because the resulting always-present
#     CLI flag would always look "user-set" to the manager (CLI > config).
#   * CLOUD / BRIDGE_CONFIG / HOST keep their Dockerfile ENV defaults
#     because the docker persona wants paths under /data and a non-
#     loopback bind, which differ from manager defaults.
#   * PLUGIN_DIR defaults to /data/plugins so drop-in plugins "just work":
#     mount a folder there and restart. Empty/absent dir loads nothing.
#     Loading code from this dir runs it in-process, so only mount plugins
#     you trust (same trust as installing a package).
#   * PUID/PGID default to 1000 which matches the first non-root user
#     on most desktop / Pi / Armbian installs. Override with
#     `-e PUID=$(id -u) -e PGID=$(id -g)` for hosts where the data
#     directory is owned by a different UID (NAS, HA OS, etc.).
#   * EMBED_BRIDGE=1 keeps the single-process default that defines this
#     image's persona; set `-e EMBED_BRIDGE=0` when an external
#     rustuya-bridge (separate systemd service / sibling container) is
#     already broadcasting on the same MQTT broker, so the container
#     doesn't double-publish.
ENV HOST=0.0.0.0 \
    PORT=8373 \
    CLOUD=/data/tuyadevices.json \
    BRIDGE_CONFIG=/data/config.json \
    PLUGIN_DIR=/data/plugins \
    PUID=1000 \
    PGID=1000 \
    EMBED_BRIDGE=1

EXPOSE 8373

# Entrypoint starts as root, renumbers manager to PUID/PGID, chowns
# /data, then re-execs itself under `gosu manager` so the actual app
# runs unprivileged. See docker-entrypoint.sh for details.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
