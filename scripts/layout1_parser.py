#!/usr/bin/env python3
"""The flashpod 1.0 command line (#97) declared with Python's own argparse:
the reference test_cli_args.py --layout-1 holds the Rust parser to, now
that the 0.x Python CLI no longer speaks the same commands.

    python3 scripts/layout1_parser.py music ls --bogus

Parses like the Rust binary and stops where the Rust binary leaves the
parser: help, version and usage errors print exactly what argparse
prints; a 0.x command name prints the Rust binary's pointer (rc 2); a
command that parsed prints "parsed: ..." (rc 0), which test_cli_args
never compares (its lines all end inside the parser). The description
is the binary's own, rust/flashpod-cli/src/doc1.txt. Keep the tree below
in step with build_cli() in rust/flashpod-cli/src/main.rs.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOC = open(os.path.join(HERE, "..", "rust", "flashpod-cli", "src", "doc1.txt")).read()
VERSION = "0.5.2"

RENAMED = [
    ("ls", "music ls"), ("list", "music ls"), ("add", "music add"), ("rm", "music rm"), ("remove", "music rm"),
    ("delete", "music rm"), ("erase", "music rm"), ("init", "music init"), ("rebuild", "music rebuild"),
    ("burn", "card burn"),
]


def renamed_command(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--mount", "--raw"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return dict(RENAMED).get(a)
    return None


def flash_args(p):
    p.add_argument("device", nargs="?", help="target disk, e.g. /dev/sdb (else interactive chooser)")
    p.add_argument("--firmware", default=None,
                   help="firmware image: raw Firmware-*, .bin.gz or .ipsw "
                        "(default: choose from firmware/firmware.json)")
    p.add_argument("--model", default=None,
                   help="iPod model to flash for, e.g. 1G, 2G, 3G "
                        "(default: ask; decides which firmware is offered)")
    p.add_argument("--yes", action="store_true", help="skip the typed ERASE confirmation")
    p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    p.add_argument("--no-format", action="store_true",
                   help="keep the data partition and its music: rewrite "
                        "only the firmware and partition table (refused "
                        "unless the card's data partition matches)")
    arm = p.add_mutually_exclusive_group()
    arm.add_argument("--arm", action="store_true",
                     help="arm the card for a disk-mode update: on its next "
                          "start on FireWire power, the iPod reprograms its "
                          "flash chip from the card (small risk; see README)")
    arm.add_argument("--no-arm", action="store_true", help="don't arm, and don't ask")
    p.add_argument("--lba48", action="store_true",
                   help="EXPERIMENTAL: use the whole card for data, past the "
                        "128 GiB LBA28 cap (only works on an LBA48-patched iPod)")
    p.add_argument("--max-data-gb", type=float, default=None,
                   help="cap the data partition to N decimal GB, overriding the "
                        "firmware's manifest limit (use to probe an early "
                        "firmware's real disk-size ceiling)")
    p.add_argument("--self-test", action="store_true", help="validate layout logic and exit (no hardware)")


def burn_args(p):
    p.add_argument("image", help="the card image file")
    p.add_argument("device", nargs="?", help="target disk, e.g. /dev/sdc (else a chooser of removable disks)")
    p.add_argument("--yes", action="store_true", help="skip the typed ERASE confirmation")
    p.add_argument("--dry-run", action="store_true", help="check everything and show the plan; write nothing")
    p.add_argument("--no-verify", action="store_true", help="don't read the card back afterwards")


def build():
    parser = argparse.ArgumentParser(prog="flashpod", description=DOC,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    parser.add_argument("--mount", default=None,
                        help="iPod mountpoint (default: auto-detect from mounted filesystems)")
    parser.add_argument("--raw", default=None, metavar="DEVICE",
                        help="read the iPod directly from a raw device (e.g. "
                             "/dev/rdisk1s2), no OS mount — for iPods the OS "
                             "can't mount, like a flaky FireWire bridge on "
                             "macOS. Needs root. Currently `ls` only.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mount", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--raw", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--unsafe-queue", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--unsafe-queue", action="store_true", default=False,
                        help="proceed even if the FireWire host queue settings are known-broken for the iPod")
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    p = sub.add_parser("inspect", help="report everything about a card, iPod or image")
    p.add_argument("args", nargs="*", metavar="TARGET")

    fw = sub.add_parser("firmware", help="inspect, extract, patch and write firmware")
    fsub = fw.add_subparsers(dest="firmware_command")
    fsub.required = True
    fsub.add_parser("list", help="the firmware flashpod can write")
    p = fsub.add_parser("extract", help="copy the firmware partition (or one image) off a card")
    p.add_argument("target", help="card image file, or a card or iPod disk (needs root)")
    p.add_argument("-o", "--output", required=True, help="file to write; a FILE.json note is written beside it")
    p.add_argument("--image", choices=["osos", "aupd", "rsrc"],
                   help="one image from the firmware directory, not the whole partition")
    flash_args(fsub.add_parser("write", help="write iPod firmware to a CF/SD card (erases it)"))
    for name, help_ in (("patch", "build a patched firmware"), ("patches", "list the patch library"),
                        ("arm", "arm a disk-mode (boot ROM) update"), ("recover", "the boot ROM recovery routine")):
        fsub.add_parser(name, help=help_).add_argument("args", nargs="*", metavar="ARG")

    card = sub.add_parser("card", help="card image files")
    csub = card.add_subparsers(dest="card_command")
    csub.required = True
    csub.add_parser("new", help="make a blank card image").add_argument("args", nargs="*", metavar="ARG")
    burn_args(csub.add_parser("burn", help="write a card image file to an SD/CF card (erases it)"))

    music = sub.add_parser("music", help="the music library on an iPod, card or image")
    msub = music.add_subparsers(dest="music_command")
    msub.required = True
    p = msub.add_parser("ls", aliases=["list"], help="list library", parents=[common])
    p.add_argument("field", nargs="?", choices=["all", "artist", "artists", "album", "albums"],
                   help="'all' adds tracks to the tree; 'artist'/'album' print flat counts")
    p = msub.add_parser("add", help="add audio files", parents=[common])
    p.add_argument("files", nargs="*", metavar="path",
                   help="audio files or directories (scanned recursively); prompts if omitted")
    p = msub.add_parser("rm", aliases=["remove", "delete", "erase"], help="remove tracks", parents=[common])
    p.add_argument("what", nargs="+", metavar="id|artist|album",
                   help="track ids, or `artist <name>` / `album <name>` to remove every matching track")
    p = msub.add_parser("init", help="create directory structure + empty DB", parents=[common])
    p.add_argument("name", nargs="?", help="iPod name (default: iPod)")
    p = msub.add_parser("rebuild", help="rebuild the iTunesDB from the music files already on the iPod",
                        parents=[common])
    p.add_argument("name", nargs="?", help="iPod name (default: iPod)")
    for name, help_ in (("export", "copy music off with tags and readable names"),
                        ("playlist", "make and edit playlists")):
        msub.add_parser(name, help=help_, parents=[common]).add_argument("args", nargs="*", metavar="ARG")

    sub.add_parser("emulate", help="boot a firmware or card image in emupod").add_argument(
        "args", nargs="*", metavar="ARG")
    flash_args(sub.add_parser("flash", help="write iPod firmware to a CF/SD card (alias of `firmware write`)"))
    return parser


def main(argv):
    new = renamed_command(argv)
    if new:
        sys.stderr.write("flashpod: that command is now `flashpod %s` (flashpod 1.0 groups commands: "
                         "run `flashpod --help`).\n" % new)
        return 2
    opts = build().parse_args(argv)
    print("parsed: %r" % sorted(vars(opts).items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
