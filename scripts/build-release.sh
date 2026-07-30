#!/usr/bin/env bash
#
# Build a self-contained release archive.
#
#   ./scripts/build-release.sh            wheels for this platform
#   ./scripts/build-release.sh --no-vendor  skip wheels (needs network to install)
#
# The archive contains the application, a pinned requirements.lock, and — by
# default — every dependency as a wheel under vendor/, so the recipient runs
# ./run.sh and needs no network.
#
# Wheels are platform- and Python-version-specific. An archive built here
# installs on a machine with a compatible platform tag; anywhere else, run.sh
# falls back to PyPI. Build on the target platform when that matters.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VENDOR=1
[ "${1:-}" = "--no-vendor" ] && VENDOR=0

say() { printf '\033[1m==>\033[0m %s\n' "$1"; }

PY="${PY:-$HERE/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

VERSION="$("$PY" - <<'EOF'
import pathlib, re
text = pathlib.Path("pyproject.toml").read_text()
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
print(match.group(1) if match else "0.0.0")
EOF
)"

NAME="jarvis-agent-${VERSION}"
BUILD="$HERE/dist/$NAME"

say "Building $NAME"
rm -rf "$BUILD"
mkdir -p "$BUILD"

# Ship what the app needs to run. Deliberately excluded: .env (secrets), qa/
# (the QA database and embedding index hold real ticket text and source),
# runs/ (archived run records), .venv, and the test suite.
say "Copying application files…"
for item in meta_harness pyproject.toml requirements.lock run.sh README.md LICENSE .env.example configs candidates agents docs MANIFEST.in; do
  [ -e "$HERE/$item" ] && cp -R "$HERE/$item" "$BUILD/"
done
find "$BUILD" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name '*.pyc' -delete 2>/dev/null || true

if [ "$VENDOR" = "1" ]; then
  say "Downloading dependency wheels into vendor/ …"
  mkdir -p "$BUILD/vendor"
  "$PY" -m pip download --quiet --dest "$BUILD/vendor" -r "$HERE/requirements.lock"

  # The build backend is needed to install the project itself and is not a
  # runtime dependency, so it never appears in the lockfile — without it an
  # offline install fails at "installing build dependencies". `pip` is here
  # too because run.sh bootstraps it from this directory when the host has no
  # ensurepip.
  BUILD_DEPS="$("$PY" - <<'EOF'
import pathlib, re
text = pathlib.Path("pyproject.toml").read_text()
block = re.search(r'^\s*requires\s*=\s*\[(.*?)\]', text, re.S | re.M)
print(" ".join(re.findall(r'"([^"]+)"', block.group(1))) if block else "setuptools wheel")
EOF
)"
  say "Vendoring build dependencies: $BUILD_DEPS pip"
  "$PY" -m pip download --quiet --dest "$BUILD/vendor" $BUILD_DEPS pip

  say "Vendored $(find "$BUILD/vendor" -type f | wc -l | tr -d ' ') package file(s)."
else
  say "Skipping vendored wheels (--no-vendor); the recipient will need network access."
fi

cat > "$BUILD/INSTALL.md" <<'EOF'
# Install

```bash
./run.sh
```

That creates a virtualenv, installs every dependency, runs preflight checks,
and opens the web UI at http://127.0.0.1:8877.

`./run.sh --check` runs the checks without starting anything.

## Requirements not bundled

Two things cannot ship inside the archive:

- **Python 3.9+** — install from your package manager if missing. On
  Debian/Ubuntu you also need `python3-venv`.
- **The `claude` CLI** — it installs and authenticates separately, under your
  own account. Ticket generation, QA review and module checks all need it.
  Everything else works without it.

## Configuration

`run.sh` creates `.env` from `.env.example` on first run. Fill in whichever
keys you need — each is independent, and a missing one disables only its own
feature:

| Key | Enables |
|---|---|
| `CLICKUP_API_TOKEN` | the ClickUp tab, ticket creation |
| `LINEAR_API_KEY` | the Linear tab |
| `GEMINI_API_KEY` | repository indexing and retrieval |

`meta-harness doctor` reports exactly what is missing at any point.
EOF

say "Creating the archive…"
tar -czf "$HERE/dist/$NAME.tar.gz" -C "$HERE/dist" "$NAME"

SIZE="$(du -h "$HERE/dist/$NAME.tar.gz" | cut -f1)"
say "Built dist/$NAME.tar.gz ($SIZE)"
say "Verify with: tar -xzf dist/$NAME.tar.gz -C /tmp && cd /tmp/$NAME && ./run.sh --check"
