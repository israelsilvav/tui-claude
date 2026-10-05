"""Filesystem links that work on POSIX and on Windows.

Everything tui-claude does is re-pointing links, and POSIX symlinks are the
natural tool. Windows only lets an unprivileged user create symlinks in
Developer Mode, so each kind of link falls back to what needs no privilege:

    directories  POSIX: symlink   Windows: junction (never needs privilege)
    files        POSIX: symlink   Windows: symlink, else hardlink

A hardlink is not a pointer but a second name for the same file, so it cannot
dangle (the target is created first) and it is broken by an atomic write
exactly like a symlink is — repair_sharing() already handles that case.

~/.claude.json is not linked at all on Windows: see main.py, COPY_LIVE_JSON.
"""

import os
import sys

IS_WINDOWS = sys.platform == "win32"

# ERROR_PRIVILEGE_NOT_HELD: symlinks need Developer Mode or an elevated shell.
_ERROR_PRIVILEGE_NOT_HELD = 1314

# What a file shared by hardlink starts with when neither side has it yet.
_EMPTY_CONTENT = {".json": "{}\n"}


def link_dir(target, link):
    if IS_WINDOWS:
        # _winapi.CreateJunction is private CPython API (stable since 3.5 and
        # used by the stdlib's own tests); `cmd /c mklink /J` is the
        # documented equivalent.
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
    else:
        os.symlink(target, link)


def link_file(target, link):
    if not IS_WINDOWS:
        os.symlink(target, link)
        return
    try:
        os.symlink(target, link)
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_PRIVILEGE_NOT_HELD:
            raise
    # A hardlink needs an existing target. Creating it in the pool keeps the
    # promise symlinks make: the first write lands in the pool, not the profile.
    if not os.path.exists(target):
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(_EMPTY_CONTENT.get(os.path.splitext(target)[1], ""))
    os.link(target, link)


def is_link(path):
    """A symlink or a junction — something that points elsewhere."""
    return os.path.islink(path) or os.path.isjunction(path)


def points_to(link, target):
    """Does `link` reach `target`, through a symlink, junction or hardlink?"""
    if is_link(link):
        return os.path.realpath(link) == os.path.realpath(target)
    if os.path.isfile(link) and os.path.isfile(target):
        try:
            return os.path.samefile(link, target)
        except OSError:
            return False
    return False


def read_link(path):
    """Where a link points, without Windows' \\\\?\\ prefix."""
    target = os.readlink(path)
    if target.startswith("\\\\?\\"):
        target = target[4:]
    return target


def remove_link(path):
    """Remove the link itself, never what it points to."""
    os.unlink(path)  # also removes junctions, leaving the target alone
