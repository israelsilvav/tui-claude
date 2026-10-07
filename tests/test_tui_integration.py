"""Exercises main.py against a sandbox pointed at by TUI_CLAUDE_TEST_DIR."""

import importlib
import json
import os
import subprocess
import sys

import pytest

from tui_claude import sharing, tmp_mode


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
    # Write where Claude Code writes: the live file, a symlink on POSIX and a
    # copy on Windows.
    config_path = tui.CLAUDE_JSON
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
    if tui.COPY_LIVE_JSON:
        assert not os.path.exists(tui.CLAUDE_JSON), "nothing to copy in yet"
    else:
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


# --- renaming ---------------------------------------------------------------

def test_rename_moves_the_profile(tui):
    tui.add_profile("old-name")
    tui.refresh_profiles()

    assert tui.rename_profile("old-name", "new-name") is True

    tui.refresh_profiles()
    assert "new-name" in tui.state["profiles"]
    assert "old-name" not in tui.state["profiles"]


def test_renaming_the_active_profile_keeps_it_active(tui):
    tui.add_profile("work")
    tui.switch_profile("work")
    tui.refresh_profiles()

    tui.rename_profile("work", "employer")

    tui.refresh_profiles()
    assert tui.state["active_profile"] == "employer"
    assert os.path.realpath(tui.CLAUDE_DIR) == \
        os.path.realpath(os.path.join(tui.PROFILES_DIR, "employer"))
    if not tui.COPY_LIVE_JSON:
        assert os.readlink(tui.CLAUDE_JSON) == \
            os.path.join(tui.PROFILES_DIR, "employer", "claude.json")


def test_renaming_keeps_the_account_and_conversations(tui):
    tui.add_profile("work")
    config_path = os.path.join(tui.PROFILES_DIR, "work", "claude.json")
    with open(config_path, "w") as handle:
        json.dump({"oauthAccount": {"emailAddress": "me@example.com"}}, handle)
    projects = os.path.join(tui.PROFILES_DIR, "work", "projects", "-home-user")
    os.makedirs(projects)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.switch_profile("work")
    tui.refresh_profiles()

    tui.rename_profile("work", "employer")

    moved = os.path.join(tui.PROFILES_DIR, "employer")
    assert json.load(open(os.path.join(moved, "claude.json")))["oauthAccount"]["emailAddress"] \
        == "me@example.com"
    assert os.path.exists(os.path.join(moved, "projects", "-home-user",
                                       "conversation.jsonl"))


def test_renaming_a_shared_profile_keeps_it_shared(tui):
    """The links inside a shared profile point into the pool by absolute path,
    so they must survive the directory moving."""
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    projects = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "projects")
    os.makedirs(projects, exist_ok=True)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.refresh_profiles()

    tui.rename_profile("work", "employer")

    tui.refresh_profiles()
    assert tui.state["sharing"]["employer"] is True
    listing = os.listdir(os.path.join(tui.PROFILES_DIR, "employer", "projects"))
    assert "conversation.jsonl" in listing


def test_rename_rejects_a_name_already_taken(tui):
    tui.add_profile("one")
    tui.add_profile("two")
    tui.refresh_profiles()

    assert tui.rename_profile("one", "two") is False

    tui.refresh_profiles()
    assert {"one", "two"} <= set(tui.state["profiles"])
    assert "already exists" in tui.state["message"]


def test_rename_rejects_the_reserved_prefix(tui):
    tui.add_profile("work")
    tui.refresh_profiles()

    tui.rename_profile("work", "_shared")

    tui.refresh_profiles()
    assert "shared" in tui.state["profiles"], "the underscore is stripped"
    assert "_shared" not in tui.state["profiles"]


def test_rename_flow_cancels_on_a_blank_name(tui):
    tui.add_profile("work")
    tui.refresh_profiles()

    assert tui.rename_flow("work", FakeTerminal("").ask, lambda *a: None) is False

    tui.refresh_profiles()
    assert "work" in tui.state["profiles"]
    assert tui.state["message"] == "Rename cancelled."


def test_rename_flow_applies_the_typed_name(tui):
    tui.add_profile("work")
    tui.refresh_profiles()
    term = FakeTerminal("employer")

    assert tui.rename_flow("work", term.ask, term.say) is True

    assert "only its name changes" in term.screen
    tui.refresh_profiles()
    assert "employer" in tui.state["profiles"]


def test_remove_dialogue_points_at_rename(tui):
    """The mistake this guards against: pressing [R] meaning to rename, then
    typing the new name into the y/N prompt."""
    tui.add_profile("work")
    tui.refresh_profiles()
    term = FakeTerminal("v360")          # a name, not a confirmation

    assert tui.remove_flow("work", term.ask, term.say) is False

    assert "press [N]" in term.screen, "the dialogue offers the way out"
    tui.refresh_profiles()
    assert "work" in tui.state["profiles"], "anything but 'y' cancels"


def test_remove_dialogue_says_what_is_lost(tui):
    tui.add_profile("isolated-one")
    projects = os.path.join(tui.PROFILES_DIR, "isolated-one", "projects", "-home-user")
    os.makedirs(projects)
    open(os.path.join(projects, "conversation.jsonl"), "w").write("{}")
    tui.refresh_profiles()
    term = FakeTerminal("n")

    tui.remove_flow("isolated-one", term.ask, term.say)

    assert "isolated" in term.screen and "1 conversation(s) go with it" in term.screen


def test_remove_dialogue_reassures_when_shared(tui):
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    tui.refresh_profiles()
    term = FakeTerminal("n")

    tui.remove_flow("work", term.ask, term.say)

    assert "stay there" in term.screen, "shared conversations are not at risk"


# --- terminals pinned with `tui-claude tmp` ----------------------------------

def pin(tui, name, pid=None):
    tmp_mode.register_pin(tui.PROFILES_DIR, name, os.getpid() if pid is None else pid)
    tui.refresh_profiles()


def test_refresh_counts_live_pins_and_cleans_dead_ones(tui):
    tui.add_profile("work")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    tmp_mode.register_pin(tui.PROFILES_DIR, "work", proc.pid)
    pin(tui, "work")

    assert tui.state["pins"] == {"work": 1}
    assert os.listdir(tmp_mode.pins_dir(tui.PROFILES_DIR)) == [str(os.getpid())]


def test_pins_directory_is_never_listed_as_a_profile(tui):
    tui.add_profile("work")
    pin(tui, "work")

    assert tmp_mode.PINS_NAME not in tui.state["profiles"]


def test_a_pinned_profile_cannot_be_renamed(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    pin(tui, "work")

    assert tui.rename_profile("work", "employer") is False
    assert "is in use by 1 terminal(s) (tmp)" in tui.state["message"]
    assert os.path.isdir(os.path.join(tui.PROFILES_DIR, "work"))


def test_a_pinned_profile_cannot_be_removed(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    pin(tui, "work")

    assert tui.delete_profile("work") is False
    assert os.path.isdir(os.path.join(tui.PROFILES_DIR, "work"))


def test_other_profiles_stay_editable_while_one_is_pinned(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    pin(tui, "work")

    assert tui.rename_profile("personal", "home") is True


def test_rename_and_remove_dialogues_refuse_before_asking(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    pin(tui, "work")

    for flow in (tui.rename_flow, tui.remove_flow):
        term = FakeTerminal()   # no answers: asking anything would raise IndexError
        assert flow("work", ask=term.ask, say=term.say) is False


def test_sharing_a_pinned_profile_is_refused(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    pin(tui, "work")

    term = FakeTerminal()   # no answers: asking anything would raise IndexError
    assert tui.enable_sharing_flow("work", ask=term.ask, say=term.say) is False
    assert not sharing.is_shared(tui.PROFILES_DIR, "work")


def test_isolating_a_pinned_profile_is_refused(tui):
    # One pin per test: pin() uses this process's pid, and a second pin with
    # the same pid would overwrite the first.
    tui.add_profile("work")
    tui.add_profile("personal")
    sharing.enable_sharing(tui.PROFILES_DIR, "personal")
    pin(tui, "personal")

    term = FakeTerminal()
    assert tui.disable_sharing_flow("personal", ask=term.ask, say=term.say) is False
    assert sharing.is_shared(tui.PROFILES_DIR, "personal")


def test_switching_to_a_profile_pinned_elsewhere_keeps_its_live_config(tui):
    for name in ("a", "b"):
        tui.add_profile(name)
        with open(os.path.join(tui.PROFILES_DIR, name, "claude.json"), "w") as handle:
            json.dump({"oauthAccount": {"emailAddress": f"{name}@example.net"}}, handle)
        sharing.enable_sharing(tui.PROFILES_DIR, name)
    tui.switch_profile("a")
    pin(tui, "b")
    config_a = json.load(open(tui.CLAUDE_JSON))
    config_a["fromA"] = 1
    json.dump(config_a, open(tui.CLAUDE_JSON, "w"))
    config_b = os.path.join(tui.PROFILES_DIR, "b", "claude.json")
    before = open(config_b).read()

    tui.switch_profile("b")

    assert open(config_b).read() == before, "b is open in a pinned terminal"


def test_login_in_the_global_mode_ignores_an_inherited_config_dir(tui, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/pinned")

    assert "CLAUDE_CONFIG_DIR" not in tui.login_env("work")


def test_login_in_tmp_mode_targets_the_selected_profile(tui):
    tui.add_profile("work")
    tui.state["tmp_mode"] = True

    env = tui.login_env("work")

    assert env["CLAUDE_CONFIG_DIR"] == os.path.abspath(os.path.join(tui.PROFILES_DIR, "work"))


# --- the command line --------------------------------------------------------

def test_main_without_arguments_runs_the_tui(tui, monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "run_tui", lambda: calls.append("tui"))

    assert tui.main([]) == 0
    assert calls == ["tui"]


def test_help_and_unknown_arguments(tui, capsys):
    assert tui.main(["--help"]) == 0
    assert "tui-claude tmp NAME" in capsys.readouterr().out
    assert tui.main(["bogus"]) == 2
    assert tui.main(["tmp", "a", "b"]) == 2


def test_tmp_is_refused_inside_a_pinned_terminal(tui, monkeypatch, capsys):
    monkeypatch.setenv("TUI_CLAUDE_PROFILE", "work")

    assert tui.main(["tmp"]) == 1
    assert "This terminal already uses 'work'. Type 'exit' first." in capsys.readouterr().err


def test_tmp_is_refused_on_windows(tui, monkeypatch, capsys):
    monkeypatch.setattr(tui, "IS_WINDOWS", True)

    assert tui.main(["tmp", "default"]) == 1
    assert "tmp mode is not supported on Windows yet." in capsys.readouterr().err


def test_tmp_with_an_unknown_profile(tui, monkeypatch, capsys):
    monkeypatch.delenv("TUI_CLAUDE_PROFILE", raising=False)

    assert tui.main(["tmp", "nope"]) == 1
    assert "Profile 'nope' does not exist." in capsys.readouterr().err


def test_tmp_picker_quit_pins_nothing(tui, monkeypatch):
    monkeypatch.delenv("TUI_CLAUDE_PROFILE", raising=False)
    monkeypatch.setattr(tui, "run_tui", lambda: None)
    monkeypatch.setattr(tui.os, "execve", lambda *a: pytest.fail("must not exec"))

    assert tui.main(["tmp"]) == 0
    assert tui.state["tmp_mode"] is True
    assert tmp_mode.live_pins(tui.PROFILES_DIR) == {}


def test_tmp_picker_choice_is_prepared_pinned_and_launched(tui, monkeypatch, capsys):
    monkeypatch.delenv("TUI_CLAUDE_PROFILE", raising=False)
    tui.add_profile("work")
    tui.add_profile("personal")        # the last one added is the global profile
    launched = []
    monkeypatch.setattr(tui, "run_tui", lambda: "work")
    monkeypatch.setattr(tui.os, "execve",
                        lambda path, argv, env: launched.append((path, argv, env)))

    tui.main(["tmp"])

    (path, argv, env), = launched
    assert path == "/bin/sh"
    assert env["CLAUDE_CONFIG_DIR"] == os.path.abspath(os.path.join(tui.PROFILES_DIR, "work"))
    assert tmp_mode.live_pins(tui.PROFILES_DIR) == {"work": 1}
    assert os.readlink(os.path.join(tui.PROFILES_DIR, "work", ".claude.json")) == "claude.json"
    out = capsys.readouterr().out
    assert "This terminal now uses profile 'work'." in out
    assert "Type 'exit' to return to the global profile ('personal')." in out


def test_pinning_the_global_profile_does_not_rebuild_it(tui, monkeypatch):
    monkeypatch.delenv("TUI_CLAUDE_PROFILE", raising=False)
    tui.add_profile("work")
    with open(os.path.join(tui.PROFILES_DIR, "work", "claude.json"), "w") as handle:
        json.dump({"oauthAccount": {"emailAddress": "w@example.net"}}, handle)
    sharing.enable_sharing(tui.PROFILES_DIR, "work")
    tui.switch_profile("work")
    pool = os.path.join(sharing.pool_dir(tui.PROFILES_DIR), "claude.shared.json")
    data = sharing.load_json(pool)
    data["fromElsewhere"] = 1
    sharing.save_json(pool, data)
    config = os.path.join(tui.PROFILES_DIR, "work", "claude.json")
    before = open(config).read()
    monkeypatch.setattr(tui.os, "execve", lambda *a: None)

    tui.main(["tmp", "work"])

    assert open(config).read() == before, "a global session may be running on it"


def test_tmp_exit_reports_the_global_profile_and_drops_the_pin(tui, capsys):
    tui.add_profile("work")
    tui.add_profile("personal")
    tmp_mode.register_pin(tui.PROFILES_DIR, "work", os.getpid())

    assert tui.main(["_tmp-exit", "work", str(os.getpid())]) == 0
    assert "Back to the global profile 'personal'." in capsys.readouterr().out
    assert tmp_mode.live_pins(tui.PROFILES_DIR) == {}


def test_tmp_exit_with_a_bad_pid(tui):
    assert tui.main(["_tmp-exit", "work", "x"]) == 2


def test_login_in_tmp_mode_prepares_without_switching(tui):
    tui.add_profile("work")
    tui.add_profile("personal")
    tui.state["tmp_mode"] = True
    envs = []

    tui.run_login("work", run=lambda cmd, **kw: envs.append(kw["env"]),
                  say=lambda *a: None)

    assert tui.linked_profile() == "personal", "the global profile did not move"
    assert envs[0]["CLAUDE_CONFIG_DIR"] == os.path.abspath(os.path.join(tui.PROFILES_DIR, "work"))
    assert os.path.islink(os.path.join(tui.PROFILES_DIR, "work", ".claude.json"))
