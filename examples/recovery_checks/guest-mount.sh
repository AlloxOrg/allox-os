#!/bin/sh
set -eu
export PATH="/fixtures/bin:$PATH"
export LD_LIBRARY_PATH=/fixtures/bin/lib
export PYTHONPATH=/allox/src
export PYTHONDONTWRITEBYTECODE=1
test "$(uname -r)" = "6.1.62-allox-joint-btrfs"
grep -w btrfs /proc/filesystems
for delay in $(seq 1 60); do
    [ ! -f /gate/start ] || break
    sleep 1
done
test -f /gate/start
python3 -m pip install -q --no-deps /experiment/wheels/*.whl
disk=/dev/shm/recovery-btrfs.img
mountpoint=/dev/shm/recovery-btrfs
test ! -e "$disk"
mkdir "$mountpoint"
truncate -s 512M "$disk"
mkfs.btrfs -L allox-recovery -m single -d single "$disk"
# Only the newly created disposable Guest's loop devices are touched.
for block in /sys/class/block/loop*; do
    name=${block##*/}
    [ -e "/dev/$name" ] && continue
    device=$(cat "$block/dev")
    mknod "/dev/$name" b "${device%:*}" "${device#*:}"
done
if [ ! -e /dev/loop-control ] && [ -r /sys/class/misc/loop-control/dev ]; then
    device=$(cat /sys/class/misc/loop-control/dev)
    mknod /dev/loop-control c "${device%:*}" "${device#*:}"
fi
loop=$(losetup --find --show "$disk")
trap 'umount "$mountpoint" 2>/dev/null || true; losetup -d "$loop" 2>/dev/null || true' EXIT
mount -t btrfs -o noatime "$loop" "$mountpoint"
python3 -u /experiment/guest.py "$mountpoint"
