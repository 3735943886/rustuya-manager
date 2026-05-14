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

# pyrustuyabridge ships a manylinux wheel that bundles the Rust binary,
# so the runtime image needs no compilers — just the Python deps.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Non-root runtime user. `/data` is the persistent mount point —
# `tuyadevices.json`, `tuyacreds.json`, and the embedded bridge's
# `bridge-state.json` all live here so a container restart preserves
# the cloud cache, wizard credentials, and the bridge's view of what's
# running.
RUN useradd --create-home --shell /usr/sbin/nologin manager \
    && mkdir -p /data \
    && chown manager:manager /data
USER manager
WORKDIR /data

# Defaults tuned for the container-first persona:
#   * `0.0.0.0` because reaching the UI from the host requires a
#     published port — loopback inside the container is useless.
#   * `mqtt://localhost:1883` is a placeholder; almost every real
#     deploy will override BROKER to point at a sibling container.
ENV HOST=0.0.0.0 \
    PORT=8373 \
    BROKER=mqtt://localhost:1883 \
    ROOT=rustuya

EXPOSE 8373

# Shell-form CMD so the `${VAR:+--flag $VAR}` pattern works — optional
# flags only appear when their env var is non-empty, keeping the command
# usable without extra config. `exec` hands PID 1 to the python process
# so SIGTERM from `docker stop` propagates straight to uvicorn / the
# embedded bridge instead of being swallowed by /bin/sh.
CMD exec rustuya-manager \
    --web \
    --embed-bridge \
    --host "$HOST" \
    --port "$PORT" \
    --broker "$BROKER" \
    --root "$ROOT" \
    ${AUTH:+--auth "$AUTH"} \
    ${CLOUD:+--cloud "$CLOUD"} \
    ${BRIDGE_CONFIG:+--bridge-config "$BRIDGE_CONFIG"} \
    ${BRIDGE_STATE:+--bridge-state "$BRIDGE_STATE"}
