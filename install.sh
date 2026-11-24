#!/bin/sh
# Installs (or upgrades) Docker Monitor on macOS and Linux, then puts `docker-monitor` on your PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/GD-HCK/docker-monitor/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/GD-HCK/docker-monitor/main/install.sh | sh -s -- --uninstall
#
# It uses uv if you have it, else pipx, else a private venv in ~/.local/lib/docker-monitor
# with a shim in ~/.local/bin. Nothing is installed system-wide and sudo is never needed.
set -eu

REPO='GD-HCK/docker-monitor'
REF="${DOCKER_MONITOR_REF:-main}"
SOURCE="${DOCKER_MONITOR_SOURCE:-https://github.com/$REPO/archive/$REF.zip}"
DATA_DIR="$HOME/.local/lib/docker-monitor"
BIN_DIR="$HOME/.local/bin"

say() { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

uninstall=false
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=true ;;
        -h | --help) echo 'Usage: install.sh [--uninstall]'; exit 0 ;;
        *) die "Unknown option: $arg (only --uninstall is supported)" ;;
    esac
done

if [ "$uninstall" = true ]; then
    command -v uv >/dev/null 2>&1 && uv tool uninstall docker-monitor >/dev/null 2>&1 || true
    command -v pipx >/dev/null 2>&1 && pipx uninstall docker-monitor >/dev/null 2>&1 || true
    rm -rf "$DATA_DIR"
    [ -L "$BIN_DIR/docker-monitor" ] && rm -f "$BIN_DIR/docker-monitor"
    say 'Docker Monitor uninstalled.'
    exit 0
fi

command -v docker >/dev/null 2>&1 || warn 'Heads up: the docker CLI is not on your PATH; Docker Monitor needs it to run.'

if command -v uv >/dev/null 2>&1; then
    say 'Installing with uv...'
    uv tool install --force --quiet "docker-monitor @ $SOURCE"
elif command -v pipx >/dev/null 2>&1; then
    say 'Installing with pipx...'
    pipx install --force "$SOURCE" >/dev/null
else
    python=''
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            python="$candidate"
            break
        fi
    done
    [ -n "$python" ] || die 'Docker Monitor needs Python 3.10+ (or uv / pipx). Install one and re-run.'
    say "Installing into a private venv in $DATA_DIR..."
    "$python" -m venv "$DATA_DIR/venv" || die 'Could not create a venv (on Debian/Ubuntu: sudo apt install python3-venv).'
    "$DATA_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$SOURCE"
    mkdir -p "$BIN_DIR"
    ln -sf "$DATA_DIR/venv/bin/docker-monitor" "$BIN_DIR/docker-monitor"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "Add $BIN_DIR to your PATH, e.g.: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile" ;;
    esac
fi

say 'Done. Run: docker-monitor   (or docker-monitor --snapshot for a one-shot summary)'
