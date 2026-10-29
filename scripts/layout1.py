"""The 0.x -> 1.0 command names (#97), for running a 0.x scenario on a 1.0
binary while the Python 0.x tree stays the behaviour oracle.

    argv_1_0(["--raw", "x.img", "ls", "all"]) -> ["--raw", "x.img", "music", "ls", "all"]
    output_0x(text) maps the 1.0 program names in usage/error lines back
    ("usage: flashpod music ls" -> "usage: flashpod ls"), so only real
    differences remain; normalize() reports how many lines it changed.
"""
import re

RENAMED = {
    "ls": ["music", "ls"], "list": ["music", "list"], "add": ["music", "add"],
    "rm": ["music", "rm"], "remove": ["music", "remove"], "delete": ["music", "delete"],
    "erase": ["music", "erase"], "init": ["music", "init"], "rebuild": ["music", "rebuild"],
    "burn": ["card", "burn"], "flash": ["firmware", "write"],
}
VALUED = {"--mount", "--raw"}


def argv_1_0(argv):
    """The 0.x argv with its command word spelled the 1.0 way."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in VALUED:
            out += argv[i:i + 2]
            i += 2
            continue
        if a.startswith("-"):
            out.append(a)
            i += 1
            continue
        if a in RENAMED:
            return out + RENAMED[a] + argv[i + 1:]
        return out + argv[i:]
    return out


_PROG = re.compile(r"flashpod (music|card|firmware) (ls|list|add|rm|remove|delete|erase|init|rebuild|burn|write)\b")


def normalize(text):
    """(text with 1.0 program names read back as 0.x ones, lines changed)."""
    n = 0
    lines = []
    for line in text.split("\n"):
        new = _PROG.sub(lambda m: "flashpod " + ("flash" if m.group(2) == "write" else m.group(2)), line)
        # argparse pads the usage continuation to the program name's width
        if new != line:
            n += 1
        lines.append(new)
    return "\n".join(lines), n


_JSON_ARGV = re.compile(r'"(music|card|firmware)", "(ls|list|add|rm|remove|delete|erase|init|rebuild|burn|write)"')


def normalize_calls(text):
    """A recorded tool call's JSON argv (a sudo re-exec of flashpod) with the
    1.0 command words read back: "music", "ls" -> "ls"; "firmware", "write"
    -> "flash". (text, number of replacements)."""
    return _JSON_ARGV.subn(lambda m: '"%s"' % ("flash" if m.group(2) == "write" else m.group(2)), text)
