"""A disk image file as the target: a regular file standing in for a card.

``flash``, ``init``, ``add``, ``rm`` and ``ls`` all work on a whole-disk image
exactly as they do on a card -- same partition table, same firmware placement,
same FAT32, same iTunesDB -- which is what an emulator wants to boot. What a
file does not need is everything that exists because a card is a DEVICE:
root, unmounting, wiping signatures, re-reading the partition table,
flushing the buffer cache, ejecting, and the FireWire bridge's tiny transfers.

:class:`ImagePlatform` wraps the running OS's backend and turns exactly those
into no-ops; everything else is the backend's. The file must already exist at
the size the card should be (``truncate -s 1G ipod.img``): its size is the
card's size.
"""

import os

#: Transfer size for the userspace FAT driver on a file: no bridge to protect.
IMAGE_MAX_XFER = 2048


def is_image(path):
    """True if ``path`` names an existing regular file (not a device node)."""
    try:
        return bool(path) and os.path.isfile(path)
    except (OSError, TypeError, ValueError):
        return False


class ImagePlatform(object):
    """The OS backend, minus every step that only a device needs."""

    is_image = True

    def __init__(self, inner):
        self._inner = inner
        self.name = "image(%s)" % getattr(inner, "name", "?")

    def __getattr__(self, attr):            # anything not overridden below
        return getattr(self._inner, attr)

    # -- privilege: a file the user can write needs no root ----------------
    def is_admin(self):
        return True

    # -- the target ---------------------------------------------------------
    def validate_target(self, dev, dry_run):
        import sys
        if not is_image(dev):
            sys.exit("flashpod: %s is not a regular file" % dev)
        if os.path.getsize(dev) < 512:
            sys.exit("flashpod: %s is empty -- give it the card's size first, "
                     "e.g. `truncate -s 1G %s`" % (dev, dev))

    def device_sectors(self, dev):
        return os.path.getsize(dev) // 512

    def device_mountpoints(self, dev):
        return []

    def init_before_mbr(self):
        # Nothing can be mounted from a file here, so the post-flash init goes
        # through the still-open handle -- the path Windows already takes.
        return True

    # -- device housekeeping: none of it applies to a file ------------------
    def unmount_all(self, dev, dry):
        return

    def wipe_signatures(self, dev, dry):
        return

    def invalidate_cached_partitions(self, dev):
        return

    def reread_partition_table(self, dev):
        return

    def flush_buffers(self, dev):
        return

    def eject(self, dev, dry):
        return

    def eject_checked(self, dev):
        return True, ""

    # -- raw I/O: a plain file object ----------------------------------------
    def open_raw(self, dev, mode):
        return open(dev, mode)

    def prepare_raw_write(self, dev):
        return

    def raw_part_start_override(self):
        return None

    def finalize_raw_write(self, dev):
        return

    def raw_open_direct(self):
        return False

    def raw_read_node(self, dev):
        return dev

    def raw_max_xfer(self, device=None):
        return IMAGE_MAX_XFER
