"""The 0.x -> 1.0 command names (#97), for running a 0.x scenario on a 1.0
binary while the Python 0.x tree stays the behaviour oracle.

    argv_1_0(["--raw", "x.img", "ls", "all"]) -> ["--raw", "x.img", "music", "list", "tracks"]
    output_0x(text) maps the 1.0 program names in usage/error lines back
    ("usage: flashpod music list" -> "usage: flashpod ls") and drops the 1.0
    target line (#112), so only real differences remain; normalize() reports
    how many lines it changed.
"""
import re

RENAMED = {
    "ls": ["music", "list"], "list": ["music", "list"], "add": ["music", "add"],
    "rm": ["music", "remove"], "remove": ["music", "remove"], "delete": ["music", "remove"],
    "erase": ["music", "remove"], "init": ["music", "init"], "rebuild": ["music", "rebuild"],
    "burn": ["drive", "restore"], "flash": ["firmware", "write"],
}
# 0.x ls fields that 1.0 renamed (#115): `ls all` is `music list tracks`
FIELDS = {"all": "tracks", "artist": "artists", "album": "albums"}
VALUED = {"--mount", "--raw"}


def _field_1_0(rest):
    """ls's arguments with its field (the first positional) spelled the 1.0 way."""
    out, i = list(rest), 0
    while i < len(out):
        if out[i] in VALUED:
            i += 2
        elif out[i].startswith("-"):
            i += 1
        else:
            out[i] = FIELDS.get(out[i], out[i])
            break
    return out


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
            rest = argv[i + 1:]
            if a in ("ls", "list"):
                rest = _field_1_0(rest)
            return out + RENAMED[a] + rest
        return out + argv[i:]
    return out


_TARGET = re.compile(r"Target: .* \((image file|mounted iPod|card)\)\r?$")
_PROG = re.compile(r"flashpod (music|drive|firmware) (list tracks|list|ls|add|remove|rm|init|rebuild|restore|write)\b")
# a 1.0 name read back as the 0.x one (0.x printed an alias's canonical name)
_BACK = {"list tracks": "ls all", "list": "ls", "ls": "ls", "remove": "rm", "rm": "rm",
         "restore": "burn", "write": "flash"}


def normalize(text):
    """(text with 1.0 program names read back as 0.x ones, lines changed)."""
    n = 0
    lines = []
    for line in text.split("\n"):
        if _TARGET.fullmatch(line):
            n += 1
            continue
        new = _PROG.sub(lambda m: "flashpod " + _BACK.get(m.group(2), m.group(2)), line)
        # argparse pads the usage continuation to the program name's width
        if new != line:
            n += 1
        lines.append(new)
    return "\n".join(lines), n


_JSON_ARGV = re.compile(r'"(music|drive|firmware)", "(list", "tracks|list|ls|add|remove|rm|init|rebuild|restore|write)"')


def normalize_calls(text):
    """A recorded tool call's JSON argv (a sudo re-exec of flashpod) with the
    1.0 command words read back: "music", "list" -> "ls"; "music", "list",
    "tracks" -> "ls", "all"; "firmware", "write" -> "flash". (text, number of replacements)."""
    return _JSON_ARGV.subn(lambda m: '"%s"' % _BACK.get(m.group(2).replace('", "', " "), m.group(2)).replace(" ", '", "'), text)


def flash_split(argv):
    """#115: 0.x `flash IMG --firmware FW --yes` is two 1.0 commands,
    `drive partition` then `firmware write`; their result (exit code and
    image bytes) must be the 0.x flash's. Any other flash step tested
    flash's own interface (dry run, choosers, confirmation, refusals) and
    is retired here: None. Its 1.0 counterpart is in
    scripts/test_firmware_write_only.py."""
    if not argv or argv[0] != "flash":
        return False
    rest = argv[1:]
    if "--yes" not in rest or "--firmware" not in rest:
        return None
    rest = [a for a in rest if a != "--yes"]
    i = rest.index("--firmware")
    fw = rest[i + 1]
    del rest[i:i + 2]
    if len(rest) != 1 or rest[0].startswith("-"):
        return None
    img = rest[0]
    return [["drive", "partition", "--target", img, "--yes"],
            ["firmware", "write", "--target", img, fw, "--yes"]]

