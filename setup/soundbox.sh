#!/bin/bash
# Installs or updates Soundbox (https://github.com/HamSanders/soundbox), the local AI audio studio,
# as a native service: on the Mac itself its models run on the GPU (Metal), several times faster
# than in a container. Its code is cloned and built in ~/server/soundbox/src (not in this repo);
# library, settings, model weights and its audio.cpp runtime live in ~/server/soundbox/data.
# To update: change TAG to the new release and run again. The library and the models stay.
#   bash ~/server/setup/soundbox.sh
set -euo pipefail
eval "$(/opt/homebrew/bin/brew shellenv)"
TAG=v0.4.1
DIR=~/server/soundbox
JOB=com.server.soundbox

# Node runs it; eSpeak-ng is what the Kokoro voices (the Spanish ones) pronounce with.
for f in node@22 espeak-ng; do brew list "$f" >/dev/null 2>&1 || brew install "$f"; done
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"

mkdir -p "$DIR/data" "$DIR/bin"
if [[ -d $DIR/src/.git ]]; then
  git -C "$DIR/src" fetch --quiet --depth 1 origin "refs/tags/$TAG:refs/tags/$TAG"
  git -C "$DIR/src" -c advice.detachedHead=false checkout --quiet "$TAG"
else
  git clone --quiet --depth 1 --branch "$TAG" -c advice.detachedHead=false \
    https://github.com/HamSanders/soundbox.git "$DIR/src"
fi

# The same steps as its Dockerfile: the web UI, staged where the server serves it, then the server.
cd "$DIR/src"
npm ci --workspace app --workspace server --include-workspace-root=false --no-audit --no-fund
npm run build --workspace app
rm -rf server/web && cp -r app/dist server/web
npm run build --workspace server

# audio.cpp, the runtime under every model: the Metal build, checksum-verified by Soundbox itself.
export SOUNDBOX_DATA_DIR="$DIR/data"
node server/dist/cli.js model status audiocpp | grep -q '(metal)' || node server/dist/cli.js model install audiocpp --backend metal
# ffmpeg, only for MP3: Jellyfin's, instead of installing another one.
ln -sfn /Applications/Jellyfin.app/Contents/MacOS/ffmpeg "$DIR/bin/ffmpeg"

cp ~/server/setup/launchd/$JOB.plist ~/Library/LaunchAgents/
launchctl bootout "gui/$(id -u)/$JOB" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$JOB.plist
for _ in {1..30}; do curl -fs -m 2 http://127.0.0.1:3023/api/health && break; sleep 1; done
echo
