"""Shared data between profiles.

Profiles exist to switch accounts, not to fragment your work. This module lets
any number of profiles point at one pool of conversations, memory and settings
while each keeps its own credentials.

Layout, with `work` and `personal` sharing and `client-x` isolated:

    ~/.claude-profiles/
        _shared/                    <- the pool (reserved name, never a profile)
            projects/               <- conversations + memory
            settings.json
            claude.shared.json      <- claude.json minus anything account-bound
        work/
            projects -> ../_shared/projects
            settings.json -> ../_shared/settings.json
            .credentials.json       <- real file, never shared
            claude.account.json     <- identity split out of claude.json
            claude.json             <- rebuilt = shared + account, on every switch
        client-x/
            projects/               <- real directory, sees none of the above

Whether a profile shares is read from the filesystem itself (does anything in
it point into the pool?), so there is no config file to drift out of sync.
"""

import contextlib
import json
import os
import shutil
import time

from .links import IS_WINDOWS, is_link, link_dir, link_file, points_to

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

# Reserved directory inside PROFILES_DIR; refresh_profiles() must skip "_*".
POOL_NAME = "_shared"

# Everything a profile shares. Directories are symlinked wholesale; files are
# symlinked individually.
SHARED_DIRS = [
    "projects",         # conversations and auto-memory
    "plugins",
    "shell-snapshots",
    "session-env",
    "todos",
    "agents",
    "commands",
    "skills",
    "downloads",
    "ide",
]
SHARED_FILES = ["history.jsonl", "settings.json", "CLAUDE.md"]

# Never shared: .credentials.json, sessions/, backups/, policy-limits.json,
# remote-settings*.json, mcp-needs-auth-cache.json, claude.account.json.

# --- claude.json: the one file that mixes identity with configuration --------
#
# It holds oauthAccount and userID (per profile) alongside projects, trust
# dialogs and UI flags (shareable), so it cannot simply be symlinked. It is
# split in two and rebuilt on every switch.

ACCOUNT_KEYS = {
    "oauthAccount", "userID", "primaryApiKey", "customApiKeyResponses",
    "claudeCodeFirstTokenDate", "isQualifiedForDataSharing",
    "hasAvailableSubscription", "subscriptionNoticeCount",
    "subscriptionUpsellShownCount", "fallbackAvailableWarningThreshold",
    "modelAccessCache", "orgModelDefaultCache", "additionalModelCostsCache",
    "additionalModelOptionsCache", "metricsStatusCache",
    "cachedExtraUsageDisabledReason",
}
ACCOUNT_PREFIXES = ("oauth", "cached")
ACCOUNT_SUFFIXES = ("AccessCache",)

# Safety net for account keys a future Claude Code release may introduce under
# a name none of the rules above match: classify by content instead.
IDENTITY_FIELDS = ("accountUuid", "userID", "emailAddress", "organizationUuid")


def is_account_key(key):
    return (key in ACCOUNT_KEYS or key.startswith(ACCOUNT_PREFIXES)
            or key.endswith(ACCOUNT_SUFFIXES))


def is_usable_marker(value):
    """Long enough to be specific, so it cannot match unrelated text.

    An address is distinctive from far fewer characters than a hash is, and
    short addresses are common ("eu@example.net"), so it gets its own floor.
    """
    if not isinstance(value, str):
        return False
    if "@" in value:
        return len(value) >= 6
    return len(value) >= 12


def identity_markers(account):
    """The values that identify this account: uuid, userID, email."""
    marks = set()

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in IDENTITY_FIELDS and is_usable_marker(value):
                    marks.add(value)
                else:
                    collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(account)
    return marks


def carries_identity(value, marks):
    if not marks:
        return False
    blob = json.dumps(value, ensure_ascii=False)
    return any(mark in blob for mark in marks)


# --- small filesystem helpers ------------------------------------------------

def load_json(path):
    try:
        # Explicit: Windows defaults to cp1252, and a decode error here would be
        # swallowed below and returned as {} — then written back over the file.
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def pool_dir(profiles_dir):
    return os.path.join(profiles_dir, POOL_NAME)


@contextlib.contextmanager
def pool_lock(profiles_dir):
    """Serialise read-modify-write on the pool's shared config.

    save_json is already atomic, so an unlocked write cannot corrupt the file
    — but two TUIs that read, merge and write around each other would drop one
    of the two updates. This closes that window.
    """
    pool = pool_dir(profiles_dir)
    os.makedirs(pool, exist_ok=True)
    handle = os.open(os.path.join(pool, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _lock(handle)
        yield
    finally:
        _unlock(handle)
        os.close(handle)


def _lock(fd):
    if IS_WINDOWS:
        # Locks a byte range, not the file; always the first byte. LK_LOCK
        # gives up after ten one-second retries, so keep retrying ourselves.
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd):
    if IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def pool_exists(profiles_dir):
    return os.path.isdir(pool_dir(profiles_dir))


def is_shared(profiles_dir, profile):
    """A profile shares when any of its items points into the pool.

    Deliberately "any" and not "projects": an atomic write can turn a single
    link back into a real file, and if that item were the sentinel the profile
    would read as isolated and never get repaired.
    """
    profile_path = os.path.join(profiles_dir, profile)
    pool = pool_dir(profiles_dir)
    for name in SHARED_DIRS + SHARED_FILES:
        if points_to(os.path.join(profile_path, name), os.path.join(pool, name)):
            return True
    return False


def shared_profiles(profiles_dir, profiles):
    return [p for p in profiles if is_shared(profiles_dir, p)]


def dir_size(path):
    """Bytes under `path`, not following symlinks."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        # os.walk descends into junctions even with followlinks=False.
        dirs[:] = [d for d in dirs if not is_link(os.path.join(root, d))]
        for name in files:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
    return total


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def archive_dir(profile_path):
    return os.path.join(profile_path, f"_archive-{time.strftime('%Y%m%d-%H%M%S')}")


# --- merging into the pool ---------------------------------------------------

def merge_tree(src, dst, archive):
    """Union `src` into `dst`.

    Conversations, snapshots and session state are named by UUID or timestamp,
    so they merge without collision. A file that would overwrite a different
    file already in the pool is moved to `archive` instead, byte for byte.
    Returns (merged, archived) counts.
    """
    merged = archived = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            source = os.path.join(root, name)
            target = os.path.join(target_root, name)
            if not os.path.exists(target):
                shutil.copy2(source, target)
                merged += 1
            elif not files_equal(source, target):
                kept = os.path.join(archive, rel if rel != "." else "", name)
                os.makedirs(os.path.dirname(kept), exist_ok=True)
                shutil.copy2(source, kept)
                archived += 1
    return merged, archived


def files_equal(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def merge_history(src, dst):
    """history.jsonl is append-only: union the lines, keeping order."""
    seen, out = set(), []
    for path in (dst, src):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip() and line not in seen:
                    seen.add(line)
                    out.append(line)
    with open(dst, "w", encoding="utf-8") as handle:
        handle.writelines(out)


# --- claude.json split / rebuild ---------------------------------------------

def compute_delta(base, now):
    """What changed from `base` to `now`, as nested paths."""
    sets, deletes = [], []

    def walk(before, after, path):
        for key, value in after.items():
            if key not in before:
                sets.append((path + (key,), value))
            elif before[key] != value:
                if isinstance(before[key], dict) and isinstance(value, dict):
                    walk(before[key], value, path + (key,))
                else:
                    sets.append((path + (key,), value))
        for key in before:
            if key not in after:
                deletes.append(path + (key,))

    walk(base, now, ())
    return sets, deletes


def apply_delta(target, sets, deletes):
    """Apply only this session's changes, preserving what another wrote."""
    for path, value in sets:
        node = target
        for key in path[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = value
    for path in deletes:
        node = target
        for key in path[:-1]:
            node = node.get(key)
            if not isinstance(node, dict):
                break
        else:
            node.pop(path[-1], None)
    return target


def split_claude_json(profiles_dir, profile):
    """Push this profile's shareable config into the pool, keep identity local.

    Writes a delta against the baseline saved at the last rebuild rather than
    overwriting, so two profiles that changed different keys do not erase each
    other. Returns the list of keys withheld by the identity guard.
    """
    profile_path = os.path.join(profiles_dir, profile)
    data = load_json(os.path.join(profile_path, "claude.json"))
    if not data:
        return []

    shared_path = os.path.join(pool_dir(profiles_dir), "claude.shared.json")
    account_path = os.path.join(profile_path, "claude.account.json")
    baseline_path = os.path.join(profile_path, "claude.baseline.json")

    account = {k: v for k, v in data.items() if is_account_key(k)}
    already_local = load_json(account_path)
    marks = identity_markers(account)

    with pool_lock(profiles_dir):
        shared = load_json(shared_path)

        common, withheld = {}, []
        for key, value in data.items():
            if is_account_key(key):
                continue
            # A key the pool has never seen that carries this account's
            # identity stays behind. Restricting it to new keys matters: an
            # email can appear inside `projects` (someone typed it in a
            # prompt), and hijacking that whole key would break the sharing it
            # is supposed to protect.
            if key not in shared and carries_identity(value, marks):
                account[key] = value
                if key not in already_local:
                    withheld.append(key)
            else:
                common[key] = value

        if os.path.exists(baseline_path):
            sets, deletes = compute_delta(load_json(baseline_path), common)
            shared = apply_delta(shared, sets, deletes)
        else:
            shared.update(common)  # first time: nothing to diff against

        save_json(shared_path, shared)

    save_json(account_path, account)
    save_json(baseline_path, common)
    return withheld


def rebuild_claude_json(profiles_dir, profile):
    """Rebuild <profile>/claude.json = pool config + this profile's identity."""
    profile_path = os.path.join(profiles_dir, profile)
    shared = load_json(os.path.join(pool_dir(profiles_dir), "claude.shared.json"))
    account = load_json(os.path.join(profile_path, "claude.account.json"))
    if not shared and not account:
        return

    merged = dict(shared)
    merged.update(account)
    if not account:
        # A profile with no identity of its own must not inherit another's.
        for key in [k for k in merged if is_account_key(k)]:
            del merged[key]

    save_json(os.path.join(profile_path, "claude.json"), merged)
    save_json(os.path.join(profile_path, "claude.baseline.json"), shared)


# --- the three operations the TUI calls --------------------------------------

def enable_sharing(profiles_dir, profile):
    """Point a profile at the pool, carrying its data in.

    Seeds the pool if empty; otherwise merges. Returns a summary dict.
    """
    profile_path = os.path.join(profiles_dir, profile)
    pool = pool_dir(profiles_dir)
    seeded = not os.path.isdir(pool)
    os.makedirs(pool, exist_ok=True)

    archive = archive_dir(profile_path)
    merged = archived = 0

    for name in SHARED_DIRS:
        local = os.path.join(profile_path, name)
        target = os.path.join(pool, name)
        if is_link(local):
            os.unlink(local)
        elif os.path.isdir(local):
            if not os.path.exists(target):
                shutil.move(local, target)
            else:
                got, lost = merge_tree(local, target, os.path.join(archive, name))
                merged += got
                archived += lost
                shutil.rmtree(local)
        os.makedirs(target, exist_ok=True)
        link_dir(target, local)

    for name in SHARED_FILES:
        local = os.path.join(profile_path, name)
        target = os.path.join(pool, name)
        if is_link(local) or points_to(local, target):
            os.unlink(local)
        elif os.path.isfile(local):
            if not os.path.exists(target):
                shutil.move(local, target)
            elif name == "history.jsonl":
                merge_history(local, target)   # append-only: union the lines
                os.remove(local)
                merged += 1
            elif files_equal(local, target):
                os.remove(local)
            else:
                os.makedirs(archive, exist_ok=True)
                shutil.move(local, os.path.join(archive, name))
                archived += 1
        # Link even when neither side has the file yet: a symlink may point at
        # a target that does not exist, and writing through it creates the
        # target inside the pool. Without this, the first settings.json the app
        # writes would land in the profile and quietly escape sharing.
        link_file(target, local)

    withheld = split_claude_json(profiles_dir, profile)
    return {"seeded": seeded, "merged": merged, "archived": archived,
            "archive": archive if archived else None, "withheld": withheld}


def disable_sharing(profiles_dir, profile, take_copy):
    """Detach a profile from the pool.

    take_copy=True forks: the profile keeps a full copy of the pool as it is
    now and diverges from there. take_copy=False leaves it empty.
    """
    profile_path = os.path.join(profiles_dir, profile)
    pool = pool_dir(profiles_dir)
    copied = 0

    # Materialise a standalone claude.json before dropping the link to the pool.
    rebuild_claude_json(profiles_dir, profile)
    for leftover in ("claude.baseline.json",):
        path = os.path.join(profile_path, leftover)
        if os.path.exists(path):
            os.remove(path)

    for name in SHARED_DIRS:
        local = os.path.join(profile_path, name)
        source = os.path.join(pool, name)
        if is_link(local):
            os.unlink(local)
        if take_copy and os.path.isdir(source):
            shutil.copytree(source, local, symlinks=True)
            copied += sum(len(f) for _, _, f in os.walk(local))
        else:
            os.makedirs(local, exist_ok=True)

    for name in SHARED_FILES:
        local = os.path.join(profile_path, name)
        source = os.path.join(pool, name)
        if is_link(local) or points_to(local, source):
            os.unlink(local)
        if take_copy and os.path.isfile(source):
            shutil.copy2(source, local)
            copied += 1

    return {"copied": copied}


def repair_sharing(profiles_dir, profile):
    """Re-link items that silently fell out of the pool.

    An atomic write (write to .tmp, rename over the target) replaces a symlink
    with a real file, so a profile can stop sharing one file without any error
    being raised. The content that landed in the profile is the newer one, so
    it is promoted into the pool before the link is restored. Returns the names
    that had to be repaired.
    """
    if not is_shared(profiles_dir, profile):
        return []

    profile_path = os.path.join(profiles_dir, profile)
    pool = pool_dir(profiles_dir)
    repaired = []

    for name in SHARED_DIRS + SHARED_FILES:
        local = os.path.join(profile_path, name)
        target = os.path.join(pool, name)
        if is_link(local) or points_to(local, target) or not os.path.exists(local):
            continue

        if os.path.isdir(local):
            merge_tree(local, target, os.path.join(archive_dir(profile_path), name))
            shutil.rmtree(local)
        elif name == "history.jsonl":
            merge_history(local, target)
            os.remove(local)
        elif not os.path.exists(target) or files_equal(local, target):
            shutil.move(local, target)
        else:
            # Both sides hold different content. The profile's copy is usually
            # the fresher one, but another profile may have written to the pool
            # in the meantime, so compare instead of assuming — and keep the
            # loser rather than discarding it.
            archive = archive_dir(profile_path)
            os.makedirs(archive, exist_ok=True)
            if os.path.getmtime(local) >= os.path.getmtime(target):
                shutil.copy2(target, os.path.join(archive, name))
                shutil.move(local, target)
            else:
                shutil.move(local, os.path.join(archive, name))

        if name in SHARED_DIRS:
            os.makedirs(target, exist_ok=True)
        if os.path.exists(target):
            (link_dir if name in SHARED_DIRS else link_file)(target, local)
            repaired.append(name)

    return repaired


def sync_on_switch(profiles_dir, leaving, entering):
    """Hand the pool the outgoing profile's config, then dress the incoming one.

    Called at the moment the TUI flips the symlink, which is the only instant
    where both profiles are known and neither is in use.
    """
    if leaving and leaving != entering and is_shared(profiles_dir, leaving):
        split_claude_json(profiles_dir, leaving)
    if entering and is_shared(profiles_dir, entering):
        rebuild_claude_json(profiles_dir, entering)


def pool_summary(profiles_dir):
    """Size of the pool and what it holds, for confirmation dialogs."""
    pool = pool_dir(profiles_dir)
    if not os.path.isdir(pool):
        return None
    conversations = 0
    projects = os.path.join(pool, "projects")
    for root, _, files in os.walk(projects):
        conversations += sum(1 for f in files if f.endswith(".jsonl"))
    return {"bytes": dir_size(pool), "conversations": conversations}
