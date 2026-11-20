#!/usr/bin/env bash
# Install only the native runtime used by the Ubuntu 24.04 Reel worker.
set -euo pipefail

runtime_ready() {
  local filters encoders
  ffprobe -version >/dev/null 2>&1 || return 1
  fc-match --version >/dev/null 2>&1 || return 1
  filters=$(ffmpeg -filters 2>/dev/null) || return 1
  encoders=$(ffmpeg -encoders 2>/dev/null) || return 1
  grep -Eq '^[[:space:]]*[^ ]+[[:space:]]+ass[[:space:]]' <<< "$filters" || return 1
  for encoder in libx264 aac; do
    grep -Eq "^[[:space:]]*[^ ]+[[:space:]]+$encoder[[:space:]]" <<< "$encoders" || return 1
  done
  python3 -c 'from engine.arabic_layout import _library; _library()'
}

if runtime_ready; then
  echo "Native Reel runtime already available."
  exit 0
fi

# If the runner mirror stalls, download from the signed Ubuntu archive.
# Apply wall-clock limits to network-only operations, never to dpkg unpacking.
sources=$(mktemp -d)
trap 'rm -f -- "$sources/ubuntu.list"; rmdir -- "$sources"' EXIT
for suite in noble noble-updates noble-security; do
  echo "deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://archive.ubuntu.com/ubuntu $suite main universe"
done > "$sources/ubuntu.list"
chmod 755 "$sources"
chmod 644 "$sources/ubuntu.list"

packages=(ffmpeg libass9 fontconfig)
options=(-o Acquire::Retries=1 -o Acquire::Languages=none -o DPkg::Lock::Timeout=20)
for mirror in runner ubuntu; do
  if [[ $mirror == ubuntu ]]; then
    options+=(-o "Dir::Etc::sourcelist=$sources/ubuntu.list" -o Dir::Etc::sourceparts=-)
  fi
  echo "Preparing native Reel runtime via $mirror package sources."
  if sudo timeout --kill-after=5s 45s apt-get "${options[@]}" -o APT::Update::Error-Mode=any update &&
     sudo timeout --kill-after=5s 120s apt-get "${options[@]}" --download-only --no-install-recommends -y install "${packages[@]}"; then
    sudo apt-get "${options[@]}" --no-download --no-install-recommends -y install "${packages[@]}"
    runtime_ready || { echo "Native Reel capability check failed after install." >&2; exit 1; }
    exit 0
  fi
  echo "Could not download the native runtime via $mirror." >&2
done
echo "Native Reel runtime unavailable after both package sources." >&2
exit 1
