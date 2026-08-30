#!/usr/bin/env python3
import os
import shutil
import time
import subprocess
from prompt_toolkit import Application
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style

from . import sharing

TEST_DIR = os.environ.get("TUI_CLAUDE_TEST_DIR")
if TEST_DIR:
    PROFILES_DIR = os.path.join(TEST_DIR, "claude-profiles")
    CLAUDE_DIR = os.path.join(TEST_DIR, "claude")
    CLAUDE_JSON = os.path.join(TEST_DIR, "claude.json")
else:
    PROFILES_DIR = os.path.expanduser("~/.claude-profiles")
    CLAUDE_DIR = os.path.expanduser("~/.claude")
    CLAUDE_JSON = os.path.expanduser("~/.claude.json")

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
}

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
        if os.path.islink(CLAUDE_DIR):
            # Already a symlink, check where it points
            pass
        else:
            # It is a real directory, migrate it to ~/.claude-profiles/default
            default_dir = os.path.join(PROFILES_DIR, "default")
            try:
                if not os.path.lexists(default_dir):
                    shutil.move(CLAUDE_DIR, default_dir)
                    state["message"] = "Migrated existing ~/.claude to 'default' profile"
                else:
                    backup_dir = os.path.join(PROFILES_DIR, f"backup-existing-{int(time.time())}")
                    shutil.move(CLAUDE_DIR, backup_dir)
                    state["message"] = f"Migrated existing ~/.claude to backup: {os.path.basename(backup_dir)}"
                os.symlink(default_dir, CLAUDE_DIR)
            except Exception as e:
                state["message"] = f"Failed to migrate ~/.claude: {e}"
                state["message_style"] = "error"
    else:
        # Create default profile and symlink
        default_dir = os.path.join(PROFILES_DIR, "default")
        try:
            os.makedirs(default_dir, exist_ok=True)
            os.symlink(default_dir, CLAUDE_DIR)
            state["message"] = "Initialized 'default' profile"
        except Exception as e:
            state["message"] = f"Initialization error: {e}"
            state["message_style"] = "error"

    # ~/.claude.json holds the active account (oauthAccount, userID, etc.) but
    # lives outside ~/.claude, so it must be migrated/symlinked per profile too,
    # otherwise switching profiles keeps logging in as the same account.
    active_profile_dir = None
    if os.path.islink(CLAUDE_DIR):
        try:
            active_profile_dir = os.path.abspath(os.path.expanduser(os.readlink(CLAUDE_DIR)))
        except Exception:
            pass
    if not active_profile_dir:
        active_profile_dir = os.path.join(PROFILES_DIR, "default")

    profile_json = os.path.join(active_profile_dir, "claude.json")
    if os.path.lexists(CLAUDE_JSON):
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

    state["sharing"] = {name: sharing.is_shared(PROFILES_DIR, name)
                        for name in state["profiles"]}
    state["pool"] = sharing.pool_summary(PROFILES_DIR)

    # An atomic write by Claude Code can replace a symlink with a real file,
    # dropping that one item out of the pool without raising anything. Put it
    # back, keeping whichever content is newer.
    healed = []
    for name in state["profiles"]:
        healed += sharing.repair_sharing(PROFILES_DIR, name)
    if healed and not state["message"]:
        state["message"] = f"Re-linked to the shared pool: {', '.join(sorted(set(healed)))}."
        state["message_style"] = "info"

    # Find active profile
    state["active_profile"] = None
    if os.path.islink(CLAUDE_DIR):
        try:
            target = os.readlink(CLAUDE_DIR)
            abs_target = os.path.abspath(os.path.expanduser(target))
            parent, name = os.path.split(abs_target)
            if os.path.abspath(parent) == os.path.abspath(PROFILES_DIR):
                state["active_profile"] = name
        except Exception:
            pass

    # Normalize selection index
    if not state["profiles"]:
        state["selected_index"] = 0
    elif state["selected_index"] >= len(state["profiles"]):
        state["selected_index"] = len(state["profiles"]) - 1

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
        sharing.sync_on_switch(PROFILES_DIR, state["active_profile"], name)

        if os.path.lexists(CLAUDE_DIR):
            os.unlink(CLAUDE_DIR)
        os.symlink(profile_dir, CLAUDE_DIR)

        if os.path.lexists(CLAUDE_JSON):
            os.unlink(CLAUDE_JSON)
        os.symlink(os.path.join(profile_dir, "claude.json"), CLAUDE_JSON)

        state["message"] = f"Switched to profile '{name}'."
        state["message_style"] = "success"
        return True
    except Exception as e:
        state["message"] = f"Failed to switch profile: {e}"
        state["message_style"] = "error"
        return False

def add_profile(name, share=False):
    """Create a new profile folder, optionally joining the shared pool."""
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
        switch_profile(name)
        where = "sharing data" if share else "with its own data"
        state["message"] = f"Created and activated profile '{name}' ({where})."
        state["message_style"] = "success"
        return True
    except Exception as e:
        state["message"] = f"Failed to create profile: {e}"
        state["message_style"] = "error"
        return False

def delete_profile(name):
    """Remove a profile and fallback to another if active."""
    profile_dir = os.path.join(PROFILES_DIR, name)
    if not os.path.exists(profile_dir):
        state["message"] = f"Profile '{name}' does not exist."
        state["message_style"] = "error"
        return False

    try:
        is_active = (state["active_profile"] == name)
        if is_active:
            if os.path.lexists(CLAUDE_DIR):
                os.unlink(CLAUDE_DIR)
            if os.path.lexists(CLAUDE_JSON):
                os.unlink(CLAUDE_JSON)

        shutil.rmtree(profile_dir)
        state["message"] = f"Deleted profile '{name}'."
        state["message_style"] = "success"

        # Fallback if active was deleted
        if is_active:
            refresh_profiles()
            if state["profiles"]:
                switch_profile(state["profiles"][0])
            else:
                add_profile("default")
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
    if state["profiles"]:
        selected = state["profiles"][state["selected_index"]]
        switch_profile(selected)
        refresh_profiles()
        event.app.invalidate()

@kb.add("a")
def do_add(event):
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

@kb.add("r")
def do_remove(event):
    if not state["profiles"]:
        return

    selected = state["profiles"][state["selected_index"]]
    async def remove_flow():
        def prompt_remove():
            print("\n" * 2)
            print(f"=== REMOVE PROFILE: {selected} ===")
            confirm = input(f"Are you sure you want to remove '{selected}'? (y/N): ").strip().lower()
            if confirm == "y":
                delete_profile(selected)
            else:
                state["message"] = "Profile removal cancelled."
                state["message_style"] = "info"
            time.sleep(1.2)

        await run_in_terminal(prompt_remove)
        refresh_profiles()
        event.app.invalidate()

    import asyncio
    asyncio.create_task(remove_flow())

def enable_sharing_flow(selected, ask=input, say=print):
    """Ask, then move `selected` into the shared pool. Returns True if it did.

    `ask` and `say` are injected so the whole dialogue can be driven by tests
    without a terminal.
    """
    pool = sharing.pool_summary(PROFILES_DIR)
    say(f"=== SHARE DATA: {selected} ===\n")
    if pool is None:
        say("No shared pool exists yet.")
        say(f"'{selected}' will become the pool: its conversations, memory,")
        say("settings and skills stay exactly as they are, and any other")
        say("profile you share later will see them.")
    else:
        others = [p for p in state["profiles"] if state["sharing"].get(p)]
        say(f"Shared pool: {pool['conversations']} conversations, "
            f"{sharing.human_size(pool['bytes'])}, "
            f"used by {', '.join(others)}.")
        say(f"\n'{selected}' will join it, bringing its own data in:")
        say("  - conversations, memory and snapshots merge (names never collide)")
        say("  - a settings file that disagrees with the pool's is kept aside")
        say("    in an _archive folder, never deleted")
    say("\nCredentials and account identity are NOT shared.\n")

    if ask(f"Share data for '{selected}'? (y/N): ").strip().lower() != "y":
        state["message"] = "Sharing unchanged."
        state["message_style"] = "info"
        return False

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

@kb.add("l")
def do_login(event):
    if not state["profiles"]:
        return

    selected = state["profiles"][state["selected_index"]]
    async def login_flow():
        def run_login():
            print("\n" * 2)
            print(f"=== RUNNING LOGIN FOR PROFILE: {selected} ===")
            print(f"Making sure '{selected}' is active...")
            switch_profile(selected)
            refresh_profiles()

            print(f"Running login command: {state['login_command']}")
            print("-" * 40)
            try:
                # Run the login command interactively
                subprocess.run(state["login_command"], shell=True, check=True)
                print("-" * 40)
                print("Login command finished successfully.")
            except subprocess.CalledProcessError as e:
                print("-" * 40)
                print(f"Command failed with exit code {e.returncode}")
            except Exception as e:
                print("-" * 40)
                print(f"Failed to execute command: {e}")

            print("Press Enter to return to the TUI...")
            input()

        await run_in_terminal(run_login)
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
    tokens.append(("class:subtitle", f"{CLAUDE_DIR} -> {os.readlink(CLAUDE_DIR) if os.path.islink(CLAUDE_DIR) else 'Not a symlink'}\n"))
    tokens.append(("", " Account cache: "))
    tokens.append(("class:subtitle", f"{CLAUDE_JSON} -> {os.readlink(CLAUDE_JSON) if os.path.islink(CLAUDE_JSON) else 'Not a symlink'}\n"))
    tokens.append(("", f" Login command: {state['login_command']}\n"))
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
    tokens.append(("class:help-desc", " Switch Profile  "))
    tokens.append(("class:help-key", " [A]"))
    tokens.append(("class:help-desc", " Add  "))
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

def main():
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
    app.run()

if __name__ == "__main__":
    main()
