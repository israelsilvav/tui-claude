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

    assert tmp_mode.link_account_json(profile_dir) == {"archive": None, "promoted": False}

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

    result = tmp_mode.link_account_json(profile_dir)

    assert result["promoted"] is True
    assert read(old) == {"version": "new"}
    assert os.readlink(new) == "claude.json"
    assert read(os.path.join(result["archive"], "claude.json")) == {"version": "old"}


def test_older_real_account_file_is_archived(profile_dir):
    current = os.path.join(profile_dir, "claude.json")
    stale = os.path.join(profile_dir, ".claude.json")
    write(current, {"version": "current"})
    write(stale, {"version": "stale"})
    os.utime(stale, (time.time() - 60, time.time() - 60))

    result = tmp_mode.link_account_json(profile_dir)

    assert result["promoted"] is False
    assert read(current) == {"version": "current"}
    assert read(os.path.join(result["archive"], ".claude.json")) == {"version": "stale"}


def test_real_account_file_alone_becomes_claude_json(profile_dir):
    write(os.path.join(profile_dir, ".claude.json"), {"only": 1})

    assert tmp_mode.link_account_json(profile_dir) == {"archive": None, "promoted": True}, \
        "nothing to archive, but claude.json now comes from outside"
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
    assert '"$2" -P -m tui_claude.main _tmp-exit "$3" "$$"' in argv[2]
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


# --- end to end: the real exec, sh and exit hook --------------------------------

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

FAKE_SHELL = """#!/bin/sh
{
  echo "CONFIG=$CLAUDE_CONFIG_DIR"
  echo "PROFILE=$TUI_CLAUDE_PROFILE"
  echo "PINS=$(ls "$PINS_DIR" | tr '\\n' ' ')"
  echo "SIGIGN=$(grep SigIgn /proc/self/status 2>/dev/null | awk '{print $2}')"
} > "$REPORT"
"$PY" -c '
import json, os
path = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
data = json.load(open(path))
data["trustedInPinnedTerminal"] = True
json.dump(data, open(path, "w"))
'
exit 3
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A migrated layout with `work` and `personal` sharing; `personal` global."""
    import importlib
    monkeypatch.setenv("TUI_CLAUDE_TEST_DIR", str(tmp_path))
    from tui_claude import main as main_module
    importlib.reload(main_module)
    main_module.refresh_profiles()
    for name in ("work", "personal"):
        main_module.add_profile(name)
        write(os.path.join(main_module.PROFILES_DIR, name, "claude.json"),
              {"oauthAccount": {"emailAddress": f"{name}@example.net"}})
        sharing.enable_sharing(main_module.PROFILES_DIR, name)
    main_module.switch_profile("personal")
    yield main_module
    monkeypatch.delenv("TUI_CLAUDE_TEST_DIR", raising=False)
    importlib.reload(main_module)


def cli_env(tui, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CONFIG_DIR", "TUI_CLAUDE_PROFILE")}
    env.update(TUI_CLAUDE_TEST_DIR=os.path.dirname(tui.PROFILES_DIR),
               PYTHONPATH=SRC, PY=sys.executable,
               PINS_DIR=tmp_mode.pins_dir(tui.PROFILES_DIR), **extra)
    return env


def test_tmp_end_to_end(sandbox, tmp_path):
    shell = str(tmp_path / "fake shell.sh")   # a space in the path, on purpose
    with open(shell, "w") as handle:
        handle.write(FAKE_SHELL)
    os.chmod(shell, 0o755)
    report = str(tmp_path / "report.txt")

    proc = subprocess.run([sys.executable, "-m", "tui_claude.main", "tmp", "work"],
                          env=cli_env(sandbox, SHELL=shell, REPORT=report),
                          capture_output=True, text=True, timeout=60)

    assert proc.returncode == 3, proc.stderr          # the shell's status is passed on
    lines = open(report).read().splitlines()
    assert f"CONFIG={os.path.join(sandbox.PROFILES_DIR, 'work')}" in lines
    assert "PROFILE=work" in lines
    pins_line = next(line for line in lines if line.startswith("PINS="))
    assert pins_line.split("=", 1)[1].strip().isdigit(), "exactly one pin while inside"
    ignored = next(line for line in lines if line.startswith("SIGIGN=")).split("=", 1)[1]
    if ignored:   # Linux: /proc/self/status of a command run inside the pinned shell
        mask = int(ignored, 16)
        assert not mask & (1 << 12), "SIGPIPE must not stay ignored (`cmd | head` never ends)"
        assert not mask & (1 << 24), "SIGXFSZ must not stay ignored"
    assert os.listdir(tmp_mode.pins_dir(sandbox.PROFILES_DIR)) == [], "gone after exit"
    pool = read(os.path.join(sharing.pool_dir(sandbox.PROFILES_DIR), "claude.shared.json"))
    assert pool["trustedInPinnedTerminal"] is True
    assert "This terminal now uses profile 'work'." in proc.stdout
    assert "Back to the global profile 'personal'." in proc.stdout


def test_exit_hook_ignores_modules_in_the_current_directory(sandbox, tmp_path):
    """python -m puts the cwd first on sys.path: a json.py in the directory
    where `tmp` was typed must not run when the pinned terminal exits."""
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "hijacked"
    (project / "json.py").write_text(
        f"open({str(marker)!r}, 'w').close()\nraise SystemExit('hijacked')\n")
    shell = tmp_path / "exit0.sh"
    shell.write_text("#!/bin/sh\nexit 0\n")
    shell.chmod(0o755)

    # -P keeps the launcher itself away from the cwd: only the hook is on trial.
    proc = subprocess.run([sys.executable, "-P", "-m", "tui_claude.main", "tmp", "work"],
                          cwd=str(project), env=cli_env(sandbox, SHELL=str(shell)),
                          capture_output=True, text=True, timeout=60)

    assert not marker.exists(), "the exit hook imported json.py from the cwd"
    assert os.listdir(tmp_mode.pins_dir(sandbox.PROFILES_DIR)) == []
    assert "Back to the global profile 'personal'." in proc.stdout


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.exists("/bin/bash"),
                    reason="needs Linux and bash")
def test_ctrl_c_in_the_pinned_shell_keeps_the_exit_hook(sandbox, tmp_path):
    import pty
    import select

    shell = str(tmp_path / "bash-norc")
    with open(shell, "w") as handle:
        handle.write("#!/bin/sh\nexec /bin/bash --norc --noprofile -i\n")
    os.chmod(shell, 0o755)
    env = cli_env(sandbox, SHELL=shell, PS1="pinned> ", TERM="dumb")

    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-m", "tui_claude.main", "tmp", "work"], env)

    def read_until(marker, timeout=15):
        out = b""
        deadline = time.time() + timeout
        while marker not in out:
            remaining = deadline - time.time()
            assert remaining > 0, f"timed out waiting for {marker!r}; got {out!r}"
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return out

    read_until(b"pinned> ")
    os.write(fd, b"\x03")                  # Ctrl-C at the prompt
    read_until(b"pinned> ")
    os.write(fd, b"sleep 30\n")
    time.sleep(0.5)
    os.write(fd, b"\x03")                  # Ctrl-C interrupting a command
    read_until(b"pinned> ")
    os.write(fd, b"exit\n")
    rest = read_until(b"Back to the global profile")
    os.waitpid(pid, 0)

    assert b"Back to the global profile 'personal'." in rest
    assert tmp_mode.live_pins(sandbox.PROFILES_DIR) == {}
