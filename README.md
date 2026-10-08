# tui-claude

**Switch between Claude Code accounts without losing your work.**

Claude Code keeps everything for one account in `~/.claude` — credentials, conversations, memory, settings. Logging into a second account means logging out of the first, and there is no way back to what you had. `tui-claude` turns that single directory into named profiles you can switch between in one keystroke, and lets them share the work while keeping the logins apart.

```
 ┌────────────────────────────────────────────────────────┐
 │               CLAUDE CODE PROFILE MANAGER              │
 └────────────────────────────────────────────────────────┘

 Active profile: ● work
 Config location: ~/.claude -> ~/.claude-profiles/work
 Account cache: ~/.claude.json -> ~/.claude-profiles/work/claude.json
 Login command: claude auth login

 ┌──────────────────────────────┬───────────────┬──────────────┐
 │ Profile Name                 │ Status        │ Data         │
 ├──────────────────────────────┼───────────────┼──────────────┤
 │ client-x                     │   inactive    │ ⊘ isolated   │
 │ personal                     │   inactive    │ ⇄ shared     │
 │ work                         │ ● ACTIVE      │ ⇄ shared     │
 └──────────────────────────────┴───────────────┴──────────────┘
 Shared pool: 7 conversations, 5.5 KB, 2 profile(s)

 [Enter] Switch Profile   [A] Add   [N] Rename   [R] Remove   [L] Login   [S] Share Data   [C] Set Command   [Q] Quit
```

---

## Install

Requires Python 3.12+.

```bash
git clone https://github.com/dnilsonc/tui-claude
cd tui-claude
uv sync
uv run tui-claude
```

To get a `tui-claude` command on your `PATH`:

```bash
uv tool install .
```

### Windows

Works on Windows 10/11 with no administrator rights and no Developer Mode. Windows does not let a normal user create symlinks, so the links are made differently:

| Item | Linux / macOS | Windows |
|------|---------------|---------|
| `~/.claude` and shared directories | symlink | junction |
| Shared files (`settings.json`, `history.jsonl`, `CLAUDE.md`) | symlink | symlink in Developer Mode, otherwise hardlink |
| `~/.claude.json` | symlink | real file, copied out of and into the profile on every switch |
| `tui-claude tmp` | supported | not yet |

> [!IMPORTANT]
> Close **every** Claude Code session (terminal, IDE extension, desktop app) before the first run. Windows cannot move `~/.claude` while a file in it is open; `tui-claude` then stops with an error and leaves `~/.claude` untouched instead of moving half of it.

## Usage

```bash
tui-claude
```

| Key | Action |
|-----|--------|
| `↑` `↓` / `k` `j` | Move the selection |
| `Enter` | Switch to the selected profile (in `tui-claude tmp`: use it in this terminal) |
| `A` | Create a profile |
| `N` / `F2` | Rename a profile |
| `R` | Delete a profile (asks first) |
| `L` | Run the login command for the selected profile |
| `S` | Share or isolate the selected profile's data |
| `C` | Change the login command |
| `Q` / `Ctrl-C` | Quit |

### First run

If `~/.claude` is a real directory, it is moved to `~/.claude-profiles/default` and replaced by a symlink. Nothing is lost — your existing account becomes the `default` profile.

> [!WARNING]
> If `~/.claude-profiles/default` **already exists** when `~/.claude` is still a real directory, the existing `~/.claude` is moved to `~/.claude-profiles/backup-existing-<timestamp>` instead, and the empty `default` takes its place. The active profile then looks blank and Claude Code asks you to log in again. Your data is in the backup directory, not gone — copy `projects/` out of it. This happens when a profile was created before the first migration ran.

### Renaming

`N` (or `F2`) renames the selected profile. The account, conversations and
settings stay with it — only the name changes, and a profile that was active
stays active. `R` is Remove, not Rename.

### Logging in

A new profile has no credentials. Select it and press `L`: the profile is activated and the login command runs in your terminal. The default is `claude auth login`; press `C` to change it.

### One terminal only

`tui-claude tmp` uses a profile in **this terminal only**, so two accounts can
run side by side:

```
$ tui-claude tmp            # pick "work" and press Enter
This terminal now uses profile 'work'.
Type 'exit' to return to the global profile ('personal').
$ claude                    # runs as work
$ exit
Back to the global profile 'personal'.
$ claude                    # runs as personal again
```

`tui-claude tmp work` skips the picker. Every other terminal keeps following
the global profile, and switching the global profile does not affect a pinned
terminal. Shared profiles still see the same conversations, memory and
settings.

It opens a shell with `CLAUDE_CONFIG_DIR` pointing at the profile, which makes
Claude Code read that profile directly instead of `~/.claude`. `exit` (or
Ctrl-D) closes that shell. The table shows `◆N` next to a profile pinned in N
terminals, and refuses to rename, remove or re-share it until they exit.

The prompt does not change by itself. To see the profile in it, add this to
`~/.bashrc` (or `~/.zshrc`):

```bash
PS1='${TUI_CLAUDE_PROFILE:+(claude:$TUI_CLAUDE_PROFILE) }'"$PS1"
```

> [!NOTE]
> Linux and macOS only for now. On macOS, Claude Code keeps the login of a
> pinned profile in a separate Keychain entry: log in once with `L` inside
> `tui-claude tmp`.

### Dev containers

Bind-mounting `~/.claude` and `~/.claude.json` into a container does not
follow a switch: Docker resolves the symlinks when the container starts, so
the container keeps the account that was active then.

Mount the profiles directory instead, **at the same path as on the host**, and
point the container at `_active`, a link `tui-claude` keeps on the global
profile:

```jsonc
// .devcontainer/devcontainer.json
"mounts": [
  "source=${localEnv:HOME}/.claude-profiles,target=${localEnv:HOME}/.claude-profiles,type=bind"
],
"postCreateCommand": "ln -sfn ${localEnv:HOME}/.claude-profiles/_active ~/.claude && ln -sfn ${localEnv:HOME}/.claude-profiles/_active/claude.json ~/.claude.json"
```

Remove any mount of `~/.claude` or `~/.claude.json`: a mounted file cannot be
replaced by a link (`Device or resource busy`). The same path matters because
a shared profile links into the pool by absolute path.

Switch on the host; every `claude` started in the container afterwards uses
the new account. Do not run `tui-claude` inside the container: its pins are
process ids, and the two sides cannot see each other's processes.

## How it works

Claude Code always reads from two fixed paths. `tui-claude` points them at the profile you selected:

```
~/.claude       ->  ~/.claude-profiles/<profile>/
~/.claude.json  ->  ~/.claude-profiles/<profile>/claude.json
```

Switching a profile is just re-pointing those two symlinks, so it is instant and never copies your data. Each profile directory is a complete Claude Code home: its own credentials, conversations, memory and settings.

Directory names starting with `_` are reserved for internals and are never listed as profiles.

> [!NOTE]
> Switch profiles with no Claude Code session running. A session that is already open holds its configuration in memory and writes it back on exit — into whichever profile is active *at that moment*.
>
> Terminals pinned with `tui-claude tmp` are not affected: they read their profile directly.

## Sharing data between profiles

Profiles exist to switch **accounts**. By default they also split everything else, which is rarely what you want: switch to your other account and your conversation history is gone.

Press `S` to put a profile in the shared pool. Profiles in the pool read and write the same conversations, memory, settings and skills. Each keeps its own credentials and account identity.

Sharing is per profile, so a client account stays isolated while your own profiles share. It is entirely opt-in — nothing changes until you press `S`.

### What is shared

| Shared | Never shared |
|--------|--------------|
| `projects/` — conversations and auto-memory | `.credentials.json` — auth tokens |
| `settings.json`, `CLAUDE.md` | `oauthAccount`, `userID` and account caches |
| `agents/`, `commands/`, `skills/`, `plugins/` | `sessions/`, `policy-limits.json` |
| `history.jsonl`, `shell-snapshots/`, `todos/` | `remote-settings*.json`, `mcp-needs-auth-cache.json` |

### Joining and leaving

**Joining an empty pool** — your data *becomes* the pool, untouched.

**Joining an existing pool** — conversations, memory and snapshots merge. They are named by UUID or timestamp, so they never collide and nothing is overwritten. A file that genuinely conflicts (two different `settings.json`, say) is moved to `<profile>/_archive-<timestamp>/` byte for byte. Nothing is deleted.

**Leaving** asks what the profile keeps:

- **Keep a copy** — forks the pool into the profile, which diverges from there.
- **Start empty** — the profile begins with no conversations.

Either way the pool is untouched and the other profiles keep reading it.

**Deleting a shared profile** removes only that profile. The pool survives, even when the profile deleted was the last one using it: orphaning data is recoverable, deleting it is not.

### `claude.json`

This one file mixes account identity (`oauthAccount`, `userID`) with shareable configuration (trust dialogs, per-project permissions, UI flags), so it cannot simply be symlinked — sharing it whole would defeat the point of profiles.

It is split into `_shared/claude.shared.json` and each profile's `claude.account.json`, then rebuilt on every switch:

- Account keys are filtered **by name**, plus a **content check** that catches any new key carrying this account's email or UUID under a name the rules do not know. The content check applies only to keys the pool has never seen — an email can appear inside `projects` (someone typed it into a prompt), and hijacking that whole key would break the sharing it is meant to protect.
- Writes are **deltas against a baseline**, not overwrites, so two profiles that changed different keys do not erase each other.
- The read-merge-write holds an exclusive `flock`, so concurrent instances cannot drop an update.

### Self-healing

An atomic write — write to `.tmp`, rename over the target — replaces a symlink with a real file. A shared profile can therefore stop sharing one item with **no error raised anywhere**.

`tui-claude` detects and repairs this on every refresh. When both sides changed, the newer copy wins and the older one is archived rather than discarded.

## On disk

```
~/.claude-profiles/
    _shared/                      the pool — reserved name, never a profile
        projects/                 conversations + memory
        settings.json
        claude.shared.json        claude.json minus anything account-bound
        .lock

    _pins/                        one file per terminal pinned with `tmp`

    _active         -> work       the global profile, for dev containers

    work/                         a shared profile
        projects        -> ~/.claude-profiles/_shared/projects
        settings.json   -> ~/.claude-profiles/_shared/settings.json
        .credentials.json         real file, never shared
        claude.account.json       identity split out of claude.json
        claude.baseline.json      what the pool looked like at the last switch
        claude.json               rebuilt = shared + account
        .claude.json    -> claude.json   what Claude Code reads under CLAUDE_CONFIG_DIR

    client-x/                     an isolated profile
        projects/                 real directory, sees none of the above
        .credentials.json
        claude.json
```

## Troubleshooting

**A profile shows `⊘ isolated` but I shared it.**
Something replaced a symlink with a real file. Reopen `tui-claude` — the repair pass runs on every refresh and re-links it, telling you what it fixed.

**I switched profiles and Claude Code still uses the old account.**
A session was running during the switch. Quit Claude Code, switch, then start it again.

**A profile asks me to log in every time.**
Its `.credentials.json` is missing. Select it, press `L`, and log in once.

**Where did my conversations go after I joined a pool?**
Nowhere — they are in `~/.claude-profiles/_shared/projects/`, and every shared profile reads them. If a file genuinely conflicted it is in `<profile>/_archive-<timestamp>/`.

**I closed a pinned terminal's window instead of typing `exit`.**
Nothing is lost. Its pin is cleared the next time `tui-claude` opens, and any
settings it changed reach the other profiles the next time the profile is used.

**A profile cannot be renamed, removed or shared.**
It is pinned in another terminal (`◆` in the Status column). Type `exit` there first.

## Development

```bash
uv sync
uv run pytest
```

The test suite runs entirely against temporary directories and never touches your real `~/.claude`. To drive the app by hand against a sandbox:

```bash
TUI_CLAUDE_TEST_DIR=/tmp/tui-claude-sandbox uv run tui-claude
```

That environment variable relocates all three paths (`~/.claude`, `~/.claude.json` and the profiles directory) under the directory you name, so you can try destructive operations safely.

| Path | Contents |
|------|----------|
| `src/tui_claude/main.py` | TUI: state, key bindings, rendering, profile operations |
| `src/tui_claude/sharing.py` | Shared pool: merge, fork, repair, `claude.json` split/rebuild |
| `src/tui_claude/tmp_mode.py` | tmp mode: the `.claude.json` link, pinned terminals, the shell's exec |
| `tests/test_sharing.py` | Pool semantics, identity isolation, repair, locking |
| `tests/test_tui_integration.py` | TUI flows and rendering against a sandbox |
| `tests/test_tmp_mode.py` | tmp mode, including an end-to-end run of the real exec |

`sharing.py` has no dependency on `prompt_toolkit`, and the interactive dialogues take their input and output functions as arguments — so every behaviour is reachable from tests without a terminal.
