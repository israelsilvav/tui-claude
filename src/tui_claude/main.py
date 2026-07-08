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

TEST_DIR = os.environ.get("TUI_CLAUDE_TEST_DIR")
if TEST_DIR:
    PROFILES_DIR = os.path.join(TEST_DIR, "claude-profiles")
    CLAUDE_DIR = os.path.join(TEST_DIR, "claude")
else:
    PROFILES_DIR = os.path.expanduser("~/.claude-profiles")
    CLAUDE_DIR = os.path.expanduser("~/.claude")

# Global State
state = {
    "profiles": [],
    "active_profile": None,
    "selected_index": 0,
    "login_command": "claude login",
    "message": "",
    "message_style": "info"
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

def refresh_profiles():
    """Scan profiles and active state."""
    init_profiles()

    # List subdirectories of ~/.claude-profiles
    profiles = []
    if os.path.exists(PROFILES_DIR):
        for name in os.listdir(PROFILES_DIR):
            full_path = os.path.join(PROFILES_DIR, name)
            if os.path.isdir(full_path):
                profiles.append(name)
    state["profiles"] = sorted(profiles)

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
        if os.path.lexists(CLAUDE_DIR):
            os.unlink(CLAUDE_DIR)
        os.symlink(profile_dir, CLAUDE_DIR)
        state["message"] = f"Switched to profile '{name}'."
        state["message_style"] = "success"
        return True
    except Exception as e:
        state["message"] = f"Failed to switch profile: {e}"
        state["message_style"] = "error"
        return False

def add_profile(name):
    """Create a new profile folder."""
    name = "".join([c for c in name if c.isalnum() or c in ("-", "_")]).strip()
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
        switch_profile(name)
        state["message"] = f"Created and activated profile '{name}'."
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
            if name:
                add_profile(name)
            else:
                state["message"] = "Profile creation cancelled."
                state["message_style"] = "info"
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
    tokens.append(("", f" Login command: {state['login_command']}\n"))
    tokens.append(("", "\n"))

    # Profile List Table
    tokens.append(("class:border", " ┌──────────────────────────────┬───────────────┐\n"))
    tokens.append(("class:border", " │ Profile Name                 │ Status        │\n"))
    tokens.append(("class:border", " ├──────────────────────────────┼───────────────┤\n"))

    if not state["profiles"]:
        tokens.append(("class:border", " │ "))
        tokens.append(("class:error", "No profiles found.           "))
        tokens.append(("class:border", " │               │\n"))
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

            tokens.append(("class:border", " │ "))
            tokens.append((row_style, f"{name:<28}"))
            tokens.append(("class:border", " │ "))
            tokens.append((status_style, f"{status_text:<13}"))
            tokens.append(("class:border", " │\n"))

    tokens.append(("class:border", " └──────────────────────────────┴───────────────┘\n"))
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
