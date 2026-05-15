#!/bin/sh
# Step down from root to the `manager` user, renumbering it to the
# requested PUID/PGID first so bind-mounted /data directories owned by
# any host UID stay writable. Inspired by the LinuxServer.io convention
# (PUID/PGID env vars), trimmed down: no s6-overlay, no custom init —
# just usermod + chown + exec.
#
# Two entry paths:
#   1. Container started as root (the default): renumber `manager`,
#      chown /data, re-exec this script under `gosu manager`.
#   2. Container started with `--user UID:GID` (already non-root):
#      trust the caller, skip the privilege juggling, just exec.
set -e

if [ "$(id -u)" = "0" ]; then
    PUID=${PUID:-1000}
    PGID=${PGID:-1000}

    # `usermod -o` / `groupmod -o` allow non-unique IDs; harmless on
    # fresh images, important if PUID/PGID collide with an existing
    # entry (e.g. PUID=0 would clash with root).
    groupmod -o -g "$PGID" manager
    usermod  -o -u "$PUID" manager

    # /data may have arrived via bind-mount with arbitrary ownership.
    # Chown so the (possibly renumbered) manager can read+write. -R is
    # fine here — /data holds a handful of small JSON files, not a
    # media library.
    chown -R manager:manager /data

    echo "rustuya-manager: running as manager (uid=$PUID gid=$PGID)"
    exec gosu manager "$0" "$@"
fi

# Non-root path. Either we just re-exec'd ourselves via gosu, or the
# caller passed `docker run --user`. Either way, run the app.
exec rustuya-manager \
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
