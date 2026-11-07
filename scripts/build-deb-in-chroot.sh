#!/bin/bash
# Build the pve-ui-overlays deb (arch: all) in a Debian trixie chroot that has
# debhelper, dh-python and python3 (scripts/mkchroot.sh of qdev-host makes one).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
C="${PVE_UI_CHROOT:-/var/tmp/qdev-trixie.chroot}"
OUT="${PVE_UI_DEB_OUT:-$ROOT/dist}"
[ -d "$C/usr/share/debhelper" ] || { echo "chroot $C has no debhelper" >&2; exit 1; }
sudo -n install -d "$C/work"
sudo -n rm -rf "$C/work/pve-ui-overlays"
sudo -n rsync -a --exclude .git --exclude dist --exclude 'debian/pve-ui-overlays' "$ROOT/" "$C/work/pve-ui-overlays/"
sudo -n chroot "$C" sh -c 'cd /work/pve-ui-overlays && dpkg-buildpackage -us -uc -b' 2>&1 | tail -15
mkdir -p "$OUT"
cp "$C"/work/pve-ui-overlays_*.deb "$OUT"/
ls -la "$OUT"/*.deb
