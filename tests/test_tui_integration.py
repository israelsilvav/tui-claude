"""Exercises main.py against a sandbox pointed at by TUI_CLAUDE_TEST_DIR."""

import importlib
import json
import os

import pytest

from tui_claude import sharing


@pytest.fixture
def tui(tmp_path, monkeypatch):
    monkeypatch.setenv("TUI_CLAUDE_TEST_DIR", str(tmp_path))
    from tui_claude import main as main_module
    importlib.reload(main_module)     # re-read TUI_CLAUDE_TEST_DIR
    main_module.refresh_profiles()    # bootstraps the "default" profile
    yield main_module
    monkeypatch.delenv("TUI_CLAUDE_TEST_DIR", raising=False)
    importlib.reload(main_module)


def render(tui):
    return "".join(text for _, text in tui.get_screen_text())


def test_bootstraps_and_renders(tui):
    assert tui.state["profiles"] == ["default"]
    screen = render(tui)
    assert "default" in screen
    assert "isolated" in screen, "a fresh profile shares nothing"
    assert "[S]" in screen, "the toggle is discoverable in the footer"


def test_pool_is_never_listed_as_a_profile(tui):
    tui.add_profile("trabalho")
    sharing.enable_sharing(tui.PROFILES_DIR, "trabalho")
    tui.refresh_profiles()

    assert sharing.POOL_NAME not in tui.state["profiles"]
    assert os.path.isdir(sharing.pool_dir(tui.PROFILES_DIR)), "it does exist on disk"


def test_a_profile_cannot_be_named_into_the_reserved_space(tui):
    tui.add_profile("_shared")
    tui.refresh_profiles()

    assert "_shared" not in tui.state["profiles"]
    assert "shared" in tui.state["profiles"], "the underscore is stripped, not the name"


def test_table_reports_sharing_state(tui):
    tui.add_profile("trabalho")
    tui.add_profile("cliente")
    sharing.enable_sharing(tui.PROFILES_DIR, "trabalho")
    tui.refresh_profiles()

    assert tui.state["sharing"] == {"default": False, "trabalho": True, "cliente": False}
    screen = render(tui)
    assert "shared" in screen and "isolated" in screen
    assert "Shared pool:" in screen, "the pool is summarised for the user"


def test_new_profile_can_join_the_pool_on_creation(tui):
    tui.add_profile("trabalho")
    sharing.enable_sharing(tui.PROFILES_DIR, "trabalho")

    tui.add_profile("segundo", share=True)
    tui.refresh_profiles()

    assert tui.state["sharing"]["segundo"] is True


def test_new_profile_defaults_to_isolated(tui):
    tui.add_profile("trabalho")
    sharing.enable_sharing(tui.PROFILES_DIR, "trabalho")

    tui.add_profile("cliente")  # share defaults to False
    tui.refresh_profiles()

    assert tui.state["sharing"]["cliente"] is False


def test_switching_between_shared_profiles_carries_config(tui):
    for name in ("a", "b"):
        tui.add_profile(name)
        config = os.path.join(tui.PROFILES_DIR, name, "claude.json")
        with open(config, "w") as handle:
            json.dump({"oauthAccount": {"emailAddress": f"{name}@example.net"},
                       "userID": f"userid-{name}-{'0' * 20}"}, handle)
        sharing.enable_sharing(tui.PROFILES_DIR, name)
    tui.refresh_profiles()

    tui.switch_profile("a")
    config_path = os.path.join(tui.PROFILES_DIR, "a", "claude.json")
    config = json.load(open(config_path))
    config["tipsHistory"] = {"visto": 1}
    json.dump(config, open(config_path, "w"))
    tui.refresh_profiles()

    tui.switch_profile("b")

    config_b = json.load(open(os.path.join(tui.PROFILES_DIR, "b", "claude.json")))
    assert config_b["tipsHistory"] == {"visto": 1}, "shared config followed"
    assert config_b["oauthAccount"]["emailAddress"] == "b@example.net", "identity did not"


def test_switching_is_a_noop_for_isolated_profiles(tui):
    tui.add_profile("um")
    tui.add_profile("dois")
    tui.refresh_profiles()

    assert tui.switch_profile("um") is True
    assert tui.switch_profile("dois") is True
    assert tui.state["pool"] is None, "no pool is created just by switching"


def test_deleting_a_shared_profile_keeps_the_pool(tui):
    tui.add_profile("trabalho")
    tui.add_profile("pessoal")
    for name in ("trabalho", "pessoal"):
        sharing.enable_sharing(tui.PROFILES_DIR, name)
    projects = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "projects")
    os.makedirs(projects, exist_ok=True)
    open(os.path.join(projects, "conversa.jsonl"), "w").write("{}")
    tui.refresh_profiles()

    tui.delete_profile("pessoal")
    tui.refresh_profiles()

    assert "pessoal" not in tui.state["profiles"]
    assert os.path.exists(os.path.join(projects, "conversa.jsonl"))
    assert tui.state["sharing"]["trabalho"] is True
