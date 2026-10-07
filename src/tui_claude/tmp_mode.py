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

import os
import shutil

from . import sharing

ACCOUNT_LINK = ".claude.json"


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
