#!/usr/bin/env python3
"""The flashpod 1.0 command line (#97) declared with Python's own argparse:
the reference test_cli_args.py --layout-1 holds the Rust parser to, now
that the 0.x Python CLI no longer speaks the same commands.

    python3 scripts/layout1_parser.py music list --bogus

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
VERSION = "1.0.0-dev"

# 0.x command names and the 1.0 spelling that replaced them
RENAMED = [
    ("ls", "music list"), ("list", "music list"), ("add", "music add"), ("rm", "music remove"),
    ("remove", "music remove"), ("delete", "music remove"), ("erase", "music remove"), ("init", "music init"),
    ("rebuild", "music rebuild"), ("burn", "drive restore"), ("wizard", "setup"),
    ("card", "image new` or `flashpod drive restore"),
]

# two-word names that moved in 1.0 (#115)
RENAMED_2 = [
    (("card", "new"), "image new"), (("card", "burn"), "drive restore"),
    (("firmware", "patches"), "patch list"), (("firmware", "patch"), "patch apply"),
    (("firmware", "arm"), "bootrom arm"), (("firmware", "recover"), "help recovery"),
    (("music", "delete"), "music remove"), (("music", "erase"), "music remove"),
]

# 0.x flash did three things; 1.0 splits them (#115)
FLASH_SPLIT = ("drive partition --target T` (erases the drive), then "
               "`flashpod firmware write --target T FIRMWARE")


def command_words(args):
    """args after the top-level options (--mount and --raw take a value)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--mount", "--raw"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return args[i:]
    return []


def renamed_command(args):
    w = command_words(args)
    if not w:
        return None
    if w[0] == "flash":
        return FLASH_SPLIT
    if len(w) > 1:
        new = dict(RENAMED_2).get((w[0], w[1]))
        if new:
            return new
    return dict(RENAMED).get(w[0])


def group_help(parser, words):
    """words naming only command groups: that group's help (bare
    `flashpod` is the top's), else None."""
    p = parser
    for w in words:
        sub = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not sub or w not in sub[0].choices:
            return None
        p = sub[0].choices[w]
    if not any(isinstance(a, argparse._SubParsersAction) for a in p._actions):
        return None
    return p.format_help()


def firmware_write_args(p):
    p.add_argument("firmware_file", nargs="?", metavar="FIRMWARE",
                   help="firmware image: raw Firmware-*, .bin.gz or .ipsw (default: choose from the catalog)")
    p.add_argument("--target", help="the drive or image file (else a chooser)")
    p.add_argument("--model", default=None,
                   help="iPod model, e.g. 1G, 2G, 3G (default: ask; decides which firmware is offered)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation (never approves an erase)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    # the 0.x flash flags, hidden, so each can say where it went
    for flag in ("--no-format", "--arm", "--no-arm", "--lba48"):
        p.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--max-data-gb", dest="max_data_gb_moved", help=argparse.SUPPRESS)
    p.add_argument("--format", dest="format_moved", help=argparse.SUPPRESS)
    p.add_argument("--firmware", dest="firmware_flag", help=argparse.SUPPRESS)


def partition_args(p):
    p.add_argument("--target", help="the drive or image file (else a chooser)")
    p.add_argument("--128g", dest="cap_128g", action="store_true",
                   help="cap the data partition at 128 GiB, for firmware without large-disk support (Apple's)")
    p.add_argument("--max-data-gb", type=float, default=None, help="cap the data partition to N decimal GB")
    p.add_argument("--format", choices=["fat32", "hfs"],
                   help="the data partition's filesystem: fat32 (default; any OS) or hfs "
                        "(a Mac drive: Apple Partition Map + HFS+)")
    p.add_argument("--yes", action="store_true", help="skip the typed ERASE confirmation")
    p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")


def burn_args(p):
    p.add_argument("image", help="the card image file")
    p.add_argument("device", nargs="?", help="target disk, e.g. /dev/sdc (else a chooser of removable disks)")
    p.add_argument("--yes", action="store_true", help="skip the typed ERASE confirmation")
    p.add_argument("--dry-run", action="store_true", help="check everything and show the plan; write nothing")
    p.add_argument("--no-verify", action="store_true", help="don't read the card back afterwards")


def add(sub, name, **kw):
    """sub.add_parser, suggesting the closest name on a typo (#115 H):
    suggest_on_error is per parser, subparsers don't inherit it."""
    return sub.add_parser(name, suggest_on_error=True, **kw)


def coming(sub, name, help_):
    """A command that parses its arguments and says it isn't built yet."""
    add(sub, name, help=help_ + " (coming)").add_argument("args", nargs="*", metavar="ARG")


def group(sub, name, dest, help_):
    g = add(sub, name, help=help_)
    gs = g.add_subparsers(dest=dest)
    gs.required = True
    return gs


def build():
    parser = argparse.ArgumentParser(prog="flashpod", description=DOC, suggest_on_error=True,
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

    add(sub, "setup", help="set up a card step by step: partition, firmware, library, music").add_argument(
        "args", nargs="*", metavar="ARG")

    p = add(sub, "inspect", help="report everything about a card, iPod or image")
    p.add_argument("target", help="card image file, or a card or iPod disk (needs root)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")

    dsub = group(sub, "drive", "drive_command", "a whole drive: its partitions, copies to and from image files")
    partition_args(add(dsub, "partition", help="partition a drive for an iPod (erases it)"))
    coming(dsub, "backup", "copy a whole drive into an image file")
    burn_args(add(dsub, "restore", help="write a card image file onto a drive (erases it)"))

    isub = group(sub, "image", "image_command", "card image files")
    p = add(isub, "new", help="make a blank card image")
    p.add_argument("image", help="the image file to make (must not exist)")
    p.add_argument("size", help="its size: bytes, or with K/M/G/T (decimal, as cards are sold) or KiB/MiB/GiB/TiB")

    fsub = group(sub, "firmware", "firmware_command", "write firmware to a drive, or copy it off one")
    coming(fsub, "list", "the firmware flashpod can write")
    firmware_write_args(add(fsub, "write", help="write firmware into a drive's firmware partition (nothing else changes)"))
    p = add(fsub, "extract", help="copy the firmware partition (or one image) off a card")
    p.add_argument("target", help="card image file, or a card or iPod disk (needs root)")
    p.add_argument("-o", "--output", required=True, help="file to write; a FILE.json note is written beside it")
    p.add_argument("--image", choices=["osos", "aupd", "rsrc"],
                   help="one image from the firmware directory, not the whole partition")

    bsub = group(sub, "bootrom", "bootrom_command", "the iPod's boot ROM (disk-mode update)")
    p = add(bsub, "arm", help="arm a disk-mode update from the firmware on the drive")
    p.add_argument("program", nargs="?", help="the program to arm (default: the one in the firmware on the drive)")
    p.add_argument("--target", help="the drive or image file (else a chooser)")
    p.add_argument("--yes", action="store_true", help="skip the arm confirmation")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p = add(bsub, "disarm", help="clear an armed disk-mode update")
    p.add_argument("--target", help="the drive or image file (else a chooser)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")

    psub = group(sub, "patch", "patch_command", "firmware patches")
    add(psub, "list", help="the patch library").add_argument(
        "--offline", action="store_true", help="don't refresh the library; use the copy on this computer")
    coming(psub, "show", "one patch in full")
    coming(psub, "apply", "build a patched firmware")

    music = add(sub, "music", help="the music library on an iPod, card or image")
    msub = music.add_subparsers(dest="music_command")
    msub.required = True
    p = add(msub, "list", aliases=["ls"], help="the library: artist/album tree, its tracks, or flat counts",
            parents=[common])
    p.add_argument("field", nargs="?", choices=["tracks", "artists", "albums"],
                   help="'tracks' adds the tracks (with ids) to the tree; 'artists'/'albums' print flat counts")
    coming(msub, "find", "search the library")
    coming(msub, "check", "check the library against the files on the card")
    p = add(msub, "add", help="add audio files", parents=[common])
    p.add_argument("files", nargs="*", metavar="path",
                   help="audio files or directories (scanned recursively); prompts if omitted")
    p = add(msub, "remove", aliases=["rm"], help="remove tracks", parents=[common])
    p.add_argument("what", nargs="+", metavar="id|artist|album",
                   help="track ids, or `artist <name>` / `album <name>` to remove every matching track")
    p = add(msub, "init", help="create directory structure + empty DB", parents=[common])
    p.add_argument("name", nargs="?", help="iPod name (default: iPod)")
    p = add(msub, "rebuild", help="rebuild the iTunesDB from the music files already on the iPod",
            parents=[common])
    p.add_argument("name", nargs="?", help="iPod name (default: iPod)")
    p = add(msub, "export", help="copy music off with tags and readable names", parents=[common])
    p.add_argument("--to", metavar="DIR", help="where to put it (default: ./<the iPod's name>)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--other-files", action="store_true",
                   help="also export the files the database doesn't list, without asking")
    g.add_argument("--no-other-files", action="store_true", help="don't export them, and don't ask")
    pl = add(msub, "playlist", help="make and edit playlists", parents=[common])
    plsub = pl.add_subparsers(dest="playlist_command")
    plsub.required = True
    add(plsub, "list", aliases=["ls"], help="the playlists, or one playlist's tracks", parents=[common]).add_argument(
        "name", nargs="?", help="a playlist to list the tracks of")
    p = add(plsub, "new", help="make a playlist", parents=[common])
    p.add_argument("name", help="its name")
    p.add_argument("ids", nargs="*", metavar="id", help="tracks to put in it, in order")
    for name, aliases, help_ in (("add", [], "add tracks to a playlist"),
                                 ("remove", ["rm"], "take tracks out of a playlist (they stay on the iPod)")):
        p = add(plsub, name, aliases=aliases, help=help_, parents=[common])
        p.add_argument("name", help="the playlist")
        p.add_argument("ids", nargs="+", metavar="id", help="track ids (from `flashpod music list tracks`)")
    add(plsub, "delete", help="delete a playlist (its tracks stay on the iPod)", parents=[common]).add_argument(
        "name", help="the playlist")

    gsub = group(sub, "games", "games_command", "games on the iPod")
    coming(gsub, "list", "the games on the iPod")
    coming(gsub, "add", "add a game")
    coming(gsub, "remove", "remove a game")

    coming(sub, "emulate", "boot a firmware or card image in emupod")
    coming(sub, "completion", "shell completion for bash, zsh or fish")
    add(sub, "help", help="help on a topic: recovery (a card or iPod that won't start)").add_argument(
        "topic", nargs="?", choices=["recovery"], help="the topic (default: the command list)")
    return parser


# `help recovery`: the Rust binary include_str!s this same file
RECOVERY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "rust", "flashpod-cli", "src", "help_recovery.txt")

# the commands the Rust binary parses and then says aren't built
NOT_BUILT = {"setup", "drive backup", "firmware list", "patch show", "patch apply", "music find",
             "music check", "games list", "games add", "games remove", "emulate", "completion"}

# the flags `firmware write` lost (#115), each naming where it went
MOVED = [
    ("no_format", "--no-format: `firmware write` never touches the data partition now, so drop it."),
    ("arm", "--arm: arming is its own command now: `flashpod bootrom arm --target T`."),
    ("no_arm", "--no-arm: `firmware write` never arms now, so drop it."),
    ("lba48", "--lba48: `flashpod drive partition` uses the whole drive by default now (--128g caps it)."),
    ("max_data_gb_moved", "--max-data-gb: it moved to `flashpod drive partition`."),
    ("format_moved", "--format: it moved to `flashpod drive partition`."),
    ("firmware_flag", "--firmware: the firmware file is the argument now and the drive is --target: "
                      "`flashpod firmware write --target T FIRMWARE`."),
]


def main(argv):
    new = renamed_command(argv)
    if new:
        sys.stderr.write("flashpod: that command is now `flashpod %s` (flashpod 1.0 groups commands: "
                         "run `flashpod --help`).\n" % new)
        return 2
    parser = build()
    if not any(a.startswith("-") for a in argv):
        text = group_help(parser, argv)
        if text is not None:
            sys.stdout.write(text)
            return 0
    opts = parser.parse_args(argv)
    if opts.command == "firmware" and opts.firmware_command == "write":
        for k, msg in MOVED:
            if getattr(opts, k) not in (None, False):
                sys.stderr.write("flashpod firmware write: %s\n" % msg)
                return 2
    if opts.command == "help" and opts.topic is None:
        sys.stdout.write(parser.format_help())
        return 0
    sub = getattr(opts, "%s_command" % opts.command, None)
    spelled = " ".join([opts.command] + ([sub] if sub else []))
    if opts.command == "help":
        if opts.topic == "recovery":
            with open(RECOVERY) as f:
                sys.stdout.write(f.read())
            return 0
        spelled += " " + opts.topic
    if spelled in NOT_BUILT:
        sys.stderr.write("flashpod %s: not built yet (flashpod 1.0, epic #88).\n" % spelled)
        return 2
    print("parsed: %r" % sorted(vars(opts).items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
