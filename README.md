# tui-claude

Terminal UI for managing multiple Claude Code login profiles.

Each profile is a directory under `~/.claude-profiles/`. The active one is
symlinked to `~/.claude` (and its `claude.json` to `~/.claude.json`), so
switching profiles switches which account Claude Code uses.

```
tui-claude
```

| Key | Action |
|-----|--------|
| `Enter` | Switch to the selected profile |
| `A` | Add a profile |
| `R` | Remove a profile |
| `L` | Run the login command for the selected profile |
| `S` | Share or isolate the selected profile's data |
| `C` | Change the login command |
| `Q` | Quit |

## Sharing data between profiles

Profiles exist to switch **accounts**. By default they also split everything
else — conversations, memory, settings, skills — which is rarely what you want:
switch to your other account and your work history is gone.

Press `S` on a profile to put it in the shared pool. Profiles in the pool read
and write the same conversations, memory, settings and skills, while each keeps
its own credentials and account identity.

```
 ┌──────────────────────────────┬───────────────┬──────────────┐
 │ Profile Name                 │ Status        │ Data         │
 ├──────────────────────────────┼───────────────┼──────────────┤
 │ cliente-x                    │   inactive    │ ⊘ isolated   │
 │ pessoal                      │   inactive    │ ⇄ shared     │
 │ trabalho                     │ ● ACTIVE      │ ⇄ shared     │
 └──────────────────────────────┴───────────────┴──────────────┘
 Shared pool: 7 conversations, 5.5 KB, 2 profile(s)
```

Sharing is per profile, so `cliente-x` above sees none of it.

### What is shared, what is not

| Shared | Never shared |
|--------|--------------|
| `projects/` — conversations and auto-memory | `.credentials.json` — auth tokens |
| `settings.json`, `CLAUDE.md` | `oauthAccount`, `userID` and account caches |
| `agents/`, `commands/`, `skills/`, `plugins/` | `sessions/`, `policy-limits.json`, `remote-settings*.json` |
| `history.jsonl`, `shell-snapshots/`, `todos/` | |

The pool lives in `~/.claude-profiles/_shared/`. Names starting with `_` are
reserved and never listed as profiles.

`claude.json` is the awkward one: it mixes account identity with shareable
configuration (trust dialogs, per-project permissions, UI flags) in a single
file, so it cannot simply be symlinked. It is split into
`_shared/claude.shared.json` and each profile's `claude.account.json`, then
rebuilt whenever you switch. Anything account-bound is filtered out by name,
plus a content check that catches a key holding this account's email or UUID
under a name the name rules do not know.

### Joining and leaving

**Joining** with an empty pool: your data *becomes* the pool, untouched.

**Joining** an existing pool: conversations, memory and snapshots merge — they
are named by UUID or timestamp, so they never collide. A settings file that
disagrees with the pool's is moved to `<profile>/_archive-<timestamp>/` byte for
byte. Nothing is deleted.

**Leaving** asks what the profile keeps:

- **Keep a copy** — forks the pool into the profile, which diverges from there.
- **Start empty** — the profile begins with no conversations.

Either way the pool is untouched and the other profiles keep reading it.

Deleting a shared profile removes only that profile. The pool survives, even
when the profile deleted was the last one using it — orphaning data is
recoverable, deleting it is not.

## Development

```bash
uv sync
uv run pytest
```

Tests run against a sandbox directory, never your real `~/.claude`. Point the
app at one the same way to try things out by hand:

```bash
TUI_CLAUDE_TEST_DIR=/tmp/tui-claude-sandbox uv run tui-claude
```
