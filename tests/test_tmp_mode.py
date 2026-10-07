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
