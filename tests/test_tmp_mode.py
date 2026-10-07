"""tmp mode: one terminal pinned to one profile through CLAUDE_CONFIG_DIR."""

import json
import os
import subprocess
import sys
import time

import pytest

from tui_claude import sharing, tmp_mode

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="tmp mode is POSIX only")


@pytest.fixture
def profile_dir(tmp_path):
    path = tmp_path / "claude-profiles" / "work"
    path.mkdir(parents=True)
    return str(path)


def write(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --- the account file Claude Code reads under CLAUDE_CONFIG_DIR --------------

def test_account_link_is_created_relative(profile_dir):
    write(os.path.join(profile_dir, "claude.json"), {"userID": "u"})

    assert tmp_mode.link_account_json(profile_dir) is None

    link = os.path.join(profile_dir, ".claude.json")
    assert os.readlink(link) == "claude.json", "relative, so a rename keeps it valid"
    assert read(link) == {"userID": "u"}


def test_account_link_already_right_is_left_alone(profile_dir):
    write(os.path.join(profile_dir, "claude.json"), {})
    tmp_mode.link_account_json(profile_dir)
    inode = os.lstat(os.path.join(profile_dir, ".claude.json")).st_ino

    tmp_mode.link_account_json(profile_dir)

    assert os.lstat(os.path.join(profile_dir, ".claude.json")).st_ino == inode


def test_account_link_pointing_elsewhere_is_replaced(profile_dir, tmp_path):
    write(os.path.join(profile_dir, "claude.json"), {"mine": 1})
    other = str(tmp_path / "other.json")
    write(other, {"other": 1})
    os.symlink(other, os.path.join(profile_dir, ".claude.json"))

    tmp_mode.link_account_json(profile_dir)

    assert os.readlink(os.path.join(profile_dir, ".claude.json")) == "claude.json"
    assert read(other) == {"other": 1}, "the old target is untouched"


def test_account_link_without_claude_json_dangles_like_the_global_one(profile_dir):
    tmp_mode.link_account_json(profile_dir)

    link = os.path.join(profile_dir, ".claude.json")
    assert os.path.islink(link) and not os.path.exists(link)


def test_newer_real_account_file_wins_and_the_older_is_archived(profile_dir):
    old = os.path.join(profile_dir, "claude.json")
    new = os.path.join(profile_dir, ".claude.json")
    write(old, {"version": "old"})
    write(new, {"version": "new"})
    os.utime(old, (time.time() - 60, time.time() - 60))

    archived = tmp_mode.link_account_json(profile_dir)

    assert read(old) == {"version": "new"}
    assert os.readlink(new) == "claude.json"
    assert read(os.path.join(archived, "claude.json")) == {"version": "old"}


def test_older_real_account_file_is_archived(profile_dir):
    current = os.path.join(profile_dir, "claude.json")
    stale = os.path.join(profile_dir, ".claude.json")
    write(current, {"version": "current"})
    write(stale, {"version": "stale"})
    os.utime(stale, (time.time() - 60, time.time() - 60))

    archived = tmp_mode.link_account_json(profile_dir)

    assert read(current) == {"version": "current"}
    assert read(os.path.join(archived, ".claude.json")) == {"version": "stale"}


def test_real_account_file_alone_becomes_claude_json(profile_dir):
    write(os.path.join(profile_dir, ".claude.json"), {"only": 1})

    assert tmp_mode.link_account_json(profile_dir) is None, "nothing to archive"
    assert read(os.path.join(profile_dir, "claude.json")) == {"only": 1}


def test_account_link_survives_renaming_the_profile(profile_dir):
    write(os.path.join(profile_dir, "claude.json"), {"userID": "u"})
    tmp_mode.link_account_json(profile_dir)
    renamed = os.path.join(os.path.dirname(profile_dir), "employer")

    os.rename(profile_dir, renamed)

    assert read(os.path.join(renamed, ".claude.json")) == {"userID": "u"}


# --- pins: which terminals are pinned right now ------------------------------

@pytest.fixture
def profiles_dir(tmp_path):
    path = tmp_path / "claude-profiles"
    path.mkdir()
    return str(path)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_pin_of_a_live_process_counts(profiles_dir):
    tmp_mode.register_pin(profiles_dir, "work", os.getpid())

    assert tmp_mode.live_pins(profiles_dir) == {"work": 1}


def test_pins_are_counted_per_profile(profiles_dir):
    sleepers = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                for _ in range(2)]
    try:
        tmp_mode.register_pin(profiles_dir, "work", sleepers[0].pid)
        tmp_mode.register_pin(profiles_dir, "work", sleepers[1].pid)
        tmp_mode.register_pin(profiles_dir, "personal", os.getpid())

        assert tmp_mode.live_pins(profiles_dir) == {"work": 2, "personal": 1}
    finally:
        for proc in sleepers:
            proc.kill()
            proc.wait()


def test_a_dead_pin_is_ignored_then_cleaned(profiles_dir):
    tmp_mode.register_pin(profiles_dir, "work", dead_pid())

    assert tmp_mode.live_pins(profiles_dir) == {}
    tmp_mode.clean_dead_pins(profiles_dir)
    assert os.listdir(tmp_mode.pins_dir(profiles_dir)) == []


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc")
def test_a_reused_pid_is_not_mistaken_for_the_pinned_terminal(profiles_dir):
    os.makedirs(tmp_mode.pins_dir(profiles_dir))
    with open(os.path.join(tmp_mode.pins_dir(profiles_dir), str(os.getpid())), "w") as handle:
        handle.write("work\n1\n")   # this pid, but a start time it never had

    assert tmp_mode.live_pins(profiles_dir) == {}


def test_remove_pin_is_idempotent(profiles_dir):
    tmp_mode.register_pin(profiles_dir, "work", os.getpid())

    tmp_mode.remove_pin(profiles_dir, os.getpid())
    tmp_mode.remove_pin(profiles_dir, os.getpid())

    assert tmp_mode.live_pins(profiles_dir) == {}


def test_malformed_pin_files_are_ignored(profiles_dir):
    directory = tmp_mode.pins_dir(profiles_dir)
    os.makedirs(directory)
    with open(os.path.join(directory, "not-a-pid"), "w") as handle:
        handle.write("work\n")
    with open(os.path.join(directory, str(os.getpid())), "w") as handle:
        handle.write("")                        # empty
    with open(os.path.join(directory, "0"), "w") as handle:
        handle.write("work\n")                  # pid 0 is the process group
    os.makedirs(os.path.join(directory, "123"))  # a directory, not a file

    assert tmp_mode.live_pins(profiles_dir) == {}
    tmp_mode.clean_dead_pins(profiles_dir)       # must not raise


def test_no_pins_directory_means_no_pins(profiles_dir):
    assert tmp_mode.live_pins(profiles_dir) == {}
    tmp_mode.clean_dead_pins(profiles_dir)


def test_pins_are_never_probed_on_windows(profiles_dir, monkeypatch):
    """os.kill(pid, 0) terminates the process on Windows instead of probing it."""
    tmp_mode.register_pin(profiles_dir, "work", os.getpid())
    monkeypatch.setattr(tmp_mode, "IS_WINDOWS", True)

    def forbidden(*args):
        raise AssertionError("os.kill must not run on Windows")

    monkeypatch.setattr(tmp_mode.os, "kill", forbidden)

    assert tmp_mode.live_pins(profiles_dir) == {}
    tmp_mode.clean_dead_pins(profiles_dir)


# --- the environment and the exec ---------------------------------------------

def test_config_env_points_claude_at_the_profile(profile_dir):
    env = tmp_mode.config_env(profile_dir, "work", base={"PATH": "/bin"})

    assert env == {"PATH": "/bin",
                   "CLAUDE_CONFIG_DIR": os.path.abspath(profile_dir),
                   "TUI_CLAUDE_PROFILE": "work"}


def test_config_env_is_absolute_even_from_a_relative_path(profile_dir, monkeypatch):
    """Claude Code rejects a relative CLAUDE_CONFIG_DIR."""
    monkeypatch.chdir(os.path.dirname(profile_dir))

    env = tmp_mode.config_env("work", "work", base={})

    assert env["CLAUDE_CONFIG_DIR"] == os.path.abspath(profile_dir)


def test_global_env_drops_an_inherited_config_dir():
    env = tmp_mode.global_env(base={"CLAUDE_CONFIG_DIR": "/x", "PATH": "/bin"})

    assert env == {"PATH": "/bin"}


def test_launch_runs_the_user_shell_then_reports_back(profile_dir):
    argv, env = tmp_mode.launch_command(profile_dir, "work", base_env={"SHELL": "/bin/zsh"})

    assert argv[:2] == ["/bin/sh", "-c"]
    assert argv[3:] == ["tui-claude-tmp", "/bin/zsh", sys.executable, "work"]
    assert '"$2" -m tui_claude.main _tmp-exit "$3" "$$"' in argv[2]
    assert env["CLAUDE_CONFIG_DIR"] == os.path.abspath(profile_dir)
    assert env["TUI_CLAUDE_PROFILE"] == "work"


def test_launch_falls_back_to_sh_without_a_shell(profile_dir):
    for base in ({}, {"SHELL": ""}):
        argv, _ = tmp_mode.launch_command(profile_dir, "work", base_env=base)
        assert argv[4] == "/bin/sh"


def test_exit_script_survives_ctrl_c_without_ignoring_it():
    """`trap ''` would be inherited by the shell and by claude; `trap :` is not."""
    assert tmp_mode.EXIT_SCRIPT.splitlines()[0] == "trap : INT QUIT"


# --- leaving the pinned terminal -----------------------------------------------

def test_finish_returns_config_to_the_pool_and_drops_the_pin(tmp_path):
    profiles = str(tmp_path / "claude-profiles")
    work = os.path.join(profiles, "work")
    os.makedirs(work)
    write(os.path.join(work, "claude.json"), {"oauthAccount": {"emailAddress": "w@example.net"}})
    sharing.enable_sharing(profiles, "work")
    tmp_mode.register_pin(profiles, "work", os.getpid())
    config = read(os.path.join(work, "claude.json"))
    config["trustedFolder"] = True
    write(os.path.join(work, "claude.json"), config)

    tmp_mode.finish(profiles, "work", os.getpid())

    pool = read(os.path.join(sharing.pool_dir(profiles), "claude.shared.json"))
    assert pool["trustedFolder"] is True
    assert tmp_mode.live_pins(profiles) == {}


def test_finish_after_the_profile_was_deleted_only_drops_the_pin(tmp_path):
    profiles = str(tmp_path / "claude-profiles")
    os.makedirs(profiles)
    tmp_mode.register_pin(profiles, "gone", os.getpid())

    tmp_mode.finish(profiles, "gone", os.getpid())

    assert tmp_mode.live_pins(profiles) == {}
