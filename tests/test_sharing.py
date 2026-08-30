import json
import os
import shutil

import pytest

from tui_claude import sharing


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def profiles_dir(tmp_path):
    return str(tmp_path / "claude-profiles")


def make_profile(profiles_dir, name, email=None, conversations=(), memory=None,
                 settings=None, config=None):
    """A realistic isolated profile: conversations, memory, credentials."""
    path = os.path.join(profiles_dir, name)
    projects = os.path.join(path, "projects", "-home-user")
    os.makedirs(projects, exist_ok=True)
    for conv in conversations:
        with open(os.path.join(projects, f"{conv}.jsonl"), "w") as handle:
            handle.write(f'{{"session": "{conv}"}}\n')
    if memory is not None:
        mem_dir = os.path.join(projects, "memory")
        os.makedirs(mem_dir, exist_ok=True)
        with open(os.path.join(mem_dir, "MEMORY.md"), "w") as handle:
            handle.write(memory)
    if settings is not None:
        with open(os.path.join(path, "settings.json"), "w") as handle:
            json.dump(settings, handle)
    with open(os.path.join(path, ".credentials.json"), "w") as handle:
        json.dump({"token": f"secret-of-{name}"}, handle)

    data = dict(config or {})
    if email:
        data["oauthAccount"] = {"emailAddress": email,
                                "accountUuid": f"uuid-of-{name}-0000"}
        data["userID"] = f"userid-of-{name}-{'0' * 20}"
    with open(os.path.join(path, "claude.json"), "w") as handle:
        json.dump(data, handle)
    return path


def conversations_in_pool(profiles_dir):
    projects = os.path.join(sharing.pool_dir(profiles_dir), "projects")
    found = []
    for root, _, files in os.walk(projects):
        found += [f for f in files if f.endswith(".jsonl")]
    return sorted(found)


# --- entering the pool -------------------------------------------------------

def test_first_profile_seeds_the_pool(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa", "bbb"])

    result = sharing.enable_sharing(profiles_dir, "trabalho")

    assert result["seeded"] is True
    assert result["archived"] == 0
    assert conversations_in_pool(profiles_dir) == ["aaa.jsonl", "bbb.jsonl"]
    assert sharing.is_shared(profiles_dir, "trabalho")


def test_second_profile_merges_without_losing_conversations(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa"])
    make_profile(profiles_dir, "pessoal", "p@example.org", conversations=["bbb"])

    sharing.enable_sharing(profiles_dir, "trabalho")
    result = sharing.enable_sharing(profiles_dir, "pessoal")

    assert result["seeded"] is False
    assert result["archived"] == 0, "UUID-named conversations must never collide"
    assert conversations_in_pool(profiles_dir) == ["aaa.jsonl", "bbb.jsonl"]
    # both profiles now read the same files
    for name in ("trabalho", "pessoal"):
        assert sharing.is_shared(profiles_dir, name)
        listing = os.listdir(os.path.join(profiles_dir, name, "projects", "-home-user"))
        assert {"aaa.jsonl", "bbb.jsonl"} <= set(listing)


def test_colliding_file_is_archived_not_overwritten(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", memory="memoria do trabalho")
    make_profile(profiles_dir, "pessoal", "p@example.org", memory="memoria pessoal")

    sharing.enable_sharing(profiles_dir, "trabalho")
    result = sharing.enable_sharing(profiles_dir, "pessoal")

    assert result["archived"] == 1
    pool_memory = os.path.join(sharing.pool_dir(profiles_dir), "projects",
                               "-home-user", "memory", "MEMORY.md")
    assert open(pool_memory).read() == "memoria do trabalho", "pool wins"
    # nothing is destroyed: the displaced file is kept verbatim
    archived = os.path.join(result["archive"], "projects", "-home-user",
                            "memory", "MEMORY.md")
    assert open(archived).read() == "memoria pessoal"


def test_identical_files_merge_silently(profiles_dir):
    make_profile(profiles_dir, "a", "a@example.net", settings={"theme": "dark"})
    make_profile(profiles_dir, "b", "b@example.net", settings={"theme": "dark"})

    sharing.enable_sharing(profiles_dir, "a")
    result = sharing.enable_sharing(profiles_dir, "b")

    assert result["archived"] == 0, "same content is not a conflict"


def test_history_is_merged_by_line(profiles_dir):
    for name, line in (("a", "prompt A"), ("b", "prompt B")):
        make_profile(profiles_dir, name, f"{name}@example.net")
        with open(os.path.join(profiles_dir, name, "history.jsonl"), "w") as handle:
            handle.write(json.dumps({"display": line}) + "\n")

    sharing.enable_sharing(profiles_dir, "a")
    sharing.enable_sharing(profiles_dir, "b")

    merged = open(os.path.join(sharing.pool_dir(profiles_dir), "history.jsonl")).read()
    assert "prompt A" in merged and "prompt B" in merged


# --- identity stays put ------------------------------------------------------

def test_credentials_are_never_shared(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com")
    make_profile(profiles_dir, "pessoal", "p@example.org")

    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.enable_sharing(profiles_dir, "pessoal")

    for name in ("trabalho", "pessoal"):
        creds = os.path.join(profiles_dir, name, ".credentials.json")
        assert not os.path.islink(creds)
        assert json.load(open(creds))["token"] == f"secret-of-{name}"
    pool_files = os.listdir(sharing.pool_dir(profiles_dir))
    assert ".credentials.json" not in pool_files


def test_account_keys_stay_out_of_the_pool(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com",
                 config={"projects": {"/p": {"allowedTools": ["Bash"]}}})

    sharing.enable_sharing(profiles_dir, "trabalho")

    pool_config = json.load(open(os.path.join(sharing.pool_dir(profiles_dir),
                                              "claude.shared.json")))
    assert "oauthAccount" not in pool_config
    assert "userID" not in pool_config
    assert pool_config["projects"]["/p"]["allowedTools"] == ["Bash"]


def test_profile_without_login_does_not_inherit_an_identity(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com")
    make_profile(profiles_dir, "novo")  # no email: never logged in

    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.enable_sharing(profiles_dir, "novo")
    sharing.rebuild_claude_json(profiles_dir, "novo")

    config = json.load(open(os.path.join(profiles_dir, "novo", "claude.json")))
    assert "oauthAccount" not in config
    assert "userID" not in config


def test_unknown_account_key_is_caught_by_content(profiles_dir):
    """A future Claude Code key holding the account's email must not leak."""
    make_profile(profiles_dir, "trabalho", "t@example.com")
    sharing.enable_sharing(profiles_dir, "trabalho")

    config_path = os.path.join(profiles_dir, "trabalho", "claude.json")
    config = json.load(open(config_path))
    config["seatEntitlementCache"] = {"owner": "t@example.com", "tier": "team"}
    config["novaFlagDeUI"] = True
    json.dump(config, open(config_path, "w"))

    withheld = sharing.split_claude_json(profiles_dir, "trabalho")

    pool_config = json.load(open(os.path.join(sharing.pool_dir(profiles_dir),
                                              "claude.shared.json")))
    assert withheld == ["seatEntitlementCache"]
    assert "seatEntitlementCache" not in pool_config
    assert pool_config["novaFlagDeUI"] is True, "harmless new keys still share"


def test_shared_key_is_not_hijacked_by_a_stray_email(profiles_dir):
    """`projects` already lives in the pool; an email typed into a prompt
    must not drag the whole key back into the profile."""
    make_profile(profiles_dir, "trabalho", "t@example.com",
                 config={"projects": {"/p": {"history": []}}})
    sharing.enable_sharing(profiles_dir, "trabalho")

    config_path = os.path.join(profiles_dir, "trabalho", "claude.json")
    config = json.load(open(config_path))
    config["projects"]["/p"]["history"].append({"display": "manda pro t@example.com"})
    json.dump(config, open(config_path, "w"))

    sharing.split_claude_json(profiles_dir, "trabalho")

    pool_config = json.load(open(os.path.join(sharing.pool_dir(profiles_dir),
                                              "claude.shared.json")))
    assert "projects" in pool_config
    assert len(pool_config["projects"]["/p"]["history"]) == 1


def test_two_profiles_changing_different_keys_do_not_erase_each_other(profiles_dir):
    make_profile(profiles_dir, "a", "a@example.net", config={"projects": {"/p": {"allowedTools": []}}})
    make_profile(profiles_dir, "b", "b@example.net")
    sharing.enable_sharing(profiles_dir, "a")
    sharing.enable_sharing(profiles_dir, "b")

    # both open, each unaware of the other
    sharing.rebuild_claude_json(profiles_dir, "a")
    sharing.rebuild_claude_json(profiles_dir, "b")

    config_a = json.load(open(os.path.join(profiles_dir, "a", "claude.json")))
    config_a["projects"]["/p"]["allowedTools"] = ["Bash(git *)"]
    config_a["flagDoA"] = True
    json.dump(config_a, open(os.path.join(profiles_dir, "a", "claude.json"), "w"))

    config_b = json.load(open(os.path.join(profiles_dir, "b", "claude.json")))
    config_b["flagDoB"] = True
    json.dump(config_b, open(os.path.join(profiles_dir, "b", "claude.json"), "w"))

    sharing.split_claude_json(profiles_dir, "a")
    sharing.split_claude_json(profiles_dir, "b")  # b writes last

    pool_config = json.load(open(os.path.join(sharing.pool_dir(profiles_dir),
                                              "claude.shared.json")))
    assert pool_config["projects"]["/p"]["allowedTools"] == ["Bash(git *)"]
    assert pool_config["flagDoA"] is True
    assert pool_config["flagDoB"] is True


# --- leaving the pool --------------------------------------------------------

def test_leaving_with_a_copy_forks_the_data(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa"])
    make_profile(profiles_dir, "pessoal", "p@example.org", conversations=["bbb"])
    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.enable_sharing(profiles_dir, "pessoal")

    sharing.disable_sharing(profiles_dir, "pessoal", take_copy=True)

    assert not sharing.is_shared(profiles_dir, "pessoal")
    own = os.path.join(profiles_dir, "pessoal", "projects", "-home-user")
    assert not os.path.islink(os.path.join(profiles_dir, "pessoal", "projects"))
    assert {"aaa.jsonl", "bbb.jsonl"} <= set(os.listdir(own))

    # and it really diverges: writing on one side does not reach the other
    with open(os.path.join(own, "ccc.jsonl"), "w") as handle:
        handle.write("{}")
    assert "ccc.jsonl" not in conversations_in_pool(profiles_dir)
    assert sharing.is_shared(profiles_dir, "trabalho"), "the other profile is untouched"


def test_leaving_without_a_copy_starts_clean(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa"])
    make_profile(profiles_dir, "pessoal", "p@example.org")
    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.enable_sharing(profiles_dir, "pessoal")

    sharing.disable_sharing(profiles_dir, "pessoal", take_copy=False)

    own = os.path.join(profiles_dir, "pessoal", "projects")
    assert os.path.isdir(own) and not os.path.islink(own)
    assert os.listdir(own) == []
    assert conversations_in_pool(profiles_dir) == ["aaa.jsonl"], "pool intact"


def test_leaving_keeps_a_working_claude_json(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com",
                 config={"projects": {"/p": {"allowedTools": ["Bash"]}}})
    sharing.enable_sharing(profiles_dir, "trabalho")

    sharing.disable_sharing(profiles_dir, "trabalho", take_copy=True)

    config = json.load(open(os.path.join(profiles_dir, "trabalho", "claude.json")))
    assert config["oauthAccount"]["emailAddress"] == "t@example.com"
    assert config["projects"]["/p"]["allowedTools"] == ["Bash"]
    assert not os.path.exists(os.path.join(profiles_dir, "trabalho",
                                           "claude.baseline.json"))


def test_sharing_can_be_toggled_back_on(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa"])
    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.disable_sharing(profiles_dir, "trabalho", take_copy=True)

    sharing.enable_sharing(profiles_dir, "trabalho")

    assert sharing.is_shared(profiles_dir, "trabalho")
    assert conversations_in_pool(profiles_dir) == ["aaa.jsonl"], "no duplication"


# --- destructive-operation safety -------------------------------------------

def test_deleting_a_shared_profile_never_touches_the_pool(profiles_dir):
    """delete_profile() calls shutil.rmtree on the profile directory. If that
    ever followed symlinks it would wipe every profile's conversations."""
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa"])
    make_profile(profiles_dir, "pessoal", "p@example.org", conversations=["bbb"])
    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.enable_sharing(profiles_dir, "pessoal")

    shutil.rmtree(os.path.join(profiles_dir, "pessoal"))

    assert conversations_in_pool(profiles_dir) == ["aaa.jsonl", "bbb.jsonl"]
    assert sharing.is_shared(profiles_dir, "trabalho")


def test_pool_is_not_listed_as_a_profile(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com")
    sharing.enable_sharing(profiles_dir, "trabalho")

    entries = [d for d in os.listdir(profiles_dir)
               if os.path.isdir(os.path.join(profiles_dir, d))]
    assert sharing.POOL_NAME in entries, "it is a real directory..."
    assert sharing.POOL_NAME.startswith("_"), "...but reserved by the _ prefix"


def test_switching_syncs_config_between_profiles(profiles_dir):
    make_profile(profiles_dir, "a", "a@example.net", config={"tipsHistory": {}})
    make_profile(profiles_dir, "b", "b@example.net")
    sharing.enable_sharing(profiles_dir, "a")
    sharing.enable_sharing(profiles_dir, "b")

    config_a = json.load(open(os.path.join(profiles_dir, "a", "claude.json")))
    config_a["tipsHistory"] = {"tip-visto": 3}
    json.dump(config_a, open(os.path.join(profiles_dir, "a", "claude.json"), "w"))

    sharing.sync_on_switch(profiles_dir, leaving="a", entering="b")

    config_b = json.load(open(os.path.join(profiles_dir, "b", "claude.json")))
    assert config_b["tipsHistory"] == {"tip-visto": 3}
    assert config_b["oauthAccount"]["emailAddress"] == "b@example.net", "identity kept"


def test_isolated_profile_is_untouched_by_switching(profiles_dir):
    make_profile(profiles_dir, "compartilhado", "a@example.net", conversations=["aaa"])
    make_profile(profiles_dir, "isolado", "b@example.net", conversations=["bbb"])
    sharing.enable_sharing(profiles_dir, "compartilhado")

    sharing.sync_on_switch(profiles_dir, leaving="compartilhado", entering="isolado")

    assert not sharing.is_shared(profiles_dir, "isolado")
    own = os.listdir(os.path.join(profiles_dir, "isolado", "projects", "-home-user"))
    assert own == ["bbb.jsonl"], "an isolated profile sees only its own data"


# --- self-healing -----------------------------------------------------------

def test_atomic_write_that_breaks_a_symlink_is_repaired(profiles_dir):
    """Writing to .tmp and renaming over the target replaces the symlink with
    a real file, dropping that item from the pool with no error raised."""
    make_profile(profiles_dir, "trabalho", "t@example.com", settings={"theme": "dark"})
    make_profile(profiles_dir, "pessoal", "p@example.org")
    sharing.enable_sharing(profiles_dir, "trabalho")
    sharing.enable_sharing(profiles_dir, "pessoal")

    link = os.path.join(profiles_dir, "trabalho", "settings.json")
    tmp = link + ".tmp"
    json.dump({"theme": "light"}, open(tmp, "w"))
    os.replace(tmp, link)                      # symlink is gone
    assert not os.path.islink(link)

    repaired = sharing.repair_sharing(profiles_dir, "trabalho")

    assert "settings.json" in repaired
    assert os.path.islink(link)
    # the newer content won and reached the other profile through the pool
    other = os.path.join(profiles_dir, "pessoal", "settings.json")
    assert json.load(open(other))["theme"] == "light"


def test_repair_merges_a_directory_that_came_back_as_real(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", conversations=["aaa"])
    sharing.enable_sharing(profiles_dir, "trabalho")

    link = os.path.join(profiles_dir, "trabalho", "projects")
    os.unlink(link)
    os.makedirs(os.path.join(link, "-home-user"))
    with open(os.path.join(link, "-home-user", "offline.jsonl"), "w") as handle:
        handle.write("{}")

    sharing.repair_sharing(profiles_dir, "trabalho")

    assert conversations_in_pool(profiles_dir) == ["aaa.jsonl", "offline.jsonl"], \
        "work done while unlinked is merged back, not lost"


def test_repair_is_a_noop_for_isolated_profiles(profiles_dir):
    make_profile(profiles_dir, "cliente", "c@example.net", conversations=["aaa"])

    assert sharing.repair_sharing(profiles_dir, "cliente") == []
    assert not sharing.is_shared(profiles_dir, "cliente")


def test_repair_is_idempotent(profiles_dir):
    make_profile(profiles_dir, "trabalho", "t@example.com", settings={"theme": "dark"})
    sharing.enable_sharing(profiles_dir, "trabalho")

    assert sharing.repair_sharing(profiles_dir, "trabalho") == []
    assert sharing.repair_sharing(profiles_dir, "trabalho") == []
