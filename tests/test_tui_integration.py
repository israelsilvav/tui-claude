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
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    tui.refresh_profiles()

    assert sharing.POOL_NAME not in tui.state["profiles"]
    assert os.path.isdir(sharing.pool_dir(tui.PROFILES_DIR)), "it does exist on disk"


def test_a_profile_cannot_be_named_into_the_reserved_space(tui):
    tui.add_profile("_shared")
    tui.refresh_profiles()

    assert "_shared" not in tui.state["profiles"]
    assert "shared" in tui.state["profiles"], "the underscore is stripped, not the name"


def test_table_reports_sharing_state(tui):
    tui.add_profile("work")
    tui.add_profile("client")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    tui.refresh_profiles()

    assert tui.state["sharing"] == {"default": False, "work": True, "client": False}
    screen = render(tui)
    assert "shared" in screen and "isolated" in screen
    assert "Shared pool:" in screen, "the pool is summarised for the user"


def test_new_profile_can_join_the_pool_on_creation(tui):
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")

    tui.add_profile("second", share=True)
    tui.refresh_profiles()

    assert tui.state["sharing"]["second"] is True


def test_new_profile_defaults_to_isolated(tui):
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")

    tui.add_profile("client")  # share defaults to False
    tui.refresh_profiles()

    assert tui.state["sharing"]["client"] is False


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
    config["tipsHistory"] = {"seen": 1}
    json.dump(config, open(config_path, "w"))
    tui.refresh_profiles()

    tui.switch_profile("b")

    config_b = json.load(open(os.path.join(tui.PROFILES_DIR, "b", "claude.json")))
    assert config_b["tipsHistory"] == {"seen": 1}, "shared config followed"
    assert config_b["oauthAccount"]["emailAddress"] == "b@example.net", "identity did not"


def test_switching_is_a_noop_for_isolated_profiles(tui):
    tui.add_profile("one")
    tui.add_profile("two")
    tui.refresh_profiles()

    assert tui.switch_profile("one") is True
    assert tui.switch_profile("two") is True
    assert tui.state["pool"] is None, "no pool is created just by switching"


def test_deleting_a_shared_profile_keeps_the_pool(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    for name in ("work", "personal"):
        sharing.enable_sharing(tui.PROFILES_DIR, name)
    projects = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "projects")
    os.makedirs(projects, exist_ok=True)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.refresh_profiles()

    tui.delete_profile("personal")
    tui.refresh_profiles()

    assert "personal" not in tui.state["profiles"]
    assert os.path.exists(os.path.join(projects, "conversation.jsonl"))
    assert tui.state["sharing"]["work"] is True


# --- the interactive dialogues ----------------------------------------------

class FakeTerminal:
    """Feeds scripted answers to a flow and records what it printed."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.output = []

    def ask(self, prompt):
        self.output.append(prompt)
        return self.answers.pop(0)

    def say(self, *args):
        self.output.append(" ".join(str(a) for a in args))

    @property
    def screen(self):
        return "\n".join(self.output)


def test_enable_flow_explains_seeding_then_shares(tui):
    tui.add_profile("work")
    tui.refresh_profiles()
    term = FakeTerminal("y")

    assert tui.enable_sharing_flow("work", term.ask, term.say) is True

    assert "will become the pool" in term.screen
    assert "NOT shared" in term.screen, "the identity boundary is stated up front"
    tui.refresh_profiles()
    assert tui.state["sharing"]["work"] is True
    assert "now shares data" in tui.state["message"]


def test_enable_flow_declined_changes_nothing(tui):
    tui.add_profile("work")
    tui.refresh_profiles()

    assert tui.enable_sharing_flow("work", FakeTerminal("n").ask, lambda *a: None) is False

    tui.refresh_profiles()
    assert tui.state["sharing"]["work"] is False
    assert tui.state["pool"] is None, "declining does not create a pool"


def test_enable_flow_reports_the_pool_it_is_joining(tui):
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    projects = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "projects")
    os.makedirs(projects, exist_ok=True)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.add_profile("second")
    tui.refresh_profiles()
    term = FakeTerminal("y")

    tui.enable_sharing_flow("second", term.ask, term.say)

    assert "1 conversations" in term.screen
    assert "used by work" in term.screen, "the user sees who else is in"


def test_disable_flow_keeping_a_copy(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    for name in ("work", "personal"):
        sharing.enable_sharing(tui.PROFILES_DIR, name)
    projects = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "projects")
    os.makedirs(projects, exist_ok=True)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.refresh_profiles()
    term = FakeTerminal("k")

    assert tui.disable_sharing_flow("personal", term.ask, term.say) is True

    assert "Keep a copy" in term.screen and "Start empty" in term.screen
    tui.refresh_profiles()
    assert tui.state["sharing"]["personal"] is False
    own = os.path.join(tui.PROFILES_DIR, "personal", "projects")
    assert "conversation.jsonl" in os.listdir(own), "it took the pool with it"
    assert tui.state["sharing"]["work"] is True, "the other profile is untouched"


def test_disable_flow_starting_empty(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    for name in ("work", "personal"):
        sharing.enable_sharing(tui.PROFILES_DIR, name)
    projects = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "projects")
    os.makedirs(projects, exist_ok=True)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.refresh_profiles()

    tui.disable_sharing_flow("personal", FakeTerminal("e").ask, lambda *a: None)

    tui.refresh_profiles()
    assert tui.state["sharing"]["personal"] is False
    assert os.listdir(os.path.join(tui.PROFILES_DIR, "personal", "projects")) == []
    assert "conversation.jsonl" in os.listdir(projects), "the pool survives"


def test_disable_flow_cancelled_leaves_sharing_on(tui):
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    tui.refresh_profiles()

    assert tui.disable_sharing_flow("work", FakeTerminal("c").ask, lambda *a: None) is False

    tui.refresh_profiles()
    assert tui.state["sharing"]["work"] is True
    assert tui.state["message"] == "Sharing unchanged."


def test_toggling_the_active_profile_keeps_the_symlinks_valid(tui):
    tui.add_profile("work")
    tui.switch_profile("work")
    tui.refresh_profiles()
    assert tui.state["active_profile"] == "work"

    tui.enable_sharing_flow("work", FakeTerminal("y").ask, lambda *a: None)
    tui.refresh_profiles()

    assert os.path.realpath(tui.CLAUDE_DIR) == \
        os.path.realpath(os.path.join(tui.PROFILES_DIR, "work"))
    # A brand new profile has no claude.json until Claude Code writes one, so
    # assert where the link points, not that the target exists yet.
    assert os.readlink(tui.CLAUDE_JSON) == \
        os.path.join(tui.PROFILES_DIR, "work", "claude.json")
    assert tui.state["sharing"]["work"] is True


def test_toggling_a_logged_in_profile_keeps_its_account(tui):
    tui.add_profile("work")
    config_path = os.path.join(tui.PROFILES_DIR, "work", "claude.json")
    with open(config_path, "w") as handle:
        json.dump({"oauthAccount": {"emailAddress": "t@example.com"},
                   "userID": "userid-work-" + "0" * 16,
                   "projects": {"/p": {"allowedTools": ["Bash"]}}}, handle)
    tui.switch_profile("work")
    tui.refresh_profiles()

    tui.enable_sharing_flow("work", FakeTerminal("y").ask, lambda *a: None)
    tui.disable_sharing_flow("work", FakeTerminal("k").ask, lambda *a: None)

    config = json.load(open(config_path))
    assert config["oauthAccount"]["emailAddress"] == "t@example.com", \
        "a full round trip through the pool never disturbs the login"
    assert config["projects"]["/p"]["allowedTools"] == ["Bash"]


def test_pool_summary_reflects_what_repair_promoted(tui):
    """state["pool"] must be read after the healing pass, not before."""
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    tui.refresh_profiles()
    assert tui.state["pool"]["conversations"] == 0

    # the link breaks and work happens offline, inside the profile
    link = os.path.join(tui.PROFILES_DIR, "work", "projects")
    os.unlink(link)
    os.makedirs(os.path.join(link, "-home-user"))
    open(os.path.join(link, "-home-user", "offline.jsonl"), "w").write("{}")

    tui.refresh_profiles()

    assert tui.state["pool"]["conversations"] == 1, \
        "the summary shows the repaired pool, not the stale one"


def test_enable_dialogue_handles_a_pool_with_no_members(tui):
    """The pool outlives its last member by design, so it can be empty-handed."""
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    sharing.disable_sharing(tui.PROFILES_DIR, "work", take_copy=False)
    tui.add_profile("second")
    tui.refresh_profiles()
    term = FakeTerminal("y")

    tui.enable_sharing_flow("second", term.ask, term.say)

    assert "used by ." not in term.screen
    assert "used by no profile" in term.screen
