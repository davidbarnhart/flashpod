#!/bin/bash
# A Mac card image (APM + firmware + wrapped HFS+, card::flash with mac) on
# macOS (#104): hdiutil attaches it, macOS mounts the "disk" partition by
# itself, a file is copied on and read back, and fsck_hfs passes on the
# partition before and after. Synthetic firmware only.
#   scripts/test_mac_card.sh [SIZE_BYTES]
set -euo pipefail
SIZE=${1:-2000000000}
W=$(mktemp -d)
PYTHONPATH=. python3 -c "import sys; sys.path.insert(0,'scripts'); import synth_fixtures as S; open('$W/fw.bin','wb').write(S.firmware_bytes())"
(cd rust && cargo build -q --release -p flashpod-core --example mac_card)
rust/target/release/examples/mac_card "$W/card.img" "$SIZE" "$W/fw.bin"
# fsck the HFS+ partition without mounting it
dev=$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$W/card.img" | awk 'NR==1{print $1}')
diskutil list "$dev"
part=$(diskutil list "$dev" | awk '/Apple_HFS/{print $NF}')
[ -n "$part" ] || { echo "FAIL: no Apple_HFS partition seen"; exit 1; }
fsck_hfs -n "/dev/r$part"
hdiutil detach "$dev"
# let macOS mount it, write a file, read it back
dev=$(hdiutil attach -imagekey diskimage-class=CRawDiskImage "$W/card.img" | awk 'NR==1{print $1}')
mnt=$(mount | awk -v p="/dev/$part " 'index($0,p)==1{sub(/^.* on /,""); sub(/ \(.*$/,""); print}')
[ -n "$mnt" ] || { echo "FAIL: macOS did not mount the HFS+ partition"; mount; exit 1; }
echo "mounted at $mnt"
head -c 3000000 /dev/urandom > "$W/song.bin"
mkdir -p "$mnt/iPod_Control/Music/F00"
cp "$W/song.bin" "$mnt/iPod_Control/Music/F00/song.bin"
sync
cmp "$W/song.bin" "$mnt/iPod_Control/Music/F00/song.bin"
hdiutil detach "$dev"
dev=$(hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage "$W/card.img" | awk 'NR==1{print $1}')
fsck_hfs -n "/dev/r$part"
hdiutil detach "$dev"
echo "PASS: macOS mounted the Mac card, took a file, and fsck_hfs passes before and after"
