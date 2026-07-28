#!/usr/bin/env python3
"""Interactive terminal directory/file picker.

Standalone model for what will become flashpod's source-music picker.
Pure stdlib, Python 3.6+, works on Linux, macOS, and Windows consoles.

The browser is a Miller-column view: the directories you traverse stay on
screen as side-by-side columns. Going up prepends the parent directory as
a new leftmost column (the others shift right); going into a directory
adds its listing as a new column on the right.

The cursor row carries affordance hints: a left arrow in the gutter when
going up to a parent is possible, and a right arrow when the highlighted
directory has contents to descend into.

Keys:
  Up/Down       move the cursor within the current column
  Right         open the highlighted directory
  Left          move to the parent directory's column
  Space         select / deselect the highlighted entry
  Shift+Up/Dn   sweep: move and select everything passed over; a sweep
                started on a selected entry deselects instead, and a
                change of direction starts a new sweep
  v             sweep lock on/off (movement sweeps, with the operation
                fixed at lock-on; for terminals that don't report
                Shift+arrows, e.g. Windows)
  .             show / hide dotfiles
  Enter         finish, returning the selection
  Esc or q      cancel
"""

import os
import shutil
import sys
from collections import OrderedDict

IS_WINDOWS = os.name == "nt"


class Entry(object):
    """One row in a directory listing: a file or a directory."""

    __slots__ = ("name", "path", "is_dir")

    def __init__(self, name, path, is_dir):
        self.name = name
        self.path = path
        self.is_dir = is_dir


class DirectoryLister(object):
    """Lists a directory as Entry objects: directories first, then files,
    each group sorted case-insensitively."""

    def __init__(self, show_hidden=False):
        self.show_hidden = show_hidden

    def list(self, path):
        """Return (entries, error): error is a message string when the
        directory could not be read (entries is then empty)."""
        try:
            names = os.listdir(path)
        except OSError as exc:
            return [], str(exc)
        entries = []
        for name in names:
            if not self.show_hidden and name.startswith("."):
                continue
            full = os.path.join(path, name)
            entries.append(Entry(name, full, os.path.isdir(full)))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries, None


class SelectionSet(object):
    """The set of selected paths, in the order they were picked."""

    def __init__(self):
        self._paths = OrderedDict()

    def toggle(self, path):
        if path in self._paths:
            del self._paths[path]
        else:
            self._paths[path] = True

    def add(self, path):
        self._paths[path] = True

    def discard(self, path):
        self._paths.pop(path, None)

    def __contains__(self, path):
        return path in self._paths

    def __len__(self):
        return len(self._paths)

    def paths(self):
        return list(self._paths)


class KeyReader(object):
    """Reads single keystrokes, decoded to symbolic names: 'up' 'down'
    'left' 'right' 'home' 'end' 'pgup' 'pgdn' 'enter' 'space' 'escape',
    'unknown' for other control sequences, or the literal character.
    Use as a context manager; the Unix reader owns cbreak mode."""

    @staticmethod
    def create():
        return _WindowsKeyReader() if IS_WINDOWS else _UnixKeyReader()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise NotImplementedError


class _UnixKeyReader(KeyReader):
    _ESC_MAP = {
        "[A": "up", "[B": "down", "[C": "right", "[D": "left",
        "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
        "[5~": "pgup", "[6~": "pgdn",
        "OA": "up", "OB": "down", "OC": "right", "OD": "left",
        "OH": "home", "OF": "end",
        "[1;2A": "shift+up", "[1;2B": "shift+down",   # xterm-style
        "[a": "shift+up", "[b": "shift+down",         # rxvt-style
    }

    def __enter__(self):
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        return False

    def read(self):
        ch = os.read(self._fd, 1)
        if ch != b"\x1b":
            return self._plain(ch.decode("utf-8", "replace"))
        return self._escape_sequence()

    @staticmethod
    def _plain(ch):
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch

    def _escape_sequence(self):
        # A bare Esc press and an escape sequence both start with \x1b; only
        # the sequence has more bytes already pending (or arriving within a
        # few ms on a slow line).
        import select
        seq = ""
        while len(seq) < 8:
            ready, _, _ = select.select([self._fd], [], [], 0.05)
            if not ready:
                break
            seq += os.read(self._fd, 1).decode("ascii", "replace")
            if seq in self._ESC_MAP:
                return self._ESC_MAP[seq]
            # final byte of a CSI sequence is 0x40-0x7e
            if seq[0] == "[" and len(seq) >= 2 and "\x40" <= seq[-1] <= "\x7e":
                return "unknown"
            if seq[0] == "O" and len(seq) >= 2:
                return "unknown"
            if seq[0] not in "[O":
                return "unknown"
        return "escape" if not seq else "unknown"


class _WindowsKeyReader(KeyReader):
    _EXT_MAP = {
        "H": "up", "P": "down", "K": "left", "M": "right",
        "G": "home", "O": "end", "I": "pgup", "Q": "pgdn",
    }

    def read(self):
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # extended-key prefix
            return self._EXT_MAP.get(msvcrt.getwch(), "unknown")
        if ch == "\r":
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x1b":
            return "escape"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch


class Screen(object):
    """Minimal ANSI full-screen output: alternate buffer, hidden cursor,
    whole-frame repaints. Enables VT processing on Windows 10+ consoles."""

    def __enter__(self):
        if IS_WINDOWS:
            self._enable_windows_vt()
        self._out = sys.stdout
        self._out.write("\x1b[?1049h\x1b[?25l")
        self._out.flush()
        return self

    def __exit__(self, *exc):
        self._out.write("\x1b[?1049l\x1b[?25h")
        self._out.flush()
        return False

    @staticmethod
    def _enable_windows_vt():
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | vt)

    @staticmethod
    def size():
        # get_terminal_size can report 0x0 (seen: macOS 10.8 pty with an
        # unset winsize — the ioctl "succeeds" and no fallback kicks in)
        ts = shutil.get_terminal_size()
        return (ts.columns if ts.columns > 0 else 80,
                ts.lines if ts.lines > 0 else 24)

    def draw(self, lines):
        # Repaint in place: home the cursor, rewrite every line erasing its
        # tail, then erase whatever is left below the frame.
        frame = "\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J"
        self._out.write(frame)
        self._out.flush()


class Column(object):
    """One directory pane in the browser: a path, its listing, and its
    own cursor/scroll state."""

    __slots__ = ("path", "entries", "error", "cursor", "scroll")

    def __init__(self, path):
        self.path = path
        self.entries = []
        self.error = None
        self.cursor = 0
        self.scroll = 0


class DirectoryPicker(object):
    """Miller-column directory/file browser with multi-select.

    The column list is the traversal trail: columns[active] is the pane
    the cursor lives in; ancestors sit to its left, descended-into
    directories to its right. run() drives the whole interaction and
    returns the selected paths in pick order, or None on cancel.
    """

    HEADER_ROWS = 2
    FOOTER_ROWS = 2
    HELP1 = ("Up/Dn move   Right open   Left up   Space select   "
             "Shift+Up/Dn sweep select")
    HELP2 = "v sweep lock   [.] hidden   Enter done   Esc cancel"
    MIN_COL_WIDTH = 20
    MAX_COL_WIDTH = 42

    def __init__(self, start_dir=None, show_hidden=False):
        start = os.path.abspath(start_dir or os.path.expanduser("~"))
        self.lister = DirectoryLister(show_hidden)
        self.selection = SelectionSet()
        self.columns = [self._make_column(start)]
        self.active = 0
        self.sweep = False
        self._sweep_op = None   # 'add'/'discard' while a sweep run is live
        self._sweep_key = None  # last shift-move key of the run
        self._openable = {}     # path -> has-contents probe cache
        self._first_vis = 0
        self._page = 1

    # -- model ------------------------------------------------------------

    def _make_column(self, path, remember=None):
        col = Column(path)
        self._fill(col, remember)
        return col

    def _fill(self, col, remember=None):
        """(Re)list a column; cursor goes to `remember` (a contained path)
        if given and present, else to the top."""
        entries, error = self.lister.list(col.path)
        col.entries = entries
        col.error = error
        col.scroll = 0
        col.cursor = 0
        if remember is not None:
            for i, entry in enumerate(entries):
                if entry.path == remember:
                    col.cursor = i
                    break

    def _go_up(self):
        """Move focus one column left, prepending the parent directory as
        a new leftmost column if we're already at the left edge."""
        if self.active > 0:
            self.active -= 1
            return
        top = self.columns[0]
        parent = os.path.dirname(top.path)
        if parent != top.path:
            self.columns.insert(0, self._make_column(parent,
                                                     remember=top.path))
            self._first_vis += 1  # existing columns all shifted right

    def _open(self, entry):
        """Descend into a directory entry, reusing the trail to the right
        when it already leads there."""
        nxt = self.active + 1
        if nxt < len(self.columns) and self.columns[nxt].path == entry.path:
            self.active = nxt
            return
        del self.columns[nxt:]
        self.columns.append(self._make_column(entry.path))
        self.active = nxt

    def _start_sweep(self, col):
        """Begin a sweep run unless one is already live: the operation
        comes from the entry the sweep starts on — selected means this
        run deselects, unselected means it selects."""
        if self._sweep_op is None:
            selected = col.entries[col.cursor].path in self.selection
            self._sweep_op = "discard" if selected else "add"

    def _sweep_apply(self, path):
        if self._sweep_op == "discard":
            self.selection.discard(path)
        else:
            self.selection.add(path)

    def _move_to(self, col, target, sweeping):
        """Move the column cursor; when sweeping, apply the live sweep
        operation to every entry from the old position through the new
        one (inclusive)."""
        target = max(0, min(len(col.entries) - 1, target))
        if sweeping:
            lo, hi = sorted((col.cursor, target))
            for entry in col.entries[lo:hi + 1]:
                self._sweep_apply(entry.path)
        col.cursor = target

    _MOVE_DELTAS = {"up": -1, "down": 1}
    _MOVE_KEYS = ("up", "down", "shift+up", "shift+down",
                  "pgup", "pgdn", "home", "end")

    def _handle(self, key):
        """Apply one key. Returns the final selection list on Enter,
        None on cancel, or self (sentinel: keep going)."""
        col = self.columns[self.active]
        n = len(col.entries)
        if key == "enter":
            return self.selection.paths()
        if key in ("escape", "q"):
            return None
        # Any key that doesn't continue a sweep ends the current run (the
        # next sweep re-decides select-vs-deselect from its start entry).
        continues = key == "v" or (key in self._MOVE_KEYS
                                   and (self.sweep
                                        or key.startswith("shift+")))
        if not continues:
            self._sweep_op = None
            self._sweep_key = None
        if key in self._MOVE_KEYS and n:
            sweeping = self.sweep or key.startswith("shift+")
            if sweeping:
                # a shift-sweep that changes direction is a new gesture:
                # re-decide select-vs-deselect from the entry it starts on
                # (under the v-lock the operation holds until lock-off)
                if (not self.sweep and key.startswith("shift+")
                        and key != self._sweep_key):
                    self._sweep_op = None
                self._sweep_key = key
                self._start_sweep(col)
            base = key.split("+")[-1]
            if base in self._MOVE_DELTAS:
                target = col.cursor + self._MOVE_DELTAS[base]
            elif base == "pgup":
                target = col.cursor - self._page
            elif base == "pgdn":
                target = col.cursor + self._page
            elif base == "home":
                target = 0
            else:  # end
                target = n - 1
            self._move_to(col, target, sweeping)
        elif key == "v":
            self.sweep = not self.sweep
            if self.sweep and n:
                self._start_sweep(col)
                self._sweep_apply(col.entries[col.cursor].path)
            else:
                self._sweep_op = None
        elif key == "left":
            self._go_up()
        elif key == "right" and n:
            entry = col.entries[col.cursor]
            if entry.is_dir:
                self._open(entry)
        elif key == "space" and n:
            self.selection.toggle(col.entries[col.cursor].path)
        elif key == ".":
            self.lister.show_hidden = not self.lister.show_hidden
            self._openable.clear()  # probe results depend on the filter
            for c in self.columns:
                here = c.entries[c.cursor].path if c.entries else None
                self._fill(c, remember=here)
        return self

    def _can_go_up(self):
        """True when Left would do something: there's a trail column to
        the left, or the active directory has a parent."""
        if self.active > 0:
            return True
        path = self.columns[self.active].path
        return os.path.dirname(path) != path

    def _has_children(self, path):
        """True when the directory has at least one entry the current
        filter would show. Probed lazily (one scandir, stops at the first
        hit) and cached per path."""
        if path not in self._openable:
            found = False
            try:
                for it in os.scandir(path):
                    if (self.lister.show_hidden
                            or not it.name.startswith(".")):
                        found = True
                        break
            except OSError:
                found = False
            self._openable[path] = found
        return self._openable[path]

    # -- view -------------------------------------------------------------

    GUTTER = 2  # room for the left/right affordance arrows

    @staticmethod
    def _row_text(entry, selected):
        mark = "*" if selected else " "
        return "  [%s] %s%s" % (mark, entry.name,
                                os.sep if entry.is_dir else "")

    def _column_rows(self, col):
        """The column's full listing as plain-text rows (each starts with
        the arrow gutter)."""
        rows = [self._row_text(e, e.path in self.selection)
                for e in col.entries]
        if col.error is not None:
            rows.append("  (unreadable: %s)" % col.error)
        elif not col.entries:
            rows.append("  (empty)")
        return rows

    def _cursor_cell(self, col, width):
        """The active cursor row: arrow gutters on both sides of the row
        body — '← ' when Left can go up, ' →' when the highlighted
        directory has contents to open."""
        entry = col.entries[col.cursor]
        body_w = max(0, width - 2 * self.GUTTER)
        body = self._row_text(entry, entry.path in self.selection)
        body = body[self.GUTTER:]  # the plain gutter; arrows replace it
        left = "← " if self._can_go_up() else "  "
        right = (" →" if entry.is_dir and self._has_children(entry.path)
                 else "  ")
        return left + body[:body_w].ljust(body_w) + right

    def _visible_range(self, widths, term_cols):
        """Choose which consecutive columns to show: keep the active one
        visible, moving the window as little as possible."""

        def last_fitting(first):
            total, last = 0, first
            for i in range(first, len(widths)):
                total += widths[i] + (1 if i > first else 0)  # +separator
                if total > term_cols and i > first:
                    break
                last = i
            return last

        def span(f, l):
            return sum(widths[f:l + 1]) + (l - f)  # widths + separators

        first = min(self._first_vis, self.active)
        while last_fitting(first) < self.active:
            first += 1
        # reclaim leftover width: pull ancestor columns back into view as
        # long as doing so evicts nothing currently visible
        while first > 0 and span(first - 1, last_fitting(first)) <= term_cols:
            first -= 1
        self._first_vis = first
        return first, last_fitting(first)

    def _frame(self, term_cols, term_rows):
        view = max(1, term_rows - self.HEADER_ROWS - self.FOOTER_ROWS)
        self._page = view

        all_rows = [self._column_rows(c) for c in self.columns]
        widths = [max(self.MIN_COL_WIDTH,
                      min(self.MAX_COL_WIDTH, term_cols,
                          max(len(r) for r in rows) + 1 if rows else 0))
                  for rows in all_rows]
        first, last = self._visible_range(widths, term_cols)

        header_path = self.columns[self.active].path
        if len(header_path) > term_cols - 16:
            header_path = "..." + header_path[-(term_cols - 19):]
        count = "%d selected%s" % (len(self.selection),
                                   " [SWEEP]" if self.sweep else "")
        pad = max(1, term_cols - len(header_path) - len(count) - 1)
        lines = ["\x1b[1m" + header_path + " " * pad + count + "\x1b[0m", ""]

        for i in range(first, last + 1):
            col = self.columns[i]
            if col.cursor < col.scroll:
                col.scroll = col.cursor
            if col.cursor >= col.scroll + view:
                col.scroll = col.cursor - view + 1

        for r in range(view):
            parts = []
            for i in range(first, last + 1):
                col = self.columns[i]
                idx = col.scroll + r
                rows = all_rows[i]
                text = rows[idx] if idx < len(rows) else ""
                on_cursor = idx == col.cursor and idx < len(col.entries)
                if on_cursor and i == self.active:
                    cell = ("\x1b[7m"
                            + self._cursor_cell(col, widths[i]) + "\x1b[0m")
                elif on_cursor:
                    cell = ("\x1b[1m"
                            + text[:widths[i]].ljust(widths[i]) + "\x1b[0m")
                else:
                    cell = text[:widths[i]].ljust(widths[i])
                parts.append(cell)
            lines.append("│".join(parts))

        lines.append(self.HELP1[:term_cols])
        lines.append(self.HELP2[:term_cols])
        return lines

    # -- controller -------------------------------------------------------

    def run(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RuntimeError("directory picker needs an interactive "
                               "terminal (stdin and stdout must be ttys)")
        with KeyReader.create() as keys:
            with Screen() as screen:
                while True:
                    cols, rows = screen.size()
                    screen.draw(self._frame(cols, rows))
                    try:
                        key = keys.read()
                    except KeyboardInterrupt:
                        return None
                    result = self._handle(key)
                    if result is not self:
                        return result
