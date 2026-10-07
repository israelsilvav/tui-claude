"""Pin a profile to one terminal instead of switching ~/.claude for all.

Claude Code reads its whole configuration from CLAUDE_CONFIG_DIR when that
variable is set, ignoring ~/.claude and ~/.claude.json. `tui-claude tmp` uses
that: it opens a shell with the variable pointing at one profile, so every
`claude` started there uses that account while other terminals keep following
the global symlink.

Nothing here depends on prompt_toolkit:

    account link   <profile>/.claude.json -> claude.json, the name Claude Code
                   looks for under CLAUDE_CONFIG_DIR
    pins           _pins/<pid>, one per pinned terminal, so the TUI can tell
                   which profiles are open and refuse to move them
    launch         the argv/env that replace tui-claude with a small sh, which
                   runs the user's shell and reports back when it exits
"""

import contextlib
import os
import shutil
import sys

from . import sharing
from .links import IS_WINDOWS

ACCOUNT_LINK = ".claude.json"
PINS_NAME = "_pins"   # "_" keeps it out of the profile list
CONFIG_ENV = "CLAUDE_CONFIG_DIR"
PROFILE_ENV = "TUI_CLAUDE_PROFILE"

# sh waits for the user's shell, then calls back so the profile's config goes
# to the pool. `trap :` (a no-op handler, not `trap ''`) keeps sh alive through
# Ctrl-C without the shell and everything it starts inheriting an ignored
# SIGINT. The shell's exit status is passed on.
EXIT_SCRIPT = """trap : INT QUIT
"$1"
rc=$?
"$2" -m tui_claude.main _tmp-exit "$3" "$$"
exit $rc
"""


def link_account_json(profile_dir):
    """Point <profile>/.claude.json at claude.json; return an archive dir or None.

    The link is relative, so renaming the profile keeps it valid. Claude Code
    writes through it: its atomic write lands next to the real file, so the
    link survives.
    """
    link = os.path.join(profile_dir, ACCOUNT_LINK)
    target = os.path.join(profile_dir, "claude.json")

    if os.path.islink(link):
        if os.readlink(link) == "claude.json":
            return None
        os.unlink(link)

    archived = None
    if os.path.lexists(link):
        # A real file: Claude Code ran with CLAUDE_CONFIG_DIR on this profile
        # before the link existed. Keep the newer copy, archive the other.
        link_newer = (not os.path.exists(target)
                      or os.path.getmtime(link) > os.path.getmtime(target))
        older = target if link_newer else link
        if os.path.exists(older):
            archived = sharing.archive_dir(profile_dir)
            os.makedirs(archived, exist_ok=True)
            shutil.move(older, os.path.join(archived, os.path.basename(older)))
        if link_newer:
            shutil.move(link, target)

    os.symlink("claude.json", link)
    return archived


# --- pins ----------------------------------------------------------------------

def pins_dir(profiles_dir):
    return os.path.join(profiles_dir, PINS_NAME)


def _start_time(pid):
    """When the process started (Linux, /proc); None elsewhere.

    A pid alone is not proof: once a pinned terminal dies, its pid can be
    reused by an unrelated process. The start time tells the two apart.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            stat = handle.read()
    except OSError:
        return None
    # Field 2 (comm) is in parentheses and may contain spaces; field 22
    # (starttime) is the 20th after the closing parenthesis.
    return stat.rsplit(")", 1)[1].split()[19]


def register_pin(profiles_dir, profile, pid=None):
    """Record that terminal `pid` (default: this process) is pinned to `profile`.

    Written just before exec, which keeps both the pid and its start time.
    """
    pid = os.getpid() if pid is None else pid
    os.makedirs(pins_dir(profiles_dir), exist_ok=True)
    path = os.path.join(pins_dir(profiles_dir), str(pid))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{profile}\n{_start_time(pid) or ''}\n")


def remove_pin(profiles_dir, pid):
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(pins_dir(profiles_dir), str(pid)))


def _alive(pid, start):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass   # alive, just not ours
    return not start or _start_time(pid) == start


def _scan(profiles_dir):
    """(path, profile, alive) for each well-formed pin file."""
    directory = pins_dir(profiles_dir)
    # Never on Windows: tmp mode does not run there, and os.kill(pid, 0) would
    # terminate the process instead of probing it.
    if IS_WINDOWS or not os.path.isdir(directory):
        return []
    found = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            pid = int(name)
            if pid <= 0:
                continue   # 0 and negatives address process groups
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except (ValueError, OSError):
            continue
        profile = lines[0] if lines else ""
        start = lines[1] if len(lines) > 1 else ""
        found.append((path, profile, bool(profile) and _alive(pid, start)))
    return found


def live_pins(profiles_dir):
    """{profile: number of terminals pinned to it right now}."""
    counts = {}
    for _, profile, alive in _scan(profiles_dir):
        if alive:
            counts[profile] = counts.get(profile, 0) + 1
    return counts


def clean_dead_pins(profiles_dir):
    """Drop pins left by terminals that were closed without `exit`."""
    for path, _, alive in _scan(profiles_dir):
        if not alive:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)


# --- launching and leaving ---------------------------------------------------------

def config_env(profile_dir, profile, base=None):
    """Environment for anything that must use `profile` instead of the global link."""
    env = dict(os.environ if base is None else base)
    env[CONFIG_ENV] = os.path.abspath(profile_dir)   # Claude Code wants it absolute
    env[PROFILE_ENV] = profile
    return env


def global_env(base=None):
    """Environment for commands that must follow the global profile, even when
    tui-claude itself was started inside a pinned terminal."""
    env = dict(os.environ if base is None else base)
    env.pop(CONFIG_ENV, None)
    return env


def launch_command(profile_dir, profile, base_env=None):
    """argv and env for os.execve: sh runs the user's shell, then _tmp-exit.

    exec keeps the pid, so the pin registered just before stays valid, and
    the Python process (~26 MB) becomes an sh (~1 MB) for the whole session.
    """
    env = config_env(profile_dir, profile, base_env)
    shell = env.get("SHELL") or "/bin/sh"
    argv = ["/bin/sh", "-c", EXIT_SCRIPT, "tui-claude-tmp", shell, sys.executable, profile]
    return argv, env


def finish(profiles_dir, profile, pid):
    """What a pinned terminal's sh runs once its shell has exited."""
    try:
        if (os.path.isdir(os.path.join(profiles_dir, profile))
                and sharing.is_shared(profiles_dir, profile)):
            sharing.split_claude_json(profiles_dir, profile)
    finally:
        remove_pin(profiles_dir, pid)
