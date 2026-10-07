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
