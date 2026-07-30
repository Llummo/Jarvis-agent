#!/usr/bin/env bash
#
# Start the app. Safe to run repeatedly — it only does the work still needed.
#
#   ./run.sh              install if necessary, preflight, then serve the UI
#   ./run.sh --check      preflight only, change nothing
#   ./run.sh --reinstall  rebuild the virtualenv from scratch
#   ./run.sh --with-embeddings
#                         also install the local embedding model's runtime
#                         (~5 GB: torch). Only repository indexing needs it.
#
# Dependencies install from ./vendor when a release archive ships wheels, so a
# packaged copy needs no network. Otherwise they come from PyPI.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
PY="$VENV/bin/python"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8877}"
MIN_PYTHON="3.9"
EXTRAS="${EXTRAS:-}"

say()  { printf '\033[1m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$1"; }
die()  { printf '\033[31m error\033[0m %s\n' "$1" >&2; exit 1; }

mode="run"
for arg in "$@"; do
  case "$arg" in
    --check)     mode="check" ;;
    --reinstall) mode="reinstall" ;;
    --with-embeddings) EXTRAS="[local-embeddings]" ;;
    -h|--help)   sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "Unknown option: $arg (try --help)" ;;
  esac
done

# --- interpreter ------------------------------------------------------------
# Prefer the newest python3.x present: distro `python3` is sometimes older
# than what is actually installed alongside it.
find_python() {
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= tuple(int(p) for p in '$MIN_PYTHON'.split('.')) else 1)" 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [ "$mode" = "reinstall" ] && [ -d "$VENV" ]; then
  say "Removing the existing virtualenv…"
  rm -rf "$VENV"
fi

if [ ! -x "$PY" ]; then
  SYSTEM_PYTHON="$(find_python)" || die "No Python $MIN_PYTHON+ found. Install Python and re-run."
  say "Creating a virtualenv with $SYSTEM_PYTHON…"

  if ! "$SYSTEM_PYTHON" -m venv "$VENV" 2>/dev/null; then
    # Debian and Ubuntu ship python3 without ensurepip, so the ordinary `venv`
    # fails there on a machine that is otherwise perfectly capable. A vendored
    # release carries pip as a wheel, and pip can be run directly out of that
    # wheel — so build the environment without pip and install pip into it.
    # This is what lets a packaged copy install with no network and no sudo.
    rm -rf "$VENV"
    PIP_WHEEL="$(ls "$HERE"/vendor/pip-*.whl 2>/dev/null | head -1 || true)"
    if [ -z "$PIP_WHEEL" ]; then
      die "Could not create a virtualenv (no ensurepip, no vendored pip). On Debian/Ubuntu: sudo apt install python3-venv"
    fi
    say "ensurepip is unavailable — bootstrapping pip from the bundled wheel…"
    "$SYSTEM_PYTHON" -m venv --without-pip "$VENV" ||
      die "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
    "$PY" "$PIP_WHEEL/pip" install --quiet --no-index --find-links "$HERE/vendor" pip ||
      die "Could not install pip into the virtualenv."
  fi
fi

# --- dependencies -----------------------------------------------------------
# The marker records the dependency spec the venv was last built from, so an
# edited pyproject reinstalls and an unchanged one does not.
STAMP="$VENV/.dependency-stamp"
SPEC_HASH="$EXTRAS:$("$PY" - <<'EOF'
import hashlib, pathlib
parts = []
for name in ("pyproject.toml", "requirements.lock"):
    path = pathlib.Path(name)
    if path.is_file():
        parts.append(path.read_bytes())
print(hashlib.sha256(b"".join(parts)).hexdigest())
EOF
)"

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$SPEC_HASH" ]; then
  if [ -d "$HERE/vendor" ] && [ -n "$(ls -A "$HERE/vendor" 2>/dev/null)" ]; then
    say "Installing dependencies from the bundled vendor/ directory (offline)…"
    "$PY" -m pip install --quiet --upgrade --no-index --find-links "$HERE/vendor" -e ".$EXTRAS"
  else
    say "Installing dependencies from PyPI…"
    "$PY" -m pip install --quiet --upgrade pip
    if [ -f "$HERE/requirements.lock" ]; then
      "$PY" -m pip install --quiet -r "$HERE/requirements.lock"
    fi
    "$PY" -m pip install --quiet -e ".$EXTRAS"
  fi
  printf '%s' "$SPEC_HASH" > "$STAMP"
else
  say "Dependencies already up to date."
fi

# --- configuration ----------------------------------------------------------
if [ ! -f "$HERE/.env" ] && [ -f "$HERE/.env.example" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  warn "Created .env from .env.example — fill in your API keys before using the trackers."
fi

# --- preflight --------------------------------------------------------------
say "Running preflight checks…"
if ! "$VENV/bin/meta-harness" doctor; then
  die "Preflight failed. Fix the items above and re-run."
fi

if [ "$mode" = "check" ]; then
  say "Check complete — nothing started."
  exit 0
fi

# --- serve ------------------------------------------------------------------
URL="http://$HOST:$PORT"
say "Starting the web UI at $URL  (Ctrl+C to stop)"

# Open a browser once the port is actually accepting connections, so the first
# page load isn't a connection error.
(
  for _ in $(seq 1 40); do
    if "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(0.25); sys.exit(0 if s.connect_ex(('$HOST', $PORT))==0 else 1)" 2>/dev/null; then
      for opener in xdg-open open; do
        command -v "$opener" >/dev/null 2>&1 && "$opener" "$URL" >/dev/null 2>&1 && break
      done
      break
    fi
    sleep 0.25
  done
) &

exec "$VENV/bin/meta-harness" ui --host "$HOST" --port "$PORT"
