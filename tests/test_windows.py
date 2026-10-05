"""Behaviour specific to Windows: junctions, hardlinks, copy-synced claude.json."""

import importlib
import json
import os
import sys
import threading
import time

import pytest

from tui_claude import sharing
from tui_claude.links import is_link, points_to, read_link

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.fixture
def tui(tmp_path, monkeypatch):
    monkeypatch.setenv("TUI_CLAUDE_TEST_DIR", str(tmp_path))
    from tui_claude import main as main_module
    importlib.reload(main_module)
    main_module.refresh_profiles()
    yield main_module
    monkeypatch.delenv("TUI_CLAUDE_TEST_DIR", raising=False)
    importlib.reload(main_module)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@windows_only
def test_switching_uses_a_junction_and_shows_a_clean_path(tui):
    tui.add_profile("work")
    tui.refresh_profiles()

    assert os.path.isjunction(tui.CLAUDE_DIR)
    assert tui.state["active_profile"] == "work"
    screen = "".join(text for _, text in tui.get_screen_text())
    assert "\\\\?\\" not in screen
    assert read_link(tui.CLAUDE_DIR) == os.path.join(tui.PROFILES_DIR, "work")


@windows_only
def test_live_claude_json_is_copied_out_and_back(tui):
    for name in ("a", "b"):
        tui.add_profile(name)
        write_json(os.path.join(tui.PROFILES_DIR, name, "claude.json"),
                   {"oauthAccount": {"emailAddress": f"{name}@example.net"}})

    tui.switch_profile("a")
    assert not os.path.islink(tui.CLAUDE_JSON), "a real file, not a link"
    live = read_json(tui.CLAUDE_JSON)
    assert live["oauthAccount"]["emailAddress"] == "a@example.net"

    # Claude Code rewrites the live file while "a" is active.
    live["numStartups"] = 7
    write_json(tui.CLAUDE_JSON, live)

    tui.switch_profile("b")
    assert read_json(tui.CLAUDE_JSON)["oauthAccount"]["emailAddress"] == "b@example.net"
    assert read_json(os.path.join(tui.PROFILES_DIR, "a", "claude.json"))["numStartups"] == 7

    tui.switch_profile("a")
    assert read_json(tui.CLAUDE_JSON)["numStartups"] == 7, "nothing lost on the way back"


@windows_only
def test_switching_to_a_fresh_profile_does_not_leak_the_account(tui):
    tui.add_profile("a")
    write_json(tui.CLAUDE_JSON, {"oauthAccount": {"emailAddress": "a@example.net"}})

    tui.add_profile("fresh")

    assert not os.path.exists(tui.CLAUDE_JSON), "fresh must log in on its own"
    assert read_json(os.path.join(tui.PROFILES_DIR, "a", "claude.json")) \
        ["oauthAccount"]["emailAddress"] == "a@example.net"


@windows_only
def test_first_run_seeds_the_profile_from_the_live_claude_json(tmp_path, monkeypatch):
    claude_dir = tmp_path / "claude"
    (claude_dir / "projects").mkdir(parents=True)
    (claude_dir / "projects" / "conv.jsonl").write_text("{}")
    write_json(str(tmp_path / "claude.json"), {"userID": "x" * 20})

    monkeypatch.setenv("TUI_CLAUDE_TEST_DIR", str(tmp_path))
    from tui_claude import main
    importlib.reload(main)
    try:
        main.refresh_profiles()
        default = os.path.join(main.PROFILES_DIR, "default")
        assert os.path.isjunction(main.CLAUDE_DIR)
        assert os.path.exists(os.path.join(default, "projects", "conv.jsonl"))
        assert read_json(os.path.join(default, "claude.json"))["userID"] == "x" * 20
        assert os.path.isfile(main.CLAUDE_JSON), "the live file stays in place"
    finally:
        monkeypatch.delenv("TUI_CLAUDE_TEST_DIR")
        importlib.reload(main)


@windows_only
def test_migration_refuses_while_a_file_is_open(tmp_path, monkeypatch):
    """A rename fails while Claude Code holds a file; ~/.claude must stay whole."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    held = claude_dir / "history.jsonl"
    held.write_text("line\n")
    (claude_dir / "settings.json").write_text("{}")

    monkeypatch.setenv("TUI_CLAUDE_TEST_DIR", str(tmp_path))
    from tui_claude import main
    importlib.reload(main)
    try:
        with open(held):
            main.refresh_profiles()
        assert main.state["message_style"] == "error"
        assert "Close every Claude Code session" in main.state["message"]
        assert sorted(os.listdir(claude_dir)) == ["history.jsonl", "settings.json"]
        assert not os.path.isjunction(claude_dir)
    finally:
        monkeypatch.delenv("TUI_CLAUDE_TEST_DIR")
        importlib.reload(main)


@windows_only
def test_shared_files_are_hardlinks_that_write_through(tmp_path):
    profiles_dir = str(tmp_path / "profiles")
    os.makedirs(os.path.join(profiles_dir, "work"))
    sharing.enable_sharing(profiles_dir, "work")

    link = os.path.join(profiles_dir, "work", "settings.json")
    pool_file = os.path.join(sharing.pool_dir(profiles_dir), "settings.json")
    assert points_to(link, pool_file)
    assert read_json(pool_file) == {}, "seeded so the hardlink has a target"
    assert is_link(os.path.join(profiles_dir, "work", "projects")), "dirs are junctions"

    with open(link, "a", encoding="utf-8") as handle:  # in-place write
        handle.write(" ")
    with open(pool_file, encoding="utf-8") as handle:
        assert handle.read().endswith(" ")


def test_non_ascii_claude_json_survives_split_and_rebuild(tmp_path):
    """Windows' default encoding is cp1252; a decode error read as {} would
    erase the config on the next rebuild."""
    profiles_dir = str(tmp_path / "profiles")
    profile = os.path.join(profiles_dir, "work")
    os.makedirs(profile)
    write_json(os.path.join(profile, "claude.json"), {
        "oauthAccount": {"emailAddress": "joão@exemplo.com.br"},
        "projects": {"C:\\Users\\joão\\código": {"history": ["ação ✓"]}},
    })

    sharing.enable_sharing(profiles_dir, "work")
    os.remove(os.path.join(profile, "claude.json"))
    sharing.rebuild_claude_json(profiles_dir, "work")

    rebuilt = read_json(os.path.join(profile, "claude.json"))
    assert rebuilt["oauthAccount"]["emailAddress"] == "joão@exemplo.com.br"
    assert rebuilt["projects"]["C:\\Users\\joão\\código"]["history"] == ["ação ✓"]


def test_pool_lock_excludes_a_second_holder(tmp_path):
    profiles_dir = str(tmp_path / "profiles")
    events = []

    def second():
        with sharing.pool_lock(profiles_dir):
            events.append("second")

    with sharing.pool_lock(profiles_dir):
        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.3)
        events.append("first done")
    thread.join(timeout=5)

    assert events == ["first done", "second"]
