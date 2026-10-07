#!/usr/bin/env python3
import contextlib
import os
import shutil
import subprocess
import sys
import time
from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style

from . import sharing, tmp_mode
from .links import IS_WINDOWS, is_link, link_dir, read_link, remove_link

TEST_DIR = os.environ.get("TUI_CLAUDE_TEST_DIR")
if TEST_DIR:
    PROFILES_DIR = os.path.join(TEST_DIR, "claude-profiles")
    CLAUDE_DIR = os.path.join(TEST_DIR, "claude")
    CLAUDE_JSON = os.path.join(TEST_DIR, "claude.json")
else:
    PROFILES_DIR = os.path.expanduser("~/.claude-profiles")
    CLAUDE_DIR = os.path.expanduser("~/.claude")
    CLAUDE_JSON = os.path.expanduser("~/.claude.json")

# On Windows ~/.claude.json stays a real file and is copied in and out of the
# profile on every switch. A file link there would not survive: Claude Code
# rewrites the file atomically, turning the link back into a plain file that no
# longer belongs to any profile. ~/.claude itself is a junction.
COPY_LIVE_JSON = IS_WINDOWS

# Global State
state = {
    "profiles": [],
    "active_profile": None,
    "selected_index": 0,
    "login_command": "claude auth login",
    "message": "",
    "message_style": "info",
    "sharing": {},       # profile name -> bool, read from the filesystem
    "pool": None,        # summary of the shared pool, or None if there is none
    "pins": {},          # profile name -> terminals pinned with `tui-claude tmp`
    "tmp_mode": False,   # True when started as `tui-claude tmp`
}

USAGE = """\
usage: tui-claude            manage profiles; Enter switches the global profile
       tui-claude tmp        pick a profile for this terminal only
       tui-claude tmp NAME   use profile NAME in this terminal only"""

UNMANAGED = ("~/.claude has not been migrated yet. Close every "
             "Claude Code session and restart tui-claude.")

def init_profiles():
    """Ensure the profile directories exist and migrate existing ~/.claude directory."""
    if not os.path.exists(PROFILES_DIR):
        try:
            os.makedirs(PROFILES_DIR, exist_ok=True)
        except Exception as e:
            state["message"] = f"Failed to create profiles dir: {e}"
            state["message_style"] = "error"
            return

    # Check if ~/.claude exists
    if os.path.lexists(CLAUDE_DIR):
        if is_link(CLAUDE_DIR):
            # Already a symlink, check where it points
            pass
        else:
            # It is a real directory, migrate it to ~/.claude-profiles/default
            default_dir = os.path.join(PROFILES_DIR, "default")
            try:
                if not os.path.lexists(default_dir):
                    move_claude_dir(default_dir)
                    state["message"] = "Migrated existing ~/.claude to 'default' profile"
                else:
                    backup_dir = os.path.join(PROFILES_DIR, f"backup-existing-{int(time.time())}")
                    move_claude_dir(backup_dir)
                    state["message"] = f"Migrated existing ~/.claude to backup: {os.path.basename(backup_dir)}"
                link_dir(default_dir, CLAUDE_DIR)
            except Exception as e:
                state["message"] = f"Failed to migrate ~/.claude: {e}"
                state["message_style"] = "error"
                # Leave no trace of a migration that did not happen.
                with contextlib.suppress(OSError):
                    os.rmdir(PROFILES_DIR)  # only succeeds if still empty
                return
    else:
        # Create default profile and symlink
        default_dir = os.path.join(PROFILES_DIR, "default")
        try:
            os.makedirs(default_dir, exist_ok=True)
            link_dir(default_dir, CLAUDE_DIR)
            state["message"] = "Initialized 'default' profile"
        except Exception as e:
            state["message"] = f"Initialization error: {e}"
            state["message_style"] = "error"

    # ~/.claude.json holds the active account (oauthAccount, userID, etc.) but
    # lives outside ~/.claude, so it must be migrated/symlinked per profile too,
    # otherwise switching profiles keeps logging in as the same account.
    active_profile_dir = None
    if is_link(CLAUDE_DIR):
        try:
            active_profile_dir = os.path.abspath(os.path.expanduser(read_link(CLAUDE_DIR)))
        except Exception:
            pass
    if not active_profile_dir:
        active_profile_dir = os.path.join(PROFILES_DIR, "default")

    profile_json = os.path.join(active_profile_dir, "claude.json")
    if COPY_LIVE_JSON:
        # The live file is the active profile's real copy; only seed the
        # profile from it the first time.
        if os.path.isfile(CLAUDE_JSON) and not os.path.lexists(profile_json):
            try:
                shutil.copy2(CLAUDE_JSON, profile_json)
            except Exception as e:
                state["message"] = f"Failed to migrate ~/.claude.json: {e}"
                state["message_style"] = "error"
    elif os.path.lexists(CLAUDE_JSON):
        if not os.path.islink(CLAUDE_JSON):
            try:
                if not os.path.lexists(profile_json):
                    shutil.move(CLAUDE_JSON, profile_json)
                else:
                    backup_json = os.path.join(PROFILES_DIR, f"backup-existing-claude.json-{int(time.time())}")
                    shutil.move(CLAUDE_JSON, backup_json)
                os.symlink(profile_json, CLAUDE_JSON)
            except Exception as e:
                state["message"] = f"Failed to migrate ~/.claude.json: {e}"
                state["message_style"] = "error"
    else:
        try:
            os.symlink(profile_json, CLAUDE_JSON)
        except Exception as e:
            state["message"] = f"Initialization error (claude.json): {e}"
            state["message_style"] = "error"

def move_claude_dir(dest):
    """Move the real ~/.claude out of the way, all or nothing.

    shutil.move falls back to copy + delete when a rename fails. On Windows a
    rename fails whenever Claude Code holds a file open, and the delete would
    then stop halfway, leaving ~/.claude partly gone. A plain rename either
    happens or does not.
    """
    if not IS_WINDOWS:
        shutil.move(CLAUDE_DIR, dest)
        return
    try:
        os.rename(CLAUDE_DIR, dest)
    except PermissionError as e:
        raise RuntimeError("~/.claude is in use. Close every Claude Code "
                           "session and run tui-claude again.") from e

def linked_profile():
    """The profile ~/.claude points at right now, or None."""
    if not is_link(CLAUDE_DIR):
        return None
    try:
        parent, name = os.path.split(os.path.realpath(CLAUDE_DIR))
    except OSError:
        return None
    if os.path.normcase(parent) != os.path.normcase(os.path.realpath(PROFILES_DIR)):
        return None
    return name

def capture_live_json():
    """Copy mode: save the live ~/.claude.json back into its profile.

    Claude Code writes to the live file, so the profile's copy is stale until
    this runs. Anything that reads <profile>/claude.json must call it first.
    """
    profile = linked_profile()
    if COPY_LIVE_JSON and profile and os.path.isfile(CLAUDE_JSON):
        shutil.copy2(CLAUDE_JSON, os.path.join(PROFILES_DIR, profile, "claude.json"))

def place_live_json(profile_dir):
    """Point (or, in copy mode, copy) ~/.claude.json at a profile's own."""
    profile_json = os.path.join(profile_dir, "claude.json")
    if COPY_LIVE_JSON:
        if os.path.isfile(profile_json):
            shutil.copy2(profile_json, CLAUDE_JSON)
        elif os.path.lexists(CLAUDE_JSON):
            # A profile with no config yet must not inherit another account's.
            os.remove(CLAUDE_JSON)
        return
    if os.path.lexists(CLAUDE_JSON):
        os.unlink(CLAUDE_JSON)
    os.symlink(profile_json, CLAUDE_JSON)

def refresh_profiles():
    """Scan profiles and active state."""
    init_profiles()

    # List subdirectories of ~/.claude-profiles
    profiles = []
    if os.path.exists(PROFILES_DIR):
        for name in os.listdir(PROFILES_DIR):
            full_path = os.path.join(PROFILES_DIR, name)
            # "_" is reserved for internals such as the shared pool
            if os.path.isdir(full_path) and not name.startswith("_"):
                profiles.append(name)
    state["profiles"] = sorted(profiles)

    # An atomic write by Claude Code can replace a symlink with a real file,
    # dropping that one item out of the pool without raising anything. Repair
    # first, then read the state: repairs promote content into the pool and
    # would otherwise leave the table showing a stale size and count.
    healed = []
    for name in state["profiles"]:
        healed += sharing.repair_sharing(PROFILES_DIR, name)
    if healed and not state["message"]:
        state["message"] = f"Re-linked to the shared pool: {', '.join(sorted(set(healed)))}."
        state["message_style"] = "info"

    # A pinned terminal closed without `exit` leaves its pin behind.
    tmp_mode.clean_dead_pins(PROFILES_DIR)
    state["pins"] = tmp_mode.live_pins(PROFILES_DIR)

    state["sharing"] = {name: sharing.is_shared(PROFILES_DIR, name)
                        for name in state["profiles"]}
    state["pool"] = sharing.pool_summary(PROFILES_DIR)

    # Find active profile
    state["active_profile"] = linked_profile()

    # Normalize selection index
    if not state["profiles"]:
        state["selected_index"] = 0
    elif state["selected_index"] >= len(state["profiles"]):
        state["selected_index"] = len(state["profiles"]) - 1

def refuse_pinned(name):
    """A profile open in a pinned terminal must not move under it: its
    CLAUDE_CONFIG_DIR would point at a path that no longer exists."""
    count = tmp_mode.live_pins(PROFILES_DIR).get(name, 0)
    if not count:
        return False
    state["message"] = (f"Profile '{name}' is in use by {count} terminal(s) (tmp). "
                        "Type 'exit' in them first.")
    state["message_style"] = "error"
    return True

def switch_profile(name):
    """Switch the active symlink to the chosen profile."""
    profile_dir = os.path.join(PROFILES_DIR, name)
    if not os.path.exists(profile_dir):
        state["message"] = f"Profile '{name}' does not exist."
        state["message_style"] = "error"
        return False

    try:
        # The only instant where both profiles are known and neither is in use:
        # hand the pool what the outgoing profile changed, then dress the
        # incoming one with it. No-op unless the profiles share.
        capture_live_json()
        sharing.sync_on_switch(
            PROFILES_DIR, state["active_profile"], name,
            entering_in_use=bool(tmp_mode.live_pins(PROFILES_DIR).get(name)))

        if os.path.lexists(CLAUDE_DIR):
            remove_link(CLAUDE_DIR)
        link_dir(profile_dir, CLAUDE_DIR)

        place_live_json(profile_dir)

        state["message"] = f"Switched to profile '{name}'."
        state["message_style"] = "success"
        return True
    except Exception as e:
        state["message"] = f"Failed to switch profile: {e}"
        state["message_style"] = "error"
        return False

def add_profile(name, share=False, activate=None):
    """Create a new profile folder, optionally joining the shared pool.

    It becomes the global profile unless `activate` says otherwise; by
    default tmp mode leaves the global profile alone.
    """
    if activate is None:
        activate = not state["tmp_mode"]
    name = "".join([c for c in name if c.isalnum() or c in ("-", "_")]).strip()
    name = name.lstrip("_")  # "_" is reserved for internals
    if not name:
        state["message"] = "Invalid profile name."
        state["message_style"] = "error"
        return False

    profile_dir = os.path.join(PROFILES_DIR, name)
    if os.path.lexists(profile_dir):
        state["message"] = f"Profile '{name}' already exists."
        state["message_style"] = "error"
        return False

    try:
        os.makedirs(profile_dir, exist_ok=True)
        if share:
            sharing.enable_sharing(PROFILES_DIR, name)
        where = "sharing data" if share else "with its own data"
        if activate:
            switch_profile(name)
            state["message"] = f"Created and activated profile '{name}' ({where})."
        else:
            state["message"] = f"Created profile '{name}' ({where})."
        state["message_style"] = "success"
        return True
    except Exception as e:
        state["message"] = f"Failed to create profile: {e}"
        state["message_style"] = "error"
        return False

def rename_profile(old, new):
    """Rename a profile, keeping it active and shared if it was.

    The links inside a shared profile point into the pool by absolute path (or
    are hardlinks, on Windows), so they survive the rename untouched. Only the
    two links that select the active profile have to be re-pointed.
    """
    new = "".join([c for c in new if c.isalnum() or c in ("-", "_")]).strip()
    new = new.lstrip("_")  # "_" is reserved for internals
    if not new:
        state["message"] = "Invalid profile name."
        state["message_style"] = "error"
        return False
    if new == old:
        state["message"] = "Name unchanged."
        state["message_style"] = "info"
        return False

    old_dir = os.path.join(PROFILES_DIR, old)
    new_dir = os.path.join(PROFILES_DIR, new)
    if not os.path.isdir(old_dir):
        state["message"] = f"Profile '{old}' does not exist."
        state["message_style"] = "error"
        return False
    if refuse_pinned(old):
        return False
    if os.path.lexists(new_dir):
        state["message"] = f"Profile '{new}' already exists."
        state["message_style"] = "error"
        return False

    try:
        was_active = (state["active_profile"] == old)
        if was_active:
            # Copy mode: the live file is newer than the profile's copy, and
            # must be saved while the link still resolves to the old name.
            capture_live_json()
        os.rename(old_dir, new_dir)

        if was_active:
            # Re-point directly rather than calling switch_profile: the profile
            # it would treat as "leaving" no longer exists under that name.
            if os.path.lexists(CLAUDE_DIR):
                remove_link(CLAUDE_DIR)
            link_dir(new_dir, CLAUDE_DIR)
            place_live_json(new_dir)
            state["active_profile"] = new

        state["message"] = f"Renamed '{old}' to '{new}'."
        state["message_style"] = "success"
        return True
    except Exception as e:
        state["message"] = f"Failed to rename profile: {e}"
        state["message_style"] = "error"
        return False

def delete_profile(name):
    """Remove a profile and fallback to another if active."""
    profile_dir = os.path.join(PROFILES_DIR, name)
    if not os.path.exists(profile_dir):
        state["message"] = f"Profile '{name}' does not exist."
        state["message_style"] = "error"
        return False
    if refuse_pinned(name):
        return False

    try:
        is_active = (state["active_profile"] == name)
        if is_active:
            if os.path.lexists(CLAUDE_DIR):
                remove_link(CLAUDE_DIR)
            if os.path.lexists(CLAUDE_JSON):
                os.unlink(CLAUDE_JSON)

        # rmtree does not follow symlinks or junctions, but this is the one
        # operation that must never reach into the pool: unlink first.
        for item in sharing.SHARED_DIRS + sharing.SHARED_FILES:
            path = os.path.join(profile_dir, item)
            if is_link(path):
                remove_link(path)
        shutil.rmtree(profile_dir)
        state["message"] = f"Deleted profile '{name}'."
        state["message_style"] = "success"

        # Fallback if active was deleted
        if is_active:
            refresh_profiles()
            if state["profiles"]:
                switch_profile(state["profiles"][0])
            else:
                add_profile("default", activate=True)   # ~/.claude must point somewhere
        return True
    except Exception as e:
        state["message"] = f"Failed to delete profile: {e}"
        state["message_style"] = "error"
        return False

# Styling System
app_style = Style.from_dict({
    "title": "bg:#5f5faf fg:#ffffff bold",
    "subtitle": "fg:#8a8a8a italic",
    "border": "fg:#444444",
    "active": "fg:#00ff00 bold",
    "inactive": "fg:#8a8a8a",
    "shared": "fg:#00afaf",
    "selected": "bg:#3a3a3a fg:#ffffff bold",
    "active-selected": "bg:#3a3a3a fg:#00ff00 bold",
    "info": "fg:#00afff",
    "success": "fg:#00ff00",
    "error": "fg:#ff5f5f bold",
    "help-key": "fg:#ffd700 bold",
    "help-desc": "fg:#d7d7d7",
})

# Keyboard Bindings
kb = KeyBindings()

def refuse_unmanaged(event):
    """~/.claude is still a real directory: the migration has not happened, so
    switching, adding or sharing would act on a half-initialised layout."""
    if os.path.lexists(CLAUDE_DIR) and not is_link(CLAUDE_DIR):
        state["message"] = UNMANAGED
        state["message_style"] = "error"
        event.app.invalidate()
        return True
    return False

@kb.add("q")
@kb.add("c-c")
def exit_app(event):
    event.app.exit()

@kb.add("up")
@kb.add("k")
def move_up(event):
    if state["profiles"]:
        state["selected_index"] = (state["selected_index"] - 1) % len(state["profiles"])
        event.app.invalidate()

@kb.add("down")
@kb.add("j")
def move_down(event):
    if state["profiles"]:
        state["selected_index"] = (state["selected_index"] + 1) % len(state["profiles"])
        event.app.invalidate()

@kb.add("enter")
def do_switch(event):
    if refuse_unmanaged(event):
        return
    if state["profiles"]:
        selected = state["profiles"][state["selected_index"]]
        if state["tmp_mode"]:
            event.app.exit(result=selected)   # main() pins it once the screen is gone
            return
        switch_profile(selected)
        refresh_profiles()
        event.app.invalidate()

@kb.add("a")
def do_add(event):
    if refuse_unmanaged(event):
        return
    async def add_flow():
        def prompt_add():
            print("\n" * 2)
            print("=== ADD NEW PROFILE ===")
            name = input("Enter new profile name: ").strip()
            if not name:
                state["message"] = "Profile creation cancelled."
                state["message_style"] = "info"
                time.sleep(1.2)
                return

            # A new profile is as often "another one of mine" as it is "a
            # client account, keep it separate", so ask rather than assume.
            share = False
            pool = sharing.pool_summary(PROFILES_DIR)
            if pool is not None:
                print(f"\nA shared pool exists ({pool['conversations']} conversations, "
                      f"{sharing.human_size(pool['bytes'])}).")
                print("Sharing gives the new profile those conversations, memory and")
                print("settings. Its login stays separate either way.")
                share = input(f"Share data with the pool? (y/N): ").strip().lower() == "y"
            add_profile(name, share=share)
            time.sleep(1.2)

        await run_in_terminal(prompt_add)
        refresh_profiles()
        event.app.invalidate()

    import asyncio
    asyncio.create_task(add_flow())

def rename_flow(selected, ask=input, say=print):
    """Ask for a new name and apply it. Returns True if the profile moved."""
    if refuse_pinned(selected):
        return False
    say(f"=== RENAME PROFILE: {selected} ===\n")
    say("The account, conversations and settings all stay with the profile;")
    say("only its name changes.\n")
    new = ask(f"New name for '{selected}' (blank cancels): ").strip()
    if not new:
        state["message"] = "Rename cancelled."
        state["message_style"] = "info"
        return False
    return rename_profile(selected, new)


def remove_flow(selected, ask=input, say=print):
    """Confirm a deletion, spelling out what is lost. Returns True if removed."""
    if refuse_pinned(selected):
        return False
    say(f"=== REMOVE PROFILE: {selected} ===\n")
    say(f"This deletes the credentials and settings of '{selected}'.")
    if sharing.is_shared(PROFILES_DIR, selected):
        pool = sharing.pool_summary(PROFILES_DIR)
        count = pool["conversations"] if pool else 0
        say(f"Its {count} conversation(s) live in the shared pool and stay there,")
        say("along with any other profile using it.")
    else:
        own = os.path.join(PROFILES_DIR, selected, "projects")
        count = sum(len([f for f in files if f.endswith(".jsonl")])
                    for _, _, files in os.walk(own))
        say(f"This profile is isolated, so its {count} conversation(s) go with it.")
    say(f"\nTo rename it instead, cancel and press [N].\n")

    confirm = ask(f"Type 'y' to remove '{selected}', anything else cancels: ").strip().lower()
    if confirm != "y":
        state["message"] = "Profile removal cancelled."
        state["message_style"] = "info"
        return False
    return delete_profile(selected)


def _run_profile_flow(event, flow):
    """Drive one of the prompt flows above in the terminal, then redraw."""
    if refuse_unmanaged(event):
        return
    if not state["profiles"]:
        return
    selected = state["profiles"][state["selected_index"]]

    async def run_flow():
        def run():
            print("\n" * 2)
            try:
                flow(selected)
            except Exception as exc:
                state["message"] = f"Failed: {exc}"
                state["message_style"] = "error"
            time.sleep(1.2)

        await run_in_terminal(run)
        refresh_profiles()
        event.app.invalidate()

    import asyncio
    asyncio.create_task(run_flow())


@kb.add("n")
@kb.add("f2")
def do_rename(event):
    _run_profile_flow(event, rename_flow)


@kb.add("r")
def do_remove(event):
    _run_profile_flow(event, remove_flow)

def enable_sharing_flow(selected, ask=input, say=print):
    """Ask, then move `selected` into the shared pool. Returns True if it did.

    `ask` and `say` are injected so the whole dialogue can be driven by tests
    without a terminal.
    """
    if refuse_pinned(selected):
        return False
    pool = sharing.pool_summary(PROFILES_DIR)
    say(f"=== SHARE DATA: {selected} ===\n")
    if pool is None:
        say("No shared pool exists yet.")
        say(f"'{selected}' will become the pool: its conversations, memory,")
        say("settings and skills stay exactly as they are, and any other")
        say("profile you share later will see them.")
    else:
        # The pool outlives its last member by design, so it can exist with
        # nobody in it.
        others = [p for p in state["profiles"]
                  if p != selected and state["sharing"].get(p)]
        used_by = f"used by {', '.join(others)}" if others else \
            "currently used by no profile"
        say(f"Shared pool: {pool['conversations']} conversations, "
            f"{sharing.human_size(pool['bytes'])}, {used_by}.")
        say(f"\n'{selected}' will join it, bringing its own data in:")
        say("  - conversations, memory and snapshots merge (names never collide)")
        say("  - a settings file that disagrees with the pool's is kept aside")
        say("    in an _archive folder, never deleted")
    say("\nCredentials and account identity are NOT shared.\n")

    if ask(f"Share data for '{selected}'? (y/N): ").strip().lower() != "y":
        state["message"] = "Sharing unchanged."
        state["message_style"] = "info"
        return False

    capture_live_json()  # split_claude_json reads the profile's copy
    result = sharing.enable_sharing(PROFILES_DIR, selected)
    if selected == state["active_profile"]:
        switch_profile(selected)  # refresh the symlinks in place

    parts = ["seeded the pool"] if result["seeded"] else []
    if result["merged"]:
        parts.append(f"{result['merged']} file(s) merged")
    if result["archived"]:
        parts.append(f"{result['archived']} kept in {os.path.basename(result['archive'])}")
    if result["withheld"]:
        parts.append(f"{len(result['withheld'])} account key(s) withheld")
    state["message"] = f"'{selected}' now shares data" + (
        " (" + ", ".join(parts) + ")." if parts else ".")
    state["message_style"] = "success"
    return True


def disable_sharing_flow(selected, ask=input, say=print):
    """Ask what to keep, then take `selected` out of the pool."""
    if refuse_pinned(selected):
        return False
    pool = sharing.pool_summary(PROFILES_DIR)
    size = sharing.human_size(pool["bytes"]) if pool else "0 B"
    count = pool["conversations"] if pool else 0
    say(f"=== STOP SHARING: {selected} ===\n")
    say(f"'{selected}' currently reads {count} conversation(s) from the")
    say(f"shared pool ({size}). What should it keep?\n")
    say(f"  [k] Keep a copy  - fork the pool into '{selected}' and diverge")
    say(f"                     from there (uses another {size} on disk)")
    say(f"  [e] Start empty  - '{selected}' begins with no conversations")
    say( "  [c] Cancel\n")
    say("Either way the pool itself is untouched, and other profiles")
    say("keep reading it normally.\n")

    choice = ask("Choice [k/e/c]: ").strip().lower()
    if choice not in ("k", "e"):
        state["message"] = "Sharing unchanged."
        state["message_style"] = "info"
        return False

    capture_live_json()
    result = sharing.disable_sharing(PROFILES_DIR, selected, take_copy=(choice == "k"))
    if selected == state["active_profile"]:
        switch_profile(selected)
    if choice == "k":
        state["message"] = (f"'{selected}' is isolated with its own copy "
                            f"({result['copied']} file(s)).")
    else:
        state["message"] = f"'{selected}' is isolated and starts empty."
    state["message_style"] = "success"
    return True


@kb.add("s")
def do_toggle_sharing(event):
    """Move the selected profile in or out of the shared data pool."""
    if refuse_unmanaged(event):
        return
    if not state["profiles"]:
        return

    selected = state["profiles"][state["selected_index"]]
    currently_shared = state["sharing"].get(selected, False)

    async def toggle_flow():
        def run():
            print("\n" * 2)
            try:
                if currently_shared:
                    disable_sharing_flow(selected)
                else:
                    enable_sharing_flow(selected)
            except Exception as exc:
                state["message"] = f"Sharing failed: {exc}"
                state["message_style"] = "error"
            time.sleep(1.2)

        await run_in_terminal(run)
        refresh_profiles()
        event.app.invalidate()

    import asyncio
    asyncio.create_task(toggle_flow())

@kb.add("c")
def do_change_command(event):
    async def change_cmd_flow():
        def prompt_change():
            print("\n" * 2)
            print("=== CHANGE CLAUDE LOGIN COMMAND ===")
            print(f"Current command: {state['login_command']}")
            new_cmd = input("Enter new login command: ").strip()
            if new_cmd:
                state["login_command"] = new_cmd
                state["message"] = f"Login command updated to '{new_cmd}'"
                state["message_style"] = "success"
            else:
                state["message"] = "Command change cancelled."
                state["message_style"] = "info"
            time.sleep(1.2)

        await run_in_terminal(prompt_change)
        event.app.invalidate()

    import asyncio
    asyncio.create_task(change_cmd_flow())

def login_env(name):
    """Login must land in the profile it runs for, never in the one a pinned
    terminal happens to point CLAUDE_CONFIG_DIR at."""
    if state["tmp_mode"]:
        return tmp_mode.config_env(os.path.join(PROFILES_DIR, name), name)
    return tmp_mode.global_env()

def run_login(selected, run=subprocess.run, say=print):
    """Get `selected` ready and run the login command for it.

    The global mode activates the profile first. tmp mode leaves the global
    profile alone and points CLAUDE_CONFIG_DIR at the selected one instead.
    """
    if state["tmp_mode"]:
        say(f"Preparing '{selected}' for this terminal only...")
        archive = prepare_tmp(selected)
        if archive:
            say(f"Kept a copy of the previous account files in {archive}")
    else:
        say(f"Making sure '{selected}' is active...")
        switch_profile(selected)
        refresh_profiles()

    say(f"Running login command: {state['login_command']}")
    say("-" * 40)
    try:
        # Run the login command interactively
        run(state["login_command"], shell=True, check=True, env=login_env(selected))
        say("-" * 40)
        say("Login command finished successfully.")
    except subprocess.CalledProcessError as e:
        say("-" * 40)
        say(f"Command failed with exit code {e.returncode}")
    except Exception as e:
        say("-" * 40)
        say(f"Failed to execute command: {e}")

@kb.add("l")
def do_login(event):
    if refuse_unmanaged(event):
        return
    if not state["profiles"]:
        return

    selected = state["profiles"][state["selected_index"]]
    async def login_flow():
        def run_it():
            print("\n" * 2)
            print(f"=== RUNNING LOGIN FOR PROFILE: {selected} ===")
            run_login(selected)
            print("Press Enter to return to the TUI...")
            input()

        await run_in_terminal(run_it)
        refresh_profiles()
        event.app.invalidate()

    import asyncio
    asyncio.create_task(login_flow())

# Render Logic
def get_screen_text():
    tokens = []

    # Title Bar
    tokens.append(("class:title", " ┌────────────────────────────────────────────────────────┐ \n"))
    tokens.append(("class:title", " │               CLAUDE CODE PROFILE MANAGER              │ \n"))
    tokens.append(("class:title", " └────────────────────────────────────────────────────────┘ \n"))
    tokens.append(("", "\n"))

    # Active Profile Banner
    active = state["active_profile"] or "None (Invalid Symlink)"
    tokens.append(("", " Active profile: "))
    tokens.append(("class:active", f"● {active}\n"))
    tokens.append(("", " Config location: "))
    tokens.append(("class:subtitle", f"{CLAUDE_DIR} -> {read_link(CLAUDE_DIR) if is_link(CLAUDE_DIR) else 'Not a symlink'}\n"))
    tokens.append(("", " Account cache: "))
    if COPY_LIVE_JSON:
        where = (f"copied to/from {os.path.join(PROFILES_DIR, state['active_profile'], 'claude.json')}"
                 if state["active_profile"] else "Not managed")
    else:
        where = read_link(CLAUDE_JSON) if os.path.islink(CLAUDE_JSON) else "Not a symlink"
    tokens.append(("class:subtitle", f"{CLAUDE_JSON} -> {where}\n"))
    tokens.append(("", f" Login command: {state['login_command']}\n"))
    if state["tmp_mode"]:
        tokens.append(("class:info", " Mode: this terminal only\n"))
    pinned_here = os.environ.get(tmp_mode.PROFILE_ENV)
    if pinned_here:
        tokens.append(("", " This terminal: "))
        tokens.append(("class:shared", f"{pinned_here} (tmp)\n"))
    tokens.append(("", "\n"))

    # Profile List Table
    tokens.append(("class:border", " ┌──────────────────────────────┬───────────────┬──────────────┐\n"))
    tokens.append(("class:border", " │ Profile Name                 │ Status        │ Data         │\n"))
    tokens.append(("class:border", " ├──────────────────────────────┼───────────────┼──────────────┤\n"))

    if not state["profiles"]:
        tokens.append(("class:border", " │ "))
        tokens.append(("class:error", "No profiles found.           "))
        tokens.append(("class:border", " │               │              │\n"))
    else:
        for idx, name in enumerate(state["profiles"]):
            is_selected = (idx == state["selected_index"])
            is_active = (name == state["active_profile"])

            # Format row
            row_style = ""
            status_style = ""

            if is_selected and is_active:
                row_style = "class:active-selected"
                status_style = "class:active-selected"
            elif is_selected:
                row_style = "class:selected"
                status_style = "class:selected"
            elif is_active:
                row_style = "class:active"
                status_style = "class:active"
            else:
                row_style = "class:inactive"
                status_style = "class:inactive"

            status_text = "● ACTIVE" if is_active else "  inactive"
            pins = state["pins"].get(name, 0)
            if pins:
                status_text += f" ◆{min(pins, 9)}"   # 13 columns at most
            is_sharing = state["sharing"].get(name, False)
            data_text = "⇄ shared" if is_sharing else "⊘ isolated"
            data_style = row_style if is_selected else (
                "class:shared" if is_sharing else "class:inactive")

            tokens.append(("class:border", " │ "))
            tokens.append((row_style, f"{name:<28}"))
            tokens.append(("class:border", " │ "))
            tokens.append((status_style, f"{status_text:<13}"))
            tokens.append(("class:border", " │ "))
            tokens.append((data_style, f"{data_text:<12}"))
            tokens.append(("class:border", " │\n"))

    tokens.append(("class:border", " └──────────────────────────────┴───────────────┴──────────────┘\n"))

    if state["pool"]:
        shared_count = sum(1 for v in state["sharing"].values() if v)
        tokens.append(("class:subtitle",
                       f" Shared pool: {state['pool']['conversations']} conversations, "
                       f"{sharing.human_size(state['pool']['bytes'])}, "
                       f"{shared_count} profile(s)\n"))
    tokens.append(("", "\n"))

    # Message Bar
    if state["message"]:
        tokens.append(("class:border", " Message: "))
        tokens.append((f"class:{state['message_style']}", f"{state['message']}\n"))
        tokens.append(("", "\n"))

    # Help / Footer
    tokens.append(("class:help-key", " [Enter]"))
    tokens.append(("class:help-desc", " Use Here  " if state["tmp_mode"] else " Switch Profile  "))
    tokens.append(("class:help-key", " [A]"))
    tokens.append(("class:help-desc", " Add  "))
    tokens.append(("class:help-key", " [N]"))
    tokens.append(("class:help-desc", " Rename  "))
    tokens.append(("class:help-key", " [R]"))
    tokens.append(("class:help-desc", " Remove  "))
    tokens.append(("class:help-key", " [L]"))
    tokens.append(("class:help-desc", " Login  "))
    tokens.append(("class:help-key", " [S]"))
    tokens.append(("class:help-desc", " Share Data  "))
    tokens.append(("class:help-key", " [C]"))
    tokens.append(("class:help-desc", " Set Command  "))
    tokens.append(("class:help-key", " [Q]"))
    tokens.append(("class:help-desc", " Quit\n"))

    return tokens

# tmp mode: one terminal pinned to one profile

def tmp_refusal():
    """Why this terminal cannot be pinned, or None."""
    if IS_WINDOWS:
        return "tmp mode is not supported on Windows yet."
    pinned = os.environ.get(tmp_mode.PROFILE_ENV)
    if pinned:
        return f"This terminal already uses '{pinned}'. Type 'exit' first."
    return None


def prepare_tmp(name):
    """Make a profile usable through CLAUDE_CONFIG_DIR.

    Returns the directory where files were set aside, or None.
    """
    profile_dir = os.path.join(PROFILES_DIR, name)
    linked = tmp_mode.link_account_json(profile_dir)
    archive = linked["archive"]
    if linked["promoted"] and sharing.is_shared(PROFILES_DIR, name):
        # claude.json now comes from outside: adopt it instead of diffing it
        # against the old baseline, which would strip the pool. Keep a copy
        # of the pool first, so even a mistake here stays recoverable.
        archive = archive or sharing.archive_dir(profile_dir)
        os.makedirs(archive, exist_ok=True)
        pool_config = os.path.join(sharing.pool_dir(PROFILES_DIR), "claude.shared.json")
        if os.path.exists(pool_config):
            shutil.copy2(pool_config, archive)
        sharing.adopt_claude_json(PROFILES_DIR, name)
    in_use = (name == state["active_profile"]
              or bool(tmp_mode.live_pins(PROFILES_DIR).get(name)))
    sharing.prepare_profile(PROFILES_DIR, name, in_use=in_use)
    return archive


def pin_here(name):
    """Prepare `name`, register this terminal and exec into the pinned shell."""
    archive = prepare_tmp(name)
    tmp_mode.register_pin(PROFILES_DIR, name)
    argv, env = tmp_mode.launch_command(os.path.join(PROFILES_DIR, name), name)
    if archive:
        print(f"Kept a copy of the previous account files in {archive}")
    print(f"This terminal now uses profile '{name}'.")
    if state["active_profile"]:
        print(f"Type 'exit' to return to the global profile ('{state['active_profile']}').")
    else:
        print("Type 'exit' to leave.")
    sys.stdout.flush()
    tmp_mode.restore_signals()
    os.execve(argv[0], argv, env)


def run_tmp(name=None):
    refusal = tmp_refusal()
    if refusal is None:
        refresh_profiles()   # migration, repair, dead pins, active profile
        if os.path.lexists(CLAUDE_DIR) and not is_link(CLAUDE_DIR):
            refusal = UNMANAGED
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    if name is None:
        state["tmp_mode"] = True
        name = run_tui()
        if name is None:
            return 0
        refresh_profiles()   # the screen may have added, renamed or shared profiles
    elif name not in state["profiles"]:
        print(f"Profile '{name}' does not exist.", file=sys.stderr)
        return 1
    pin_here(name)
    return 0


def tmp_exit(name, pid):
    """`_tmp-exit`: run by a pinned terminal's sh after its shell exits."""
    try:
        pid = int(pid)
    except ValueError:
        return 2
    try:
        tmp_mode.finish(PROFILES_DIR, name, pid)
    except Exception as exc:
        print(f"tui-claude: could not return '{name}' settings to the pool: {exc}",
              file=sys.stderr)
    back = linked_profile()
    print(f"Back to the global profile '{back}'." if back else "Back to the global profile.")
    return 0


def run_tui():
    """The full-screen TUI. Returns the profile picked in tmp mode, else None."""
    refresh_profiles()
    content = FormattedTextControl(get_screen_text)
    body = Window(content=content)
    layout = Layout(body)

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=app_style,
        full_screen=True
    )
    return app.run()


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        run_tui()
        return 0
    if args in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if args[0] == "tmp" and len(args) <= 2:
        return run_tmp(args[1] if len(args) == 2 else None)
    if args[0] == "_tmp-exit" and len(args) == 3:
        return tmp_exit(args[1], args[2])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
