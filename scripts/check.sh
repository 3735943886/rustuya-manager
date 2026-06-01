#!/usr/bin/env bash
# check.sh — pre-install environment doctor for rustuya-manager.
#
# Pure read-only diagnostic: probes the host for the things rustuya-manager
# needs (Python, an install path, a broker, optionally a bridge) and prints
# a recommendation for how to deploy. Never modifies the system, never
# prompts, always exits 0 — designed to be safe under `curl | bash` so a
# new user can decide whether to `pipx install`, `docker run`, or something
# else *before* committing to any of them.
#
# Usage:
#   ./check.sh                              # full report
#   ./check.sh --quiet                      # only summary + recommendation
#   ./check.sh --broker mqtt://host:1883    # probe a non-localhost broker
#   ./check.sh --port 8080                  # probe a non-default web port
#   ./check.sh --help
#
# Remote one-liner (no install required):
#   curl -fsSL https://raw.githubusercontent.com/3735943886/rustuya-manager/master/scripts/check.sh | bash

set -uo pipefail
# Intentionally no `set -e` — every probe is allowed to "fail" (that's a
# signal, not an error). The script's contract is "always exit 0 with a
# verdict", which only holds if individual probe failures don't abort.

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_BROKER_HOST="127.0.0.1"
DEFAULT_BROKER_PORT="1883"
DEFAULT_WEB_PORT="8373"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

BROKER_HOST="$DEFAULT_BROKER_HOST"
BROKER_PORT="$DEFAULT_BROKER_PORT"
WEB_PORT="$DEFAULT_WEB_PORT"
QUIET=0

# ── Logging helpers ────────────────────────────────────────────────────────────
# TTY-conditional color so `curl | bash` (no TTY on stdout) gets plain text
# that pipes cleanly into terminals, log files, and CI captures alike.
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""; C_BOLD=""; C_RESET=""
fi

# Track verdict counts so the summary can lead with "X warnings, Y errors".
OK_COUNT=0
WARN_COUNT=0
ERR_COUNT=0

# Recommendation inputs — set by individual probes, consumed by the summary.
HAS_PIPX=0
HAS_PIP=0
HAS_DOCKER=0
HAS_SYSTEMD=0
HAS_PYTHON_OK=0
HAS_BROKER=0
HAS_BRIDGE_RUNNING=0   # systemd or docker
HAS_BRIDGE_BINARY=0    # `which rustuya-bridge` only
HAS_MANAGER_INSTALLED=0
HAS_MANAGER_RUNNING_DOCKER=0
MANAGER_DOCKER_NAME=""
MANAGER_DOCKER_IMAGE=""
PORT_FREE=1

# row() formats a single check as "  STATUS  message".
# STATUS = ✓ (ok), ⚠ (warn), ✗ (err). Plain ASCII fallback is "OK/WARN/ERR"
# when stdout isn't a TTY so log files / CI captures stay grep-friendly.
row() {
    local kind="$1" msg="$2" detail="${3:-}"
    [ "$QUIET" -eq 1 ] && return 0
    local marker
    if [ -t 1 ]; then
        case "$kind" in
            ok)   marker="${C_GREEN}✓${C_RESET}" ;;
            warn) marker="${C_YELLOW}⚠${C_RESET}" ;;
            err)  marker="${C_RED}✗${C_RESET}" ;;
            *)    marker=" " ;;
        esac
    else
        case "$kind" in
            ok)   marker="OK  " ;;
            warn) marker="WARN" ;;
            err)  marker="ERR " ;;
            *)    marker="    " ;;
        esac
    fi
    if [ -n "$detail" ]; then
        printf '  %s  %s  %s%s%s\n' "$marker" "$msg" "$C_DIM" "$detail" "$C_RESET"
    else
        printf '  %s  %s\n' "$marker" "$msg"
    fi
}

# Wrappers around row() that also tally for the summary.
ok()   { OK_COUNT=$((OK_COUNT+1));   row ok   "$@"; }
warn() { WARN_COUNT=$((WARN_COUNT+1)); row warn "$@"; }
err()  { ERR_COUNT=$((ERR_COUNT+1));  row err  "$@"; }

section() {
    [ "$QUIET" -eq 1 ] && return 0
    printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_RESET"
}

# ── TCP probe (no external dep — uses bash's built-in /dev/tcp) ────────────────
# bash opens /dev/tcp/host/port as a network socket; closing it immediately
# is enough to answer "is anything listening here?" without sending bytes.
# Falls back to `nc -z` if /dev/tcp isn't supported (rare — only some
# minimal alpine images strip it).
tcp_reachable() {
    local host="$1" port="$2"
    if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
        exec 3<&-; exec 3>&-
        return 0
    fi
    if command -v nc >/dev/null 2>&1; then
        nc -z -w 1 "$host" "$port" 2>/dev/null && return 0
    fi
    return 1
}

# ── Probes ─────────────────────────────────────────────────────────────────────
check_runtime() {
    section "Runtime"
    local os arch
    os="$(uname -s 2>/dev/null || echo unknown)"
    arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$os" in
        Linux)  ok "OS: Linux ($arch)" ;;
        Darwin) warn "OS: macOS ($arch)" "rustuya-manager runs but systemd suggestions won't apply" ;;
        *)      warn "OS: $os ($arch)" "untested platform" ;;
    esac

    if command -v systemctl >/dev/null 2>&1; then
        HAS_SYSTEMD=1
        ok "systemd available" "for optional service unit"
    else
        warn "systemd not available" "auto-start at boot won't be a one-step option"
    fi
}

check_python() {
    section "Python"
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found" "rustuya-manager requires Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+"
        return
    fi
    local pyver major minor
    pyver="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo unknown)"
    major="$(python3 -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)"
    minor="$(python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)"
    if [ "$major" -gt "$MIN_PYTHON_MAJOR" ] || \
       { [ "$major" -eq "$MIN_PYTHON_MAJOR" ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; }; then
        HAS_PYTHON_OK=1
        ok "Python $pyver" "≥ ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} required"
    else
        err "Python $pyver too old" "need ≥ ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}"
    fi

    if command -v pipx >/dev/null 2>&1; then
        HAS_PIPX=1
        ok "pipx installed" "$(pipx --version 2>/dev/null | head -n1)"
    else
        warn "pipx not installed" "recommended install method on most distros"
    fi

    if command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
        HAS_PIP=1
        ok "pip available" "fallback if pipx not installed"
    else
        warn "pip not found" ""
    fi

    # Already-installed signal. Useful when re-running check.sh on a host
    # that already has the manager — confirms which version is active.
    # The CLI has no --version flag (intentionally — adding one would mean
    # touching the package), so we read __version__ via the interpreter
    # the entry point's shebang already points at. With pipx the manager
    # lives in its own venv, so system python3 may not have it importable —
    # going through the shebang gets us the right interpreter in all cases.
    if command -v rustuya-manager >/dev/null 2>&1; then
        HAS_MANAGER_INSTALLED=1
        local mver entry_py
        entry_py="$(head -n1 "$(command -v rustuya-manager)" 2>/dev/null | sed -e 's|^#!||' -e 's| .*||')"
        if [ -x "$entry_py" ]; then
            mver="$("$entry_py" -c 'import rustuya_manager; print(rustuya_manager.__version__)' 2>/dev/null || echo unknown)"
        else
            mver="unknown"
        fi
        ok "rustuya-manager already installed" "$mver"
    fi
}

check_docker() {
    section "Container tooling"
    if ! command -v docker >/dev/null 2>&1; then
        warn "docker not installed" "alternative install path unavailable"
        return
    fi
    if docker info >/dev/null 2>&1; then
        HAS_DOCKER=1
        ok "docker installed and reachable" "$(docker --version 2>/dev/null)"
    else
        warn "docker installed but daemon unreachable" "may need 'sudo systemctl start docker' or group membership"
    fi
}

check_manager_running() {
    # Detect a rustuya-manager container already up so subsequent checks /
    # recommendations don't treat the host as a fresh install. Symmetric to
    # the rustuya-bridge docker probe below; image-name match so renamed
    # containers still get caught.
    [ "$HAS_DOCKER" -eq 1 ] || return 0
    local line name image status rest
    line="$(docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null \
        | awk -F'|' '$2 ~ /(^|\/)rustuya-manager(:|$)/' \
        | head -n1)"
    [ -z "$line" ] && return 0
    HAS_MANAGER_RUNNING_DOCKER=1
    name="${line%%|*}"
    rest="${line#*|}"
    image="${rest%%|*}"
    status="${rest#*|}"
    MANAGER_DOCKER_NAME="$name"
    MANAGER_DOCKER_IMAGE="$image"
    section "rustuya-manager"
    ok "rustuya-manager running in docker" "$image — container: $name ($status)"
}

check_broker() {
    section "MQTT broker"
    if tcp_reachable "$BROKER_HOST" "$BROKER_PORT"; then
        HAS_BROKER=1
        ok "broker reachable" "${BROKER_HOST}:${BROKER_PORT}"
    else
        warn "no broker at ${BROKER_HOST}:${BROKER_PORT}" "install mosquitto, or point manager at a remote broker via config.json"
    fi
}

check_bridge() {
    section "rustuya-bridge"
    local found=""

    # systemd-managed bridge takes priority — the user clearly intends an
    # external bridge service and the manager should NOT embed.
    if [ "$HAS_SYSTEMD" -eq 1 ] && systemctl is-active --quiet rustuya-bridge 2>/dev/null; then
        HAS_BRIDGE_RUNNING=1
        found="systemd"
        ok "rustuya-bridge active via systemd" "external mode — don't pass --embed-bridge"
    fi

    # Docker-managed bridge: same intent as systemd, second priority. We
    # match on image name rather than container name because users rename
    # containers freely.
    if [ -z "$found" ] && [ "$HAS_DOCKER" -eq 1 ]; then
        if docker ps --format '{{.Image}}' 2>/dev/null | grep -q -E '(^|/)rustuya-bridge(:|$)'; then
            HAS_BRIDGE_RUNNING=1
            found="docker"
            ok "rustuya-bridge running in docker" "external mode — don't pass --embed-bridge"
        fi
    fi

    # Bare binary present but not actively managed — could go either way,
    # so we report it without making a strong recommendation.
    if [ -z "$found" ] && command -v rustuya-bridge >/dev/null 2>&1; then
        HAS_BRIDGE_BINARY=1
        found="binary"
        local bver
        bver="$(rustuya-bridge --version 2>/dev/null | head -n1 || echo unknown)"
        warn "rustuya-bridge binary present but not running" "$bver — embed via --embed-bridge, or start it separately"
    fi

    if [ -z "$found" ]; then
        if [ "$HAS_MANAGER_RUNNING_DOCKER" -eq 1 ]; then
            # Can't see inside the container; the image's EMBED_BRIDGE
            # default is on, so the bridge is likely embedded there.
            ok "no external bridge visible" "running manager likely has --embed-bridge on (image default)"
        else
            ok "no external bridge detected" "manager can run with --embed-bridge"
        fi
    fi
}

check_port() {
    section "Networking"
    if tcp_reachable "127.0.0.1" "$WEB_PORT"; then
        if [ "$HAS_MANAGER_RUNNING_DOCKER" -eq 1 ]; then
            # The running manager container almost certainly holds this
            # port (host-networking is the documented setup). Don't flag
            # it as a problem — that would scare a user into changing
            # --port for a phantom conflict.
            ok "port ${WEB_PORT} in use" "expected — rustuya-manager container detected above"
        else
            PORT_FREE=0
            warn "port ${WEB_PORT} is in use" "pass --port to rustuya-manager (default ${DEFAULT_WEB_PORT})"
        fi
    else
        ok "port ${WEB_PORT} is free" "default web UI port"
    fi
}

# ── Recommendation ─────────────────────────────────────────────────────────────
# Inject `-e EMBED_BRIDGE=0` into the recommended docker command only when
# an external bridge is already running (so the image's default-on embed
# doesn't double-publish on the broker). Two flavors:
#   docker_embed_env       — inline form, for the single-line alt suggestion
#   docker_embed_env_cont  — continuation form, for the multi-line `\` block
# Both render empty when no external bridge was detected (image default is
# correct as-is).
docker_embed_env() {
    [ "$HAS_BRIDGE_RUNNING" -eq 1 ] && printf ' -e EMBED_BRIDGE=0' || true
}
docker_embed_env_cont() {
    [ "$HAS_BRIDGE_RUNNING" -eq 1 ] && printf '\n    -e EMBED_BRIDGE=0 \\' || true
}

print_recommendation() {
    printf '\n%sSummary%s  %d ok, %d warn, %d err\n' \
        "$C_BOLD" "$C_RESET" "$OK_COUNT" "$WARN_COUNT" "$ERR_COUNT"

    if [ "$HAS_PYTHON_OK" -eq 0 ] && [ "$HAS_DOCKER" -eq 0 ]; then
        printf '\n%sCannot install yet.%s Install one of:\n' "$C_RED" "$C_RESET"
        printf '  • Python ≥ %d.%d (preferred for pipx install)\n' "$MIN_PYTHON_MAJOR" "$MIN_PYTHON_MINOR"
        printf '  • Docker (run as a container instead)\n'
        return
    fi

    # Already-running-in-docker branch wins over every install path —
    # recommending a fresh install while the manager is up would lead the
    # user into a duplicate deploy (port collision, broker contention).
    # Lead with the upgrade pattern: pull + recreate with original flags.
    if [ "$HAS_MANAGER_RUNNING_DOCKER" -eq 1 ]; then
        local image_base="${MANAGER_DOCKER_IMAGE%:*}"
        local image_tag="${MANAGER_DOCKER_IMAGE##*:}"
        # No-tag case ("image" without ":tag") leaves image_base == image_tag;
        # surface that explicitly rather than printing the image twice.
        [ "$image_base" = "$MANAGER_DOCKER_IMAGE" ] && image_tag="latest"
        printf '\n%sAlready running in docker%s — %s\n' \
            "$C_BOLD" "$C_RESET" "$MANAGER_DOCKER_IMAGE"
        printf '  Container: %s\n' "$MANAGER_DOCKER_NAME"
        printf '  To upgrade — pull a newer image, then recreate with your original flags:\n'
        printf '    docker pull %s:latest\n' "$image_base"
        printf '    %s# then re-create (docker compose up -d, or your stop+rm+run script).%s\n' \
            "$C_DIM" "$C_RESET"
        printf '  %sCurrently pinned to: %s%s\n' "$C_DIM" "$image_tag" "$C_RESET"
        if [ "$HAS_MANAGER_INSTALLED" -eq 1 ]; then
            # Rare both-paths case (migration in progress, debug install,
            # etc.). The docker container is the live service; mention the
            # pipx side so the user can decide whether to clean it up.
            printf '  %s(Also installed via pipx — `pipx upgrade rustuya-manager` if you keep that path.)%s\n' \
                "$C_DIM" "$C_RESET"
        fi
    elif [ "$HAS_MANAGER_INSTALLED" -eq 1 ]; then
        printf '\n%sAlready installed%s — to update:\n' "$C_BOLD" "$C_RESET"
        if [ "$HAS_PIPX" -eq 1 ]; then
            printf '  pipx upgrade rustuya-manager\n'
            printf '  %s(or: pipx reinstall rustuya-manager — rebuilds the venv from scratch)%s\n' "$C_DIM" "$C_RESET"
        else
            printf '  Update with whichever tool you originally used (pip, system pkg mgr, docker).\n'
        fi
    elif [ "$HAS_PIPX" -eq 1 ]; then
        printf '\n%sRecommended install%s\n' "$C_BOLD" "$C_RESET"
        printf '  pipx install rustuya-manager\n'
        if [ "$HAS_DOCKER" -eq 1 ]; then
            printf '  %sAlternatively (docker): docker run -d --network host -v /var/lib/rustuya-manager:/data%s%s 3735943886/rustuya-manager:latest%s\n' \
                "$C_DIM" "$(docker_embed_env)" "$C_DIM" "$C_RESET"
        fi
    elif [ "$HAS_PYTHON_OK" -eq 1 ] && [ "$HAS_PIP" -eq 1 ]; then
        printf '\n%sRecommended install%s\n' "$C_BOLD" "$C_RESET"
        printf '  python3 -m pip install --user pipx && python3 -m pipx ensurepath\n'
        printf '  pipx install rustuya-manager\n'
        printf '  %s(pipx is the cleanest install — pip-managed apps in isolated venvs)%s\n' "$C_DIM" "$C_RESET"
        if [ "$HAS_DOCKER" -eq 1 ]; then
            printf '  %sAlternatively (docker): docker run -d --network host -v /var/lib/rustuya-manager:/data%s%s 3735943886/rustuya-manager:latest%s\n' \
                "$C_DIM" "$(docker_embed_env)" "$C_DIM" "$C_RESET"
        fi
    elif [ "$HAS_PYTHON_OK" -eq 1 ] && [ "$HAS_DOCKER" -eq 0 ]; then
        # Bug #1 fix: Python is here but no pip/pipx → bootstrap branch.
        # Common on a fresh Ubuntu where python3 ships but pip/pipx are
        # separate packages. Don't try to guess the distro definitively;
        # list the three most common package managers and let the user
        # pick the line that matches their box.
        printf '\n%sBootstrap your install path first%s\n' "$C_BOLD" "$C_RESET"
        printf '  Python is here but neither pipx nor pip is installed.\n'
        printf '  Install one of them with your package manager, then re-run this check:\n'
        printf '    Debian/Ubuntu:  %ssudo apt install pipx%s\n' "$C_BOLD" "$C_RESET"
        printf '    Fedora/RHEL:    %ssudo dnf install pipx%s\n' "$C_BOLD" "$C_RESET"
        printf '    Arch:           %ssudo pacman -S python-pipx%s\n' "$C_BOLD" "$C_RESET"
    elif [ "$HAS_DOCKER" -eq 1 ]; then
        printf '\n%sRecommended install%s\n' "$C_BOLD" "$C_RESET"
        printf '  docker run -d --name rustuya-manager --network host \\\n'
        printf '    -v /var/lib/rustuya-manager:/data \\%s\n' "$(docker_embed_env_cont)"
        printf '    3735943886/rustuya-manager:latest\n'
        printf '  %s(no Python on this host; docker is the only available path)%s\n' "$C_DIM" "$C_RESET"
    fi

    printf '\n%sFlags to consider%s\n' "$C_BOLD" "$C_RESET"
    if [ "$HAS_MANAGER_RUNNING_DOCKER" -eq 1 ]; then
        # The new-install flag suggestions don't apply when there's already
        # a manager up — the running container has its own flag set and we
        # can't see inside. Point the user at logs instead so they can
        # verify what's actually configured.
        printf '  • Manager already running — install/upgrade flags apply to a re-deploy only.\n'
        printf '    Inspect: %sdocker logs %s%s\n' "$C_BOLD" "$MANAGER_DOCKER_NAME" "$C_RESET"
        if [ "$HAS_BROKER" -eq 0 ]; then
            # Broker still matters: the running container may already be
            # disconnected, which is a real-world problem worth surfacing.
            printf '  • No broker at %s:%s — the running container may already be disconnected;\n' \
                "$BROKER_HOST" "$BROKER_PORT"
            printf '    install a local one (%ssudo apt install mosquitto%s) or fix the network path.\n' \
                "$C_BOLD" "$C_RESET"
        fi
    else
        if [ "$HAS_BRIDGE_RUNNING" -eq 1 ]; then
            # The "how to opt out of embed" depends on which install path
            # the user picked, so spell both out — pipx omits the flag,
            # docker passes the env var (image entrypoint reads it).
            printf '  • Bridge already managed externally — avoid double-publishing:\n'
            printf '      pipx path:    omit --embed-bridge from the manager command\n'
            printf '      docker path:  add %s-e EMBED_BRIDGE=0%s to docker run\n' "$C_BOLD" "$C_RESET"
        elif [ "$HAS_BRIDGE_BINARY" -eq 1 ]; then
            printf '  • rustuya-bridge binary present but idle — start it as a service, OR\n'
            printf '    let the manager embed it with --embed-bridge (docker: default is on).\n'
        else
            printf '  • No bridge found — pass --embed-bridge (docker: default is on).\n'
        fi
        if [ "$HAS_BROKER" -eq 0 ]; then
            printf '  • No broker at %s:%s — install a local one (Debian/Ubuntu: %ssudo apt install mosquitto%s)\n' \
                "$BROKER_HOST" "$BROKER_PORT" "$C_BOLD" "$C_RESET"
            printf '    or point the manager at a remote broker via config.json.\n'
        fi
        if [ "$PORT_FREE" -eq 0 ]; then
            printf '  • Port %s is taken — start the manager with --port <free_port>.\n' "$WEB_PORT"
        fi
    fi
    if [ "$HAS_SYSTEMD" -eq 1 ] \
       && [ "$HAS_MANAGER_INSTALLED" -eq 0 ] \
       && [ "$HAS_MANAGER_RUNNING_DOCKER" -eq 0 ]; then
        printf '\n%sOptional systemd unit (after install)%s — see the README for a template.\n' "$C_DIM" "$C_RESET"
    fi
}

# ── Help ───────────────────────────────────────────────────────────────────────
cmd_help() {
    cat <<EOF
check.sh — pre-install environment doctor for rustuya-manager

Usage:
  $(basename "$0") [options]

Options:
  --broker HOST:PORT   Probe this broker instead of ${DEFAULT_BROKER_HOST}:${DEFAULT_BROKER_PORT}.
  --port N             Check whether this port is free (default ${DEFAULT_WEB_PORT}).
  --quiet              Suppress per-check rows; print only the summary + recommendation.
  -h, --help           Show this message.

Pure read-only — never modifies the system. Always exits 0.
EOF
}

# ── Argument parsing ───────────────────────────────────────────────────────────
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --broker)
                [ $# -lt 2 ] && { err "--broker needs HOST:PORT" ""; exit 0; }
                shift
                BROKER_HOST="${1%:*}"
                BROKER_PORT="${1##*:}"
                # `host` and `host:1883` both legal — fall back to default
                # port when the user didn't supply one (no `:` in the arg).
                if [ "$BROKER_HOST" = "$BROKER_PORT" ]; then
                    BROKER_PORT="$DEFAULT_BROKER_PORT"
                fi
                ;;
            --port)
                [ $# -lt 2 ] && { err "--port needs a number" ""; exit 0; }
                shift
                WEB_PORT="$1"
                ;;
            --quiet|-q) QUIET=1 ;;
            -h|--help)  cmd_help; exit 0 ;;
            *)          err "Unknown option: $1" "use --help"; exit 0 ;;
        esac
        shift
    done
}

main() {
    parse_args "$@"
    if [ "$QUIET" -eq 0 ]; then
        printf '%srustuya-manager environment check%s\n' "$C_BOLD" "$C_RESET"
        printf '%s(read-only diagnostic — nothing on this host will be modified)%s\n' "$C_DIM" "$C_RESET"
    fi
    check_runtime
    check_python
    check_docker
    check_manager_running
    check_broker
    check_bridge
    check_port
    print_recommendation
    exit 0
}

main "$@"
