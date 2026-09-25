#!/bin/bash
# Install the working tree into the local Kodi and (re)load it.
# Usage: tools/dev-install.sh
#
# The exclude list here is the source of truth that tools/build.py mirrors:
# development-only files that must not reach an installed addon.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.kodi/addons/plugin.audio.kotome"
KODI_RPC="http://localhost:8080/jsonrpc"

# --delete-excluded as well as --delete: plain --exclude *protects* an existing
# copy in the destination, so without it a file that was shipped before being
# added to this list would sit in the installed tree forever.
rsync -a --delete --delete-excluded \
    --exclude '.git' --exclude '.github' --exclude '.gitignore' \
    --exclude '.git-blame-ignore-revs' \
    --exclude '.venv' --exclude '.tox' \
    --exclude '__pycache__' --exclude '.mypy_cache' --exclude '.pytest_cache' \
    --exclude 'docs' --exclude 'tests' --exclude 'tools' --exclude 'dist' \
    --exclude 'mypy.ini' --exclude 'tox.ini' --exclude 'pyproject.toml' \
    --exclude 'requirements-dev.txt' \
    "$SRC/" "$DEST/"

if ! curl -s -m 2 -u kodi:kodi -o /dev/null "$KODI_RPC" \
        -X POST -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.Ping"}'; then
    echo "installed to $DEST (Kodi not reachable — skipped reload/enable)"
    exit 0
fi

"$HOME/bin/kodi-builtin" 'UpdateLocalAddons()'
sleep 2
# service.py owns the sleep timer and playback reporting, so a reinstall has to
# bounce it — enabling an already-enabled addon is a no-op and would leave the
# old code running. Disable first, then enable.
for state in false true; do
    curl -s -u kodi:kodi -X POST -H 'Content-Type: application/json' \
        -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"Addons.SetAddonEnabled\",\"params\":{\"addonid\":\"plugin.audio.kotome\",\"enabled\":$state}}" \
        "$KODI_RPC" > /dev/null
    sleep 2
done
echo "installed and enabled plugin.audio.kotome"
