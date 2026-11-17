#!/usr/bin/env python3
"""Shared library for the Project Tasks system.

The filesystem is the source of truth. One Markdown file per task; the task's
status is the directory it lives in. This module knows how to read, validate
and move those files. All liveness and locking logic lives in bin/tick.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portable  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
PROJECTS_DIR = ROOT / "projects"
INBOX = ROOT / "inbox"
ARCHIVE = ROOT / "archive"
NOTIFY_FILE = ROOT / "NOTIFY.md"
CONFIG_FILE = Path(os.environ.get("CLAUDE_TASKS_CONFIG", ROOT / "config.json"))


# --------------------------------------------------------------------------
# where the board sits
# --------------------------------------------------------------------------
#
# A board is in one of two shapes, and it can tell which by looking at itself
# rather than being told.
#
#   standalone  its own git repository, `projects/<name>/` inside it, each
#               project naming a `repo_path` somewhere else on the machine.
#               One board, many repositories, work across all of them.
#
#   embedded    a `.tasks/` directory inside somebody else's repository. There
#               is exactly one project - the repository around it - so the board
#               root *is* the project directory: `.tasks/tasks/<status>/`,
#               `.tasks/project.md`, `.tasks/journal.md`, no `projects/` level
#               to walk through to reach a single entry.
#
# Embedded is the shape an agent wants. Handed one directory it sees the code
# and the tasks about that code together, which is the thing that no amount of
# `repo_path` configuration buys you: the task and the file it is about are the
# same `git log`, the same clone, the same review.
#
# The test is "is my own directory inside a git repository that is not me". No
# marker file, because a marker file can be copied to somewhere it is not true;
# `git rev-parse` cannot be wrong about this.

_GIT_TOPLEVEL: list = []          # a one-slot cache; [] is "not asked yet"


def git_toplevel() -> Path | None:
    """The root of the git repository containing the board, if there is one."""
    if _GIT_TOPLEVEL:
        return _GIT_TOPLEVEL[0]
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    top = None
    if result.returncode == 0 and result.stdout.strip():
        top = Path(result.stdout.strip()).resolve()
    _GIT_TOPLEVEL.append(top)
    return top


def host_repo() -> Path | None:
    """The repository this board is embedded in, or None when it is standalone.

    An override exists for one real case: a board vendored into a repository
    that is not itself a git checkout yet, or a fixture. `PROJECT_TASKS_HOST`
    names the directory; `0` forces standalone.
    """
    override = os.environ.get("PROJECT_TASKS_HOST")
    if override is not None:
        return None if override in ("", "0") else Path(override).resolve()
    top = git_toplevel()
    if top is None or top == ROOT.resolve():
        return None
    return top


def is_embedded() -> bool:
    return host_repo() is not None


def board_project_dir() -> Path | None:
    """The board root, when the board root is itself the one project.

    Written as a filesystem question rather than as `if is_embedded()` so that
    the two can never disagree: what makes a directory a project on this board
    is that it holds `tasks/`, and that is as true of the root as of any other.
    """
    return ROOT if (ROOT / "tasks").is_dir() else None

STATUSES = [
    "backlog",
    "ready",
    "in-progress",
    "blocked",
    "needs-human",
    "review",
    "done",
    "failed",
    "cancelled",
]

# Statuses that count as "the task is finished, stop scheduling it".
CLOSED = {"done", "cancelled"}

# Allowed state machine edges. Anything not listed is rejected by `move`.
TRANSITIONS = {
    "backlog": {"ready", "blocked", "needs-human", "cancelled"},
    "ready": {"in-progress", "blocked", "backlog", "needs-human", "cancelled"},
    "in-progress": {"review", "done", "failed", "blocked", "needs-human", "ready", "cancelled"},
    "blocked": {"ready", "backlog", "needs-human", "cancelled"},
    "needs-human": {"ready", "backlog", "blocked", "cancelled"},
    "review": {"done", "ready", "failed", "needs-human", "cancelled"},
    "failed": {"ready", "needs-human", "cancelled"},
    "done": {"ready"},        # reopen
    "cancelled": {"backlog", "ready"},
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# What kind of work this is, and roughly how much of it. Both are part of the
# task schema rather than free tags because views and lint read them: a board
# where "type" means whatever each author felt like cannot be filtered, and
# `lint` cannot tell a missing field from a misspelt one.
TASK_TYPES = ["bug", "feature", "chore", "research", "ops"]
TASK_SIZES = ["S", "M", "L"]

DEFAULT_CONFIG = {
    # Where this machine keeps the working copies a standalone board's projects
    # point at. Empty by default and derived when it is: shipping one person's
    # directory layout as the default tells every other board something untrue
    # about itself. Only the grooming prompt reads it, and only to say where to
    # look; a task carries its own repo_path.
    "projects_root": "",
    # Which agent the wrapper runs, and how to talk to it. The loop needs three
    # things from an agent CLI: it runs non-interactively, it takes a prompt, and
    # it prints something while it works. Everything else is per-profile.
    "agent": "claude",
    "agents": {
        "claude": {
            "command": ["claude", "--permission-mode", "bypassPermissions",
                        "--output-format", "stream-json", "--verbose"],
            "prompt": "flag:-p",
            "workdir_flag": "--add-dir",
            "stream": "claude-json",
        },
        "opencode": {
            "command": ["opencode", "run"],
            "prompt": "arg",
            "workdir_flag": None,
            "stream": "text",
        },
    },
    "stall_timeout_sec": 600,      # silence on the agent's event stream = stalled
    "stall_strikes": 2,            # consecutive ticks of silence before killing
    "max_wall_sec": 7200,          # hard ceiling for one task attempt
    # Hard ceiling for one tick, which works task after task instead of stopping
    # after one. Reaching it ends the tick cleanly between tasks and lets launchd
    # start a fresh one; 0 means no ceiling.
    "max_tick_wall_sec": 21600,
    "heartbeat_flush_sec": 30,     # how often worker.json is written to disk
    "sigterm_grace_sec": 30,       # wait after SIGTERM before SIGKILL
    "groom_interval_sec": 3600,    # idle grooming cadence
    "board_refresh_sec": 15,       # how often the served web board refreshes itself
    "usage_limit_backoff_sec": 3600,   # wait this long when the plan's usage limit is hit
    # Refunded attempts per task before the ceiling comes back. One cap for the
    # whole infra class - plan limit, network, credentials, missing working copy,
    # full disk - because a task cannot tell which detector was wrong about it,
    # and per-cause caps would let causes take turns and never reach the ceiling.
    "infra_max_refunds": 3,
    "retry_backoff_sec": 600,          # first delay before a requeued attempt runs again
    "retry_backoff_max_sec": 21600,    # ceiling for that exponential backoff
    "archive_after_days": 30,
    "default_max_attempts": 3,
    # How much of its own history a task file carries. The full log lives in the
    # `<id>.log.jsonl` sidecar; keeping every line in the body meant that by the
    # tenth tick most of what the agent read was its own "checkpoint saved" lines.
    "task_log_kept": 8,            # log entries left in the task body
    "task_attempts_kept": 5,       # lines kept in the `## Attempts` section
    "project_brief_chars": 1400,   # how much of project.md the prompt inlines
    "local_policy_chars": 2000,    # how much of LOCAL.md the prompt inlines
    # The project journal: one paragraph per task that landed, newest first. The
    # entry cap is the point of it - a record that may grow without limit turns
    # into documentation, and documentation is the thing nobody updates.
    "journal_brief_chars": 1600,   # how much of journal.md the prompt inlines
    "journal_entry_chars": 700,    # the most one journal entry may hold
    # How many tasks may be in flight at once. Each one still needs its own
    # resource free: two runs never share a repository, however many slots exist.
    "max_concurrent_tasks": 1,
    "max_new_tasks_per_tick": 5,
    "max_subtask_depth": 3,
    # A day's ceiling on what the loop may spend, in US dollars. 0 means none,
    # which is the default: a board that has never watched its own spending
    # should not discover a limit it did not set. bin/tick records every run's
    # cost in .runtime/spend.json and, above this, holds until the day turns.
    "daily_spend_limit_usd": 0,
    # A desktop banner where the platform has one: osascript, notify-send or a
    # WinRT toast. `notify_macos` is the old name and is still honoured, from
    # when a banner was the only kind this board could raise.
    "notify_desktop": True,
    "notify_file": True,
    "git_autocommit": True,
}


# The order every view shows statuses in: what needs a human first, what is
# waiting last. Both the terminal board and the web board read this, so they
# cannot drift apart.
# Ready sits last: it is the longest column and the one you scan least often, so
# putting it after Done keeps the states that need attention near the left edge.
BOARD_COLUMNS = [
    ("in-progress", "In progress"),
    ("review", "Review"),
    ("needs-human", "Needs you"),
    ("blocked", "Blocked"),
    ("backlog", "Backlog"),
    ("failed", "Failed"),
    ("done", "Done"),
    ("ready", "Ready"),
]


# --------------------------------------------------------------------------
# config / time helpers
# --------------------------------------------------------------------------

_CONFIG_CACHE: dict | None = None


# Old name -> current name. `usage_limit_max_refunds` was the cap when the plan
# limit was the only failure that refunded an attempt; the cap now belongs to
# the whole infra class.
RENAMED_CONFIG_KEYS = {"usage_limit_max_refunds": "infra_max_refunds"}


def load_config(refresh: bool = False) -> dict:
    """Read config.json once per process; it is hot on every task property."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not refresh:
        return _CONFIG_CACHE
    cfg = dict(DEFAULT_CONFIG)
    stated = {}
    if CONFIG_FILE.exists():
        try:
            stated = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"warning: config.json is not valid JSON ({exc}); using defaults",
                  file=sys.stderr)
    for old_key, new_key in RENAMED_CONFIG_KEYS.items():
        # Only what the file actually states can be renamed forward - the
        # defaults already carry the new name, so comparing against the merged
        # dict would find it present every time and drop the old setting on the
        # floor. A silently ignored knob looks exactly like a knob that does
        # nothing, which is the bug a rename is supposed to avoid.
        if old_key in stated and new_key not in stated:
            stated[new_key] = stated[old_key]
    cfg.update(stated)
    _CONFIG_CACHE = cfg
    return cfg


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def boot_id() -> str:
    """Identifies the current boot, so monotonic timestamps are comparable."""
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5).stdout
        match = re.search(r"sec\s*=\s*(\d+)", out)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


DEFAULT_PROFILE = {
    "command": [],
    "prompt": "arg",          # "arg" | "flag:-p" | "stdin"
    "workdir_flag": None,     # repeated per extra directory when set
    "stream": "text",         # "claude-json" parses result events for cost and limits
}


def agent_profile(config: dict | None = None) -> dict:
    """How to launch the configured agent.

    A checkout written before profiles existed carries `claude_bin`/`claude_args`
    instead; that still works, and describes the same thing.
    """
    config = config or load_config()
    name = str(config.get("agent") or "claude")
    profile = (config.get("agents") or {}).get(name)
    # `claude_bin` is not a default any more, so its presence means a config file
    # written before profiles existed - and someone who set it meant it. It wins
    # over the stock claude profile rather than being silently ignored.
    if config.get("claude_bin") and name == "claude":
        profile = {
            "command": [config["claude_bin"], *config.get("claude_args", [])],
            "prompt": "flag:-p",
            "workdir_flag": "--add-dir",
            "stream": "claude-json",
        }
        name = "claude (from claude_bin/claude_args)"
    if profile is None:
        raise SystemExit(f"config error: no agent profile named {name!r}; "
                         f"known profiles: {', '.join(sorted(config.get('agents') or {})) or 'none'}")
    resolved = dict(DEFAULT_PROFILE)
    resolved.update(profile)
    resolved["name"] = name
    if not resolved["command"]:
        raise SystemExit(f"config error: agent profile {name!r} has no command")
    return resolved


def agent_command(profile: dict, prompt: str, extra_dirs=()) -> tuple[list[str], str | None]:
    """The argv to run, and what to write to stdin (None when the prompt is argv).

    Kept apart from the running of it so that it can be read - and tested -
    without starting an agent.
    """
    command = list(profile["command"])
    if profile.get("workdir_flag"):
        for directory in extra_dirs:
            if directory:
                command += [profile["workdir_flag"], directory]
    mode = str(profile.get("prompt") or "arg")
    if mode == "stdin":
        return command, prompt
    if mode.startswith("flag:"):
        return command + [mode.split(":", 1)[1], prompt], None
    return command + [prompt], None


# --------------------------------------------------------------------------
# runtime state
#
# `.runtime/` belongs to the wrapper. These helpers are how everything else -
# the CLI, the web board - reads it, so that "is a hold in force" has one
# answer rather than one per reader.
# --------------------------------------------------------------------------

def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload) -> None:
    """Write via a temporary file so a reader never sees a half-written state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def projects_root() -> str:
    """Where this board's working copies live, for the grooming prompt to say.

    Configured, or worked out: an embedded board's is the repository it is in,
    and a standalone board's is the directory holding the checkouts its projects
    point at - which is a fact about the projects and not something anybody
    should have to write down twice.
    """
    configured = str(load_config().get("projects_root") or "").strip()
    if configured:
        return os.path.expanduser(configured)
    host = host_repo()
    if host is not None:
        return str(host)
    parents = {str(repo.parent) for repo in
               (resolve_repo(project_meta(d).get("repo_path") or "")
                for d in iter_project_dirs()) if repo is not None}
    if len(parents) == 1:
        return parents.pop()
    return "(not set; each task names its own repository)"


def resolve_repo(repo_path: str) -> Path | None:
    """A project's `repo_path` as an absolute directory, or None when unset.

    A relative path is relative to the *board*, never to whatever directory the
    command happened to be run from. That is what makes `repo_path: ..` work at
    all: an embedded board's project points at the repository around it, and a
    relative path is the only spelling that survives the repository being
    cloned to another machine, renamed, or checked out under a different user -
    which a path committed as `/Users/someone/code/myapp` does not.
    """
    text = str(repo_path or "").strip()
    if not text:
        return None
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = ROOT / path
    return Path(os.path.normpath(path))


def repo_state(repo_path: str) -> dict:
    """Where a project's working copy is standing right now.

    Runs share this checkout and none of them may move it, so the branch belongs
    next to the project rather than being discovered after something has gone
    sideways.
    """
    path = resolve_repo(repo_path)
    if path is None:
        return {"path": "", "exists": False, "branch": "", "dirty": 0}
    if not (path / ".git").exists():
        return {"path": str(path), "exists": False, "branch": "", "dirty": 0}

    def run(*args):
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True, timeout=10)

    try:
        branch = run("branch", "--show-current").stdout.strip() or "detached HEAD"
        dirty = len([line for line in run("status", "--porcelain").stdout.splitlines() if line])
    except (OSError, subprocess.SubprocessError):
        return {"path": str(path), "exists": True, "branch": "?", "dirty": 0}
    return {"path": str(path), "exists": True, "branch": branch, "dirty": dirty}


def live_workers() -> list[dict]:
    """Every run in flight right now, as the wrapper recorded it.

    A worker file whose process is gone is not a run; it is litter from one, and
    the next tick clears it while reclaiming the task it left behind.
    """
    workers = []
    for path in sorted((RUNTIME / "workers").glob("*.json")):
        worker = read_json(path, {}) or {}
        pid = int(worker.get("pid") or 0)
        if not pid:
            continue
        try:
            os.kill(pid, 0)
        except OSError as exc:
            import errno
            if exc.errno != errno.EPERM:
                continue
        workers.append(worker)
    return workers


def usage_limit_hold() -> dict:
    """The plan-limit hold if one is in force right now, otherwise empty."""
    state = read_json(RUNTIME / "usage_limit.json", {}) or {}
    try:
        until = float(state.get("until", 0) or 0)
    except (TypeError, ValueError):
        return {}
    return state if until > time.time() else {}


# --------------------------------------------------------------------------
# minimal frontmatter parsing (no third-party dependencies on purpose)
# --------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)\Z", re.S)

KEY_ORDER = [
    "id", "title", "project", "status", "type", "size", "priority", "owner",
    "created", "updated", "started", "closed",
    "depends_on", "ready_after", "due", "parent", "depth",
    "attempts", "max_attempts", "refunds", "lease_generation", "checkpoint_at",
    "journal_at", "last_error", "tags",
]


def _scalar(raw: str):
    raw = raw.strip()
    if raw == "":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def _parse_meta_block(block: str) -> dict:
    meta: dict = {}
    last_key = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and last_key is not None:
            meta.setdefault(last_key, [])
            if not isinstance(meta[last_key], list):
                meta[last_key] = [meta[last_key]] if meta[last_key] is not None else []
            meta[last_key].append(_scalar(line.lstrip()[2:]))
            continue
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        meta[key] = _scalar(raw)
        last_key = key
    return meta


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    return _parse_meta_block(match.group(1)), match.group(2)


def read_frontmatter(path: Path) -> tuple[dict, str | None]:
    """Read a task file's metadata without reading its body.

    Almost every board-wide operation - list, next, doctor, the index - wants
    only frontmatter, but a task file is mostly log: the bodies on a 500-task
    board are megabytes that no scan ever looks at. Stopping at the closing
    `---` makes a scan cost the number of tasks instead of their history.

    Returns `(meta, body)` with `body` None when the body was not read; a file
    with no usable frontmatter has no such shortcut, so it comes back whole.
    """
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline()
        if first.rstrip("\n").rstrip(" \t") != "---":
            return {}, first + handle.read()
        block = []
        for line in handle:
            if line.rstrip("\n").rstrip(" \t") == "---":
                return _parse_meta_block("".join(block)), None
            block.append(line)
    # Unterminated frontmatter is not frontmatter; fall back to the whole file
    # so the caller sees exactly what parse_frontmatter() would have said.
    return {}, path.read_text(encoding="utf-8")


def _emit(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value if v is not None) + "]"
    return str(value)


def dump_frontmatter(meta: dict, body: str) -> str:
    keys = [k for k in KEY_ORDER if k in meta]
    keys += [k for k in meta if k not in keys]
    lines = [f"{k}: {_emit(meta[k])}".rstrip() for k in keys]
    return "---\n" + "\n".join(lines) + "\n---\n" + body


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------

SKIP_DIRS = {".git", ".runtime", ".claude", "archive", "bin", "lib", "prompts", "node_modules"}


def iter_project_dirs():
    """A project is any directory that contains a `tasks/` subdirectory.

    Including the board root itself, which is what an embedded board looks
    like: one project, and no `projects/<name>/` level to walk down to reach it.
    """
    board = board_project_dir()
    if board is not None:
        yield board
    stack = [ROOT]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir()):
            if not child.is_dir() or child.name in SKIP_DIRS or child.name.startswith("."):
                continue
            if (child / "tasks").is_dir():
                yield child
            stack.append(child)


_PROJECT_NAMES: dict[str, str] = {}
_PROJECT_META: dict[str, dict] = {}


def project_name(project_dir: Path) -> str:
    """Cached: `Task.project` asks for this once per task, and it resolves a path."""
    key = str(project_dir)
    name = _PROJECT_NAMES.get(key)
    if name is None:
        rel = project_dir.resolve().relative_to(ROOT)
        name = "/".join(p for p in rel.parts if p != "projects")
        if not name:
            # The board root as a project: its path says nothing, because it is
            # the thing every other path is relative to. Its `project.md` names
            # it, and failing that the repository it is embedded in does.
            declared = str(project_meta(project_dir).get("name") or "").strip()
            host = host_repo()
            name = declared or (host.name if host else ROOT.parent.name)
        _PROJECT_NAMES[key] = name
    return name


def project_dir_for(name: str) -> Path | None:
    name = name.strip("/")
    for candidate in iter_project_dirs():
        if project_name(candidate) == name:
            return candidate
    return None


def default_project() -> str | None:
    """The project a command may leave unsaid, when there is only one to mean.

    Embedded, that is the whole point: there is one project, it is the
    repository the board is sitting in, and `--project` would be a word typed
    on every command to name the only thing it could name. Standalone with
    several projects it stays required, because there the word carries a
    decision.
    """
    names = [project_name(d) for d in iter_project_dirs()]
    return names[0] if len(names) == 1 else None


def project_meta(project_dir: Path, refresh: bool = False) -> dict:
    """Cached for the life of the process, like `load_config`.

    `resource_key` and `Task.project` ask for a project's metadata once per
    task, and re-reading `project.md` several hundred times to answer the same
    question is most of what a board scan used to spend its time on. A process
    that rewrites a project.md passes `refresh=True`.
    """
    key = str(project_dir)
    if not refresh and key in _PROJECT_META:
        return _PROJECT_META[key]
    project_file = project_dir / "project.md"
    if not project_file.exists():
        _PROJECT_META[key] = {}
        return {}
    meta, _ = read_frontmatter(project_file)
    _PROJECT_META[key] = meta
    return meta


def ensure_project(name: str, repo_path: str | None = None, prefix: str | None = None) -> Path:
    existing = project_dir_for(name)
    if existing:
        return existing
    parts = name.strip("/").split("/")
    path = PROJECTS_DIR
    for index, part in enumerate(parts):
        path = path / part if index == 0 else path / "projects" / part
    for status in STATUSES:
        (path / "tasks" / status).mkdir(parents=True, exist_ok=True)
    (path / "artifacts").mkdir(exist_ok=True)
    if not (path / "project.md").exists():
        auto_prefix = prefix or "".join(p[0] for p in parts)[:3].upper() or "GEN"
        meta = {
            "name": name,
            "prefix": auto_prefix,
            "repo_path": repo_path or "",
            "created": utcnow(),
        }
        body = (
            "\n## Context\n\nWhat this project is and why it exists.\n"
            "\n## Definition of done\n\n- Tests pass\n- Change is committed\n"
            "\n## Conventions\n\n- \n"
            "\n## Constraints\n\n- \n"
        )
        (path / "project.md").write_text(dump_frontmatter(meta, body), encoding="utf-8")
        # A project.md that did not exist when something last asked for it is
        # cached as "no metadata"; drop that answer now it is wrong.
        _PROJECT_META.pop(str(path), None)
    return path


def derive_prefix(name: str) -> str:
    """A task id prefix from a repository name: `webhook-relay` -> `WR`.

    Initials when there are several words, the first three letters when there is
    one. A single word taken by initials gives a one-letter prefix, and `M-001`
    is not an id anybody can recognise in a commit message six months later -
    which is the entire job of the prefix.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    if not words:
        return "GEN"
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words)[:4].upper()


def init_embedded(prefix: str | None = None, name: str | None = None) -> Path:
    """Make the board root its own project. What `bin/task init` does.

    The same directories `ensure_project` makes, one level up: `tasks/<status>/`
    and `artifacts/` beside `project.md` rather than under
    `projects/<name>/`. An embedded board has exactly one project and no use
    for a directory whose only job is to tell several of them apart.

    `repo_path` is written as `..` and stays relative, so the board keeps
    working after the repository is cloned somewhere else, renamed, or checked
    out on another machine - which is the property an absolute path in a
    committed file cannot have.
    """
    host = host_repo() or ROOT.parent
    name = name or host.name
    for status in STATUSES:
        (ROOT / "tasks" / status).mkdir(parents=True, exist_ok=True)
    (ROOT / "artifacts").mkdir(exist_ok=True)
    project_file = ROOT / "project.md"
    if not project_file.exists():
        meta = {
            "name": name,
            "prefix": (prefix or derive_prefix(name)),
            "repo_path": "..",
            "created": utcnow(),
        }
        body = (
            f"\n## Context\n\nWhat `{name}` is, and what an agent needs to know before\n"
            "touching it. This file is inlined into every task prompt, so what is here is\n"
            "what the agent knows without spending a tool call.\n"
            "\n## Definition of done\n\n- Tests pass\n- Change is committed\n"
            "\n## Conventions\n\n- \n"
            "\n## Constraints\n\n- \n"
        )
        project_file.write_text(dump_frontmatter(meta, body), encoding="utf-8")
        _PROJECT_META.pop(str(ROOT), None)
        _PROJECT_NAMES.pop(str(ROOT), None)
    return ROOT


# --------------------------------------------------------------------------
# body sections: bookkeeping the wrapper and the agent both rewrite
# --------------------------------------------------------------------------

# The three sections that machines own, in the order they must appear. Anything
# a human wrote (Goal, Acceptance criteria, Context, Constraints) stays above
# them, so a task file still reads top-down as a brief rather than as a journal.
SECTION_ORDER = ["## Attempts", "## Checkpoint", "## Log"]


def split_section(body: str, heading: str) -> tuple[str, str, str] | None:
    """Split a body into (before, section content, after) around one heading."""
    match = re.search(r"^" + re.escape(heading) + r"[ \t]*$", body, re.M)
    if not match:
        return None
    rest = body[match.end():]
    following = re.search(r"^#{1,6}[ \t]+\S", rest, re.M)
    end = match.end() + (following.start() if following else len(rest))
    return body[:match.start()], body[match.end():end], body[end:]


def set_section(body: str, heading: str, content: str) -> str:
    """Replace one bookkeeping section, creating it in canonical order.

    Appending blindly is what produced files where `## Checkpoint` sat below a
    hundred log lines: the section a resuming run needs first was the one it had
    to scroll furthest to find.
    """
    block = heading + "\n\n" + content.strip("\n") + "\n\n"
    found = split_section(body, heading)
    if found:
        head, _, tail = found
        return head + block + tail.lstrip("\n")
    later = SECTION_ORDER[SECTION_ORDER.index(heading) + 1:] if heading in SECTION_ORDER else []
    for name in later:
        found = split_section(body, name)
        if found:
            head = found[0]
            return head + block + body[len(head):]
    return body.rstrip("\n") + "\n\n" + block


# `- 2026-08-26T07:24:26Z tick: ready -> in-progress`. Only used to migrate the
# log of a task written before the sidecar existed; new entries never round-trip
# through Markdown.
LOG_LINE_RE = re.compile(r"^-[ \t]+(\S+)[ \t]+([A-Za-z][\w.@-]*):[ \t]*(.*)$")


def parse_log_section(body: str) -> list[dict]:
    found = split_section(body, "## Log")
    if not found:
        return []
    entries = []
    for line in found[1].splitlines():
        match = LOG_LINE_RE.match(line.strip())
        if match:
            entries.append({"ts": match.group(1), "actor": match.group(2),
                            "message": match.group(3)})
    return entries


def render_log(entries: list[dict], sidecar: str, kept: int | None = None) -> str:
    """Render log entries as Markdown. `kept=None` renders the whole history."""
    shown = entries if not kept or len(entries) <= kept else entries[-kept:]
    lines = []
    if entries:
        if len(shown) < len(entries):
            lines.append(f"_{len(entries)} entries in `{sidecar}`; "
                         f"the {len(shown)} most recent are kept here._\n")
        else:
            lines.append(f"_Full history: `{sidecar}`._\n")
    for entry in shown:
        lines.append(f"- {entry.get('ts', '')} {entry.get('actor', 'agent')}: "
                     f"{entry.get('message', '')}")
    return "\n".join(lines)


LOCAL_POLICY_FILE = "LOCAL.md"


def local_policy(limit: int | None = None) -> str:
    """This copy's own rules, for inlining into a prompt.

    `AGENTS.md` is mirrored, so a rule written there is a rule for every board
    that will ever be made from this one. Some rules are not: a mode granted for
    a week, a repository this machine may not reach, a standing instruction that
    belongs to the copy that runs the loop. Written into AGENTS.md they diverge
    the two checkouts by construction and mirroring silently reverts them, which
    is how this board lost a grant once already. LOCAL.md is where they go
    instead - never mirrored, never pruned, and read out to every run.
    """
    path = ROOT / LOCAL_POLICY_FILE
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    limit = int(limit or load_config()["local_policy_chars"])
    if len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        text = text[:cut].rstrip() + f"\n\n(truncated - the rest is in {path})"
    return re.sub(r"^(#{1,5})(?= )", r"#\1", text, flags=re.M)


def project_brief(project_dir: Path, limit: int | None = None) -> str:
    """The head of project.md, for inlining into a prompt.

    The prompt used to give only the path. An agent that skips reading it works
    against conventions it was never shown, so the cheapest fix is to spend the
    tokens up front.
    """
    path = Path(project_dir) / "project.md"
    if not path.is_file():
        return ""
    _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    text = body.strip()
    limit = int(limit or load_config()["project_brief_chars"])
    if len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        text = text[:cut].rstrip() + f"\n\n(truncated - the rest is in {path})"
    # Demote the headings one level: inlined as-is, project.md's own `## Context`
    # would close the prompt's section instead of sitting inside it.
    return re.sub(r"^(#{1,5})(?= )", r"#\1", text, flags=re.M)


# --------------------------------------------------------------------------
# the project journal: the line the project took, in the order it took it
# --------------------------------------------------------------------------

# `project.md` says what the project is and never changes much. The journal says
# what has happened to it lately, and it exists because of one failure: a task
# written three weeks ago is worked against a repository that four tasks have
# changed since, by an agent that has no memory of any of them. It re-decides
# what was already decided, or contradicts it, and nothing on the board says so.
#
# The task log already holds all of that, in far too much detail and in the wrong
# place - it is per task, and the agent that needs it is on a different one. The
# journal is the same knowledge cut down to what carries forward: one paragraph
# per task that landed, newest first, inlined into the next agent's prompt.
#
# Everything here is about keeping it small. One entry per closing transition, a
# hard cap on its length, and only the head of the file is ever inlined. A record
# of decisions that grows without limit becomes documentation, and documentation
# is what nobody updates and everybody skips.

JOURNAL_FILE = "journal.md"

JOURNAL_HEADER = """# Journal - {name}

What this project decided, newest first: one paragraph per task that landed,
written by `bin/task done` and `bin/task review`. The detail stays in the task
and its log; this is only the part the next task needs to know. The head of this
file is inlined into every agent prompt for this project, so keep entries short -
if it reads like documentation it has already stopped being useful."""

# An entry heading. Dated on purpose: the id says which task, the date says
# whether the entry is describing the repository as it is now or as it was.
JOURNAL_ENTRY_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} ", re.M)

# Below this an entry is not a sentence. "done", "fixed it" and "ok" are the
# entries this refuses, and they are the ones that would have been written.
JOURNAL_MIN_CHARS = 40


def journal_path(project_dir: Path) -> Path:
    return Path(project_dir) / JOURNAL_FILE


def check_decision(text: str) -> str:
    """Validate one journal entry, or say why it is not one.

    Raised rather than trimmed. A silent truncation loses the half of the
    sentence that said *why*, which is the only half worth keeping, and a
    silently accepted one-word entry teaches the next agent that one word is
    what this asks for.
    """
    text = "\n".join(line.rstrip() for line in str(text or "").splitlines()).strip()
    cap = int(load_config()["journal_entry_chars"])
    if len(text) < JOURNAL_MIN_CHARS:
        raise ValueError(
            "a journal entry is a short paragraph, not a word: say what changed, "
            "why that way rather than the obvious alternative, and what it leaves "
            f"for later (at least {JOURNAL_MIN_CHARS} characters, this was {len(text)})")
    if len(text) > cap:
        raise ValueError(
            f"a journal entry is capped at {cap} characters and this is {len(text)}. "
            "The detail belongs in the task log, which is unbounded; the journal "
            "carries only what the next task needs to know")
    # A heading inside an entry would read as the start of the next entry.
    return re.sub(r"^#{1,6}(?= )", "####", text, flags=re.M)


def split_journal(text: str) -> tuple[str, list[str]]:
    """A journal file as (header, entries newest first)."""
    marks = list(JOURNAL_ENTRY_RE.finditer(text))
    if not marks:
        return text.strip(), []
    entries = []
    for n, mark in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        entries.append(text[mark.start():end].strip())
    return text[:marks[0].start()].strip(), entries


def journal_entries(project_dir: Path) -> list[str]:
    path = journal_path(project_dir)
    if not path.is_file():
        return []
    return split_journal(path.read_text(encoding="utf-8"))[1]


def append_journal(project_dir: Path, task_id: str, title: str,
                   status: str, text: str) -> Path:
    """Put one entry at the top of the project's journal.

    Newest first, because the head of the file is what gets read - by the prompt,
    which inlines only as much as fits, and by a person opening it. Appending at
    the bottom would put the entry that matters most where it is found last.
    """
    path = journal_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    header, entries = split_journal(existing)
    if not header:
        header = JOURNAL_HEADER.format(name=project_name(Path(project_dir)))
    entry = (f"## {utcnow()[:10]} {task_id} ({status}) - {title}".rstrip()
             + "\n\n" + text.strip())
    path.write_text("\n\n".join([header, entry] + entries) + "\n", encoding="utf-8")
    return path


def project_journal(project_dir: Path, limit: int | None = None) -> str:
    """The head of the journal, for inlining into a prompt.

    Cut at an entry boundary rather than a character count: half an entry is
    worse than none, because the half that survives is the claim and the half
    that is dropped is the reason for it.
    """
    entries = journal_entries(project_dir)
    if not entries:
        return ""
    limit = int(limit or load_config()["journal_brief_chars"])
    shown, used = [], 0
    for entry in entries:
        if shown and used + len(entry) > limit:
            break
        shown.append(entry)
        used += len(entry) + 2
    text = "\n\n".join(shown)
    dropped = len(entries) - len(shown)
    if dropped:
        text += (f"\n\n({dropped} older entr{'y' if dropped == 1 else 'ies'} in "
                 f"{journal_path(project_dir)})")
    # Demoted for the same reason project.md's headings are: inlined as-is, an
    # entry's `##` would close the prompt section it was meant to sit inside.
    return re.sub(r"^(#{1,5})(?= )", r"#\1", text, flags=re.M)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

class Task:
    """One task file. Metadata is read eagerly, the body only when asked for.

    Constructing a Task used to read the whole file, which meant every scan of
    the board paid for every task's log even when it only compared statuses and
    ids. The body is still one attribute access away - `task.body` reads the
    file on first use - so nothing that needs it has to know it was deferred.
    """

    def __init__(self, path: Path, meta: dict | None = None, body: str | None = None):
        self.path = Path(path)
        if meta is None:
            meta, body = read_frontmatter(self.path)
        self.meta = meta
        self._body = body

    @property
    def body(self) -> str:
        if self._body is None:
            _, self._body = parse_frontmatter(self.path.read_text(encoding="utf-8"))
        return self._body

    @body.setter
    def body(self, value: str) -> None:
        self._body = value

    # --- derived properties -------------------------------------------------

    @property
    def status(self) -> str:
        """The directory is authoritative; the frontmatter field is a mirror."""
        return self.path.parent.name

    @property
    def id(self) -> str:
        return str(self.meta.get("id", self.path.stem))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", self.path.stem))

    @property
    def project_dir(self) -> Path:
        return self.path.parent.parent.parent

    @property
    def project(self) -> str:
        return project_name(self.project_dir)

    @property
    def priority(self) -> str:
        return str(self.meta.get("priority", "P2"))

    @property
    def attempts(self) -> int:
        try:
            return int(self.meta.get("attempts", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def max_attempts(self) -> int:
        try:
            return int(self.meta.get("max_attempts", 0) or 0) or load_config()["default_max_attempts"]
        except (TypeError, ValueError):
            return load_config()["default_max_attempts"]

    @property
    def type(self) -> str:
        return str(self.meta.get("type") or "").strip().lower()

    @property
    def size(self) -> str:
        return str(self.meta.get("size") or "").strip().upper()

    @property
    def due(self) -> str:
        return str(self.meta.get("due") or "").strip()

    @property
    def depends_on(self) -> list[str]:
        value = self.meta.get("depends_on")
        if not value:
            return []
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return [str(value)]

    def has_checkpoint(self) -> bool:
        return bool(self.meta.get("checkpoint_at"))

    # --- mutation -----------------------------------------------------------

    def save(self) -> None:
        self.meta["status"] = self.status
        self.meta["updated"] = utcnow()
        self.path.write_text(dump_frontmatter(self.meta, self.body), encoding="utf-8")

    @property
    def log_path(self) -> Path:
        """The full log, beside the task file so the two move together."""
        return self.path.parent / f"{self.id}.log.jsonl"

    def log_entries(self) -> list[dict]:
        """Every entry ever written, oldest first."""
        path = self.log_path
        if not path.exists():
            # A task written before the sidecar existed: its history is still in
            # the body, and that is the only copy there is.
            return parse_log_section(self.body)
        entries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn line must not lose the ones around it
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def log(self, message: str, actor: str = "agent") -> None:
        path = self.log_path
        seed = [] if path.exists() else parse_log_section(self.body)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for entry in seed + [{"ts": utcnow(), "actor": actor, "message": message}]:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.body = set_section(
            self.body, "## Log",
            render_log(self.log_entries(), path.name, load_config()["task_log_kept"]))
        self.save()

    def note_attempt(self, outcome: str, detail: str = "") -> None:
        """Record one line about an attempt that ended, newest first.

        The log says what happened minute by minute; this says only what each
        attempt tried and what broke, which is the part the next attempt needs
        and the part that used to be buried.
        """
        detail = " ".join(str(detail).split())[:200]
        line = (f"- {self.attempts}/{self.max_attempts} {utcnow()} {outcome}"
                + (f": {detail}" if detail else ""))
        found = split_section(self.body, "## Attempts")
        previous = [l.rstrip() for l in found[1].splitlines() if l.strip().startswith("- ")] if found else []
        kept = int(load_config()["task_attempts_kept"])
        self.body = set_section(self.body, "## Attempts",
                                "\n".join(([line] + previous)[:kept]))
        self.save()

    def checkpoint_text(self) -> str:
        found = split_section(self.body, "## Checkpoint")
        return found[1].strip() if found else ""

    def set_checkpoint(self, text: str) -> None:
        self.body = set_section(self.body, "## Checkpoint", text)
        self.meta["checkpoint_at"] = utcnow()
        self.save()

    def record_decision(self, text: str, status: str | None = None) -> Path:
        """Leave this task's paragraph in the project journal.

        `journal_at` is what stops a second entry being demanded when a human
        moves a task on from `review`: the work was already written up once, and
        the same paragraph twice would push the older entries out of the head of
        the file that the next prompt reads.
        """
        path = append_journal(self.project_dir, self.id, self.title,
                              status or self.status, text)
        self.meta["journal_at"] = utcnow()
        self.save()
        return path

    def move(self, new_status: str, message: str | None = None,
             actor: str = "agent", force: bool = False) -> None:
        current = self.status
        if new_status not in STATUSES:
            raise ValueError(f"unknown status: {new_status}")
        if new_status == current:
            if message:
                self.log(message, actor)
            return
        if not force and new_status not in TRANSITIONS.get(current, set()):
            raise ValueError(f"illegal transition {current} -> {new_status}")
        target_dir = self.project_dir / "tasks" / new_status
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / self.path.name
        old_log = self.log_path
        os.replace(self.path, target)
        self.path = target
        # The sidecar is addressed by "beside the task file", so a move that
        # left it behind would silently start a second, empty history.
        if old_log.exists():
            os.replace(old_log, self.log_path)
        if new_status == "in-progress":
            self.meta["started"] = utcnow()
        if new_status in CLOSED or new_status == "failed":
            self.meta["closed"] = utcnow()
        self.save()
        self.log(f"{current} -> {new_status}" + (f" ({message})" if message else ""), actor)


def iter_task_paths(status: str | None = None, project: str | None = None):
    """The task files, without opening any of them.

    Separate from `iter_tasks` because deciding *which* file you want is often
    possible from its name alone, and a decision made from names costs a
    directory listing where the same decision made from Task objects costs a
    read and a parse of every task on the board.
    """
    for project_dir in iter_project_dirs():
        if project and project_name(project_dir) != project:
            continue
        statuses = [status] if status else STATUSES
        for state in statuses:
            directory = project_dir / "tasks" / state
            if not directory.is_dir():
                continue
            yield from sorted(directory.glob("*.md"))


def iter_tasks(status: str | None = None, project: str | None = None):
    for path in iter_task_paths(status, project):
        yield Task(path)


def board_index(tasks: list[Task] | None = None) -> tuple[list[Task], dict[str, Task]]:
    """One walk of the board, as both a list and an id lookup.

    Callers used to walk twice - once to build the id index a dependency check
    needs, once for the tasks they actually wanted - and a walk is the most
    expensive thing on this board. The list keeps duplicates that the dict
    cannot, which is what lets `doctor` still see two files claiming one id.
    """
    tasks = list(iter_tasks()) if tasks is None else tasks
    return tasks, {t.id: t for t in tasks}


def find_task(task_id: str) -> Task | None:
    """Resolve an id to a task, by filename first and by content second.

    `bin/task new` names a file `<id>-<slug>.md`, so the id is nearly always
    readable from the name; trusting it turns what was a full-board read into a
    handful of directory listings. The name is only a hint, though - a file
    renamed by hand keeps its id in the frontmatter - so anything the hint
    misses is still resolved the slow, correct way.
    """
    wanted = task_id.strip().lower()
    if not wanted:
        return None
    unnamed = []
    for path in iter_task_paths():
        stem = path.stem.lower()
        if stem == wanted or stem.startswith(wanted + "-"):
            task = Task(path)
            if task.id.lower() == wanted:
                return task
        else:
            unnamed.append(path)
    for path in unnamed:
        task = Task(path)
        if task.id.lower() == wanted:
            return task
    return None


# `<prefix>-<number>` at the head of a filename. Task files are named
# `<id>-<slug>.md` by `create_task`, and archived ones keep that name, so the id
# is readable without opening the file.
TASK_ID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+)(?:-|$)")


def id_from_filename(path: Path) -> str | None:
    """The task id a filename claims, or None if it does not claim one."""
    match = TASK_ID_RE.match(path.stem)
    return match.group(1) if match else None


_ARCHIVED_IDS: set[str] | None = None


def archived_ids(refresh: bool = False) -> set[str]:
    """Ids that have left the board for `archive/`.

    `iter_tasks()` deliberately does not walk the archive, so every caller that
    reasons about ids - allocating a new one, resolving a dependency - has to
    consult this as well, or an archived id looks like an id that never existed.

    Cached for the life of the process, like `load_config`: the archive only
    grows when `archive_closed()` runs, and a dependency check that walks it
    once per unresolved id turns a slow scan into a quadratic one.
    """
    global _ARCHIVED_IDS
    if _ARCHIVED_IDS is not None and not refresh:
        return _ARCHIVED_IDS
    found = set()
    if ARCHIVE.is_dir():
        for path in ARCHIVE.rglob("*.md"):
            claimed = id_from_filename(path)
            if claimed:
                found.add(claimed.upper())
    _ARCHIVED_IDS = found
    return found


def next_id(project_dir: Path) -> str:
    prefix = str(project_meta(project_dir).get("prefix") or
                 project_name(project_dir).split("/")[-1][:3].upper())
    highest = 0
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.I)
    # Archived ids count: reusing one would point old logs, commits and task
    # references at two different pieces of work.
    candidates = sorted(archived_ids())
    for path in iter_task_paths():
        # The name carries the id, so allocating the next one does not have to
        # read the board. Only a file someone renamed by hand has to be opened -
        # and it has to be, because guessing low here reuses a live id.
        claimed = id_from_filename(path)
        candidates.append(claimed if claimed else Task(path).id)
    for candidate in candidates:
        match = pattern.match(candidate)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1:03d}"


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "task"


# --------------------------------------------------------------------------
# lint: is this task fit to be worked?
#
# `blocking_reasons` answers "may the loop pick this task up now" - dependencies,
# time, attempts, and whether the work is reserved for a human at all. Lint
# answers a different one: "could an agent act on this in a fresh session, with
# nothing but the file in front of it". The two are deliberately separate,
# because a task can be perfectly schedulable and still say nothing about what
# done means - and one that is fit to be worked may still be nobody's to work.
# --------------------------------------------------------------------------

CRITERIA_HEADING_RE = re.compile(r"^#{1,6}[ \t]*acceptance criteria[ \t]*$", re.I | re.M)
NEXT_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S", re.M)
BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]*(?:\[[ xX]\][ \t]*)?(.*)$")

# Titles that name no outcome. They are all real: `task new` writes the first,
# and the rest are what a placeholder looks like when someone meant to come back
# to it and did not.
PLACEHOLDER_TITLES = {
    "task", "tasks", "todo", "to do", "tbd", "wip", "fix", "fixes", "misc",
    "notes", "note", "untitled", "new task", "temp", "test", "stuff", "cleanup",
}

MIN_TITLE_WORDS = 3
MIN_TITLE_CHARS = 12


def acceptance_criteria(body: str) -> list[str]:
    """The checklist items under `## Acceptance criteria`, as plain text.

    Empty items are dropped on purpose: the bare `- [ ]` that `task new` puts in
    the template is exactly the thing lint exists to catch, and counting it would
    make every freshly created task look specified.
    """
    match = CRITERIA_HEADING_RE.search(body)
    if not match:
        return []
    rest = body[match.end():]
    end = NEXT_HEADING_RE.search(rest)
    section = rest[:end.start()] if end else rest
    items = []
    for line in section.splitlines():
        bullet = BULLET_RE.match(line)
        if bullet and bullet.group(1).strip():
            items.append(bullet.group(1).strip())
    return items


def title_problem(title: str) -> str | None:
    """Why this title names no outcome, or None when it looks like one.

    This is a smell test, not grammar. It cannot tell a good title from a bad
    one, and it does not try - it catches the placeholder, the bare noun and the
    two-word label, which is what actually reaches the board.
    """
    text = " ".join(str(title or "").split())
    if not text:
        return "title is empty"
    if text.lower().strip(".:!?") in PLACEHOLDER_TITLES:
        return f"title {text!r} is a placeholder, not an outcome"
    if len(text.split()) < MIN_TITLE_WORDS or len(text) < MIN_TITLE_CHARS:
        return (f"title {text!r} is too short to name an outcome "
                f"(want at least {MIN_TITLE_WORDS} words and {MIN_TITLE_CHARS} characters)")
    return None


def lint_task(task: Task, index: dict[str, Task] | None = None) -> list[str]:
    """Everything that would stop an agent acting on this task. Empty is fit.

    Order matters a little: what the reader has to fix first comes first, so a
    task with several problems still reads as a to-do list rather than a dump.
    """
    problems = []

    problem = title_problem(task.title)
    if problem:
        problems.append(problem)

    if not acceptance_criteria(task.body):
        problems.append("no acceptance criterion: add at least one non-empty "
                        "`- [ ]` item under `## Acceptance criteria`")

    if not task.type:
        problems.append(f"type is not set (one of: {', '.join(TASK_TYPES)})")
    elif task.type not in TASK_TYPES:
        problems.append(f"type {task.type!r} is not one of: {', '.join(TASK_TYPES)}")

    # size and due are optional, but a value outside the vocabulary is worse than
    # a missing one: every view shows it and no filter matches it.
    if task.size and task.size not in TASK_SIZES:
        problems.append(f"size {task.size!r} is not one of: {', '.join(TASK_SIZES)}")
    if task.due and parse_ts(task.due) is None:
        problems.append(f"due {task.due!r} is not a date the board can read "
                        f"(use YYYY-MM-DD or an ISO timestamp)")

    repo = str(project_meta(task.project_dir).get("repo_path") or "").strip()
    if not repo:
        problems.append(f"project {task.project} has no repo_path in project.md; "
                        f"an agent would not know where to work")

    if task.depends_on:
        index = index if index is not None else {t.id: t for t in iter_tasks()}
        archived = None
        for dep in task.depends_on:
            if dep in index:
                continue
            if archived is None:
                archived = archived_ids()
            if dep.upper() not in archived:
                problems.append(f"depends_on {dep} does not resolve to any task")

    return problems


def worked_by_hand(task: Task) -> str:
    """Why the loop must leave this task to a human, or "" when it may work it.

    Both of these were prose before they were checks, and prose is re-read by an
    agent every hour at the price of an agent run. Worse, prose is what stood
    between the loop and the task that rewrites `bin/tick` while `bin/tick` is
    running: `project.md` said "the loop must not pick them up" and nothing
    enforced it.
    """
    if str(task.meta.get("owner", "agent")).strip().lower() == "human":
        return "owned by a human"
    manual = project_meta(task.project_dir).get("manual")
    if str(manual).strip().lower() in ("true", "yes", "1"):
        return f"{task.project} is worked by hand"
    return ""


def blocking_reasons(task: Task, index: dict[str, Task] | None = None) -> list[str]:
    """Why this task cannot be picked up right now. Empty list means eligible."""
    reasons = []
    index = index if index is not None else {t.id: t for t in iter_tasks()}
    archived = None
    by_hand = worked_by_hand(task)
    if by_hand:
        reasons.append(by_hand)
    for dep in task.depends_on:
        found = index.get(dep)
        if found is None:
            # A dependency that was archived is a dependency that was closed.
            if archived is None:
                archived = archived_ids()
            if dep.upper() not in archived:
                reasons.append(f"unknown dependency {dep}")
        elif found.status not in CLOSED:
            reasons.append(f"depends on {dep} ({found.status})")
    after = parse_ts(task.meta.get("ready_after"))
    if after and after > datetime.now(timezone.utc):
        reasons.append(f"scheduled for {after.isoformat()}")
    if task.attempts >= task.max_attempts:
        reasons.append(f"attempts exhausted ({task.attempts}/{task.max_attempts})")
    return reasons


def eligible_tasks() -> list[Task]:
    """Every task that could be worked right now, best first.

    Eligibility is about the task alone - status, dependencies, schedule,
    attempts. Whether anyone is *already* working the resource it needs is a
    liveness question, which belongs to the wrapper and its locks.
    """
    tasks, index = board_index()
    eligible = [t for t in tasks
                if t.status == "ready" and not blocking_reasons(t, index)]
    eligible.sort(key=lambda t: (
        PRIORITY_ORDER.get(t.priority, 9),
        str(t.meta.get("created", "")),
        t.id,
    ))
    return eligible


def pick_next() -> Task | None:
    tasks = eligible_tasks()
    return tasks[0] if tasks else None


def resource_key(task: Task) -> str:
    """What this task needs exclusively while it is worked.

    The unit is the working copy, not the project: two projects can point at one
    repository, and two agents editing and committing in the same checkout will
    ruin each other's work. A project with no `repo_path` has nothing to share,
    so its tasks are only exclusive with themselves.
    """
    repo = resolve_repo(project_meta(task.project_dir).get("repo_path") or "")
    if repo is not None:
        # Resolved, so that two projects naming one checkout by different
        # spellings - a symlink, a relative path, a `~` - collide on one lock
        # rather than each taking a lock of its own and both running at once.
        return "repo:" + str(repo.resolve())
    return "task:" + task.id


def lock_path(key: str) -> Path:
    """A stable, readable filename for a resource key."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-")[-60:]
    return RUNTIME / "locks" / f"{readable}-{digest}.lock"


def create_task(project: str, title: str, *, body: str = "", priority: str = "P2",
                status: str = "backlog", depends_on: list[str] | None = None,
                ready_after: str | None = None, tags: list[str] | None = None,
                parent: str | None = None, max_attempts: int | None = None,
                owner: str = "agent", task_type: str | None = None,
                size: str | None = None, due: str | None = None) -> Task:
    project_dir = project_dir_for(project) or ensure_project(project)
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    task_id = next_id(project_dir)
    depth = 0
    if parent:
        parent_task = find_task(parent)
        if parent_task:
            try:
                depth = int(parent_task.meta.get("depth", 0) or 0) + 1
            except (TypeError, ValueError):
                depth = 1
    config = load_config()
    meta = {
        "id": task_id,
        "title": title,
        "project": project_name(project_dir),
        "status": status,
        # Written even when empty so the field is visible in the file: a schema
        # you have to know about to fill in is a schema nobody fills in.
        "type": (task_type or "").strip().lower(),
        "size": (size or "").strip().upper(),
        "priority": priority,
        "owner": owner,
        "created": utcnow(),
        "updated": utcnow(),
        "depends_on": depends_on or [],
        "ready_after": ready_after or "",
        "due": (due or "").strip(),
        "parent": parent or "",
        "depth": depth,
        "attempts": 0,
        "max_attempts": max_attempts or config["default_max_attempts"],
        "tags": tags or [],
    }
    template = body.strip() or (
        "## Goal\n\n"
        f"{title}\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] \n\n"
        "## Context\n\n"
        "\n\n"
        "## Constraints\n\n"
        "- \n"
    )
    document = template.rstrip("\n") + "\n\n## Log\n\n"
    directory = project_dir / "tasks" / status
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task_id}-{slugify(title)}.md"
    path.write_text(dump_frontmatter(meta, "\n" + document), encoding="utf-8")
    task = Task(path)
    task.log(f"created in {status}", owner)
    return task


def retry_delay(attempts: int, config: dict | None = None) -> int:
    """Exponential backoff before a requeued attempt becomes eligible again.

    Without it a systemic failure - no network, a broken checkout - burns every
    attempt within a few ticks and parks a healthy task in `needs-human`.
    """
    config = config or load_config()
    base = int(config["retry_backoff_sec"])
    ceiling = int(config["retry_backoff_max_sec"])
    return min(base * (2 ** max(0, attempts - 1)), ceiling)


def schedule_retry(task: "Task", attempts: int | None = None,
                   config: dict | None = None) -> int:
    """Hold a requeued task back for the backoff window. Returns the delay."""
    delay = retry_delay(task.attempts if attempts is None else attempts, config)
    when = datetime.now(timezone.utc) + timedelta(seconds=delay)
    task.meta["ready_after"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    task.save()
    return delay


def archive_closed(days: int | None = None) -> int:
    """Move long-closed tasks into archive/. Mechanical, no agent involved."""
    config = load_config()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days or config["archive_after_days"])
    moved = 0
    for status in sorted(CLOSED):
        for task in iter_tasks(status=status):
            closed = parse_ts(task.meta.get("closed") or task.meta.get("updated"))
            if closed and closed < cutoff:
                bucket = ARCHIVE / closed.strftime("%Y-%m") / task.project.replace("/", "-")
                bucket.mkdir(parents=True, exist_ok=True)
                sidecar = task.log_path
                os.replace(task.path, bucket / task.path.name)
                if sidecar.exists():
                    os.replace(sidecar, bucket / sidecar.name)
                moved += 1
    return moved


# --------------------------------------------------------------------------
# search: a derived index, never the source of truth
#
# The board is files, and it stays files. This index exists so that an agent
# about to create a task can ask "have we already done this?" and get an answer
# in one command instead of reading the board - grooming used to catch
# duplicates by eye, which means it caught them late or not at all.
#
# Everything here is disposable. `.runtime/index.db` is gitignored, rebuilt from
# the files by `bin/task index --rebuild`, and deleting it costs one rescan. If
# the index and the files ever disagree, the files are right.
# --------------------------------------------------------------------------

INDEX_DB = RUNTIME / "index.db"

# Bumped whenever the schema below changes shape. A database written by an older
# board is discarded rather than migrated: it is derived data, and rebuilding it
# is cheaper than being careful with it.
INDEX_SCHEMA = 1

INDEX_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    mtime    REAL    NOT NULL,
    size     INTEGER NOT NULL,
    archived INTEGER NOT NULL,
    doc      INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS tasks USING fts5(
    id, title, tags, project, body,
    path UNINDEXED, status UNINDEXED, archived UNINDEXED,
    tokenize = "unicode61"
);
"""

# id, title, tags, project, body - then the unindexed columns, which contribute
# nothing. A word in the title says far more about a match than the same word
# buried in a log line, and the log is most of the body.
INDEX_WEIGHTS = (8.0, 12.0, 4.0, 1.0, 1.0, 0.0, 0.0, 0.0)


def index_sources():
    """Every file search should be able to find, as `(path, archived)`.

    The archive is included on purpose: "did we already do this?" is asked most
    often about work that is finished, and finished work is exactly what leaves
    the board.
    """
    for path in iter_task_paths():
        yield path, False
    if ARCHIVE.is_dir():
        for path in sorted(ARCHIVE.rglob("*.md")):
            yield path, True


def index_document(path: Path, archived: bool) -> dict:
    """The searchable form of one task file. Reads it whole - search wants the body."""
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]
    if archived:
        # An archived task has no status directory left to stand in for its
        # status, so here - and only here - the frontmatter is what there is.
        status = str(meta.get("status") or "done")
        project = str(meta.get("project") or path.parent.name)
    else:
        status = path.parent.name
        project = project_name(path.parent.parent.parent)
    return {
        "id": str(meta.get("id") or id_from_filename(path) or path.stem),
        "title": str(meta.get("title") or path.stem),
        "tags": " ".join(str(t) for t in tags if t),
        "project": project,
        "body": body,
        "path": str(path.relative_to(ROOT)),
        "status": status,
        "archived": 1 if archived else 0,
    }


def _index_version(conn) -> int | None:
    """The schema version the open database states, or None if it states none."""
    row = conn.execute("SELECT name FROM sqlite_master"
                       " WHERE type = 'table' AND name = 'schema_meta'").fetchone()
    if row is None:
        # A database that exists but has no tables yet is a fresh one, not a
        # stale one: connect() creates the file before the DDL runs.
        return None if conn.execute(
            "SELECT count(*) AS n FROM sqlite_master").fetchone()["n"] else INDEX_SCHEMA
    stated = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    try:
        return int(stated["value"]) if stated else None
    except (TypeError, ValueError):
        return None


def _connect_index(create: bool):
    """Open the index, or None when there is nothing usable to open.

    Never raises: a board with no sqlite3, no FTS5 or a corrupt database has to
    keep working, just without the shortcut.
    """
    try:
        import sqlite3
    except ImportError:
        return None
    if not create and not INDEX_DB.exists():
        return None
    try:
        INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(INDEX_DB, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            version = _index_version(conn)
        except sqlite3.DatabaseError:
            # Not a database at all, or a damaged one. Derived data heals by
            # being thrown away; leaving it would make every later search slow
            # for as long as nobody noticed.
            conn.close()
            INDEX_DB.unlink(missing_ok=True)
            if not create:
                return None
            conn = sqlite3.connect(INDEX_DB, timeout=10.0)
            conn.row_factory = sqlite3.Row
            version = _index_version(conn)
        if version != INDEX_SCHEMA:
            conn.close()
            # Written by a different shape of this code. Derived data is not
            # worth migrating - throw it away and let the rescan below rebuild.
            INDEX_DB.unlink(missing_ok=True)
            if not create:
                return None
            conn = sqlite3.connect(INDEX_DB, timeout=10.0)
            conn.row_factory = sqlite3.Row
        conn.executescript(INDEX_DDL)
        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                     (str(INDEX_SCHEMA),))
        conn.commit()
        return conn
    except sqlite3.Error:
        # Includes "no such module: fts5" on a Python built without it.
        return None


def refresh_index(rebuild: bool = False) -> dict:
    """Bring the index in line with the files. Returns what it had to change.

    Freshness is decided by mtime and size, per file. That is a heuristic - two
    writes inside one mtime tick that leave the size unchanged would be missed -
    which is the other reason `--rebuild` exists and why nothing may treat this
    index as authoritative.
    """
    if rebuild:
        INDEX_DB.unlink(missing_ok=True)
    conn = _connect_index(create=True)
    if conn is None:
        return {"available": False, "added": 0, "updated": 0, "removed": 0, "total": 0}
    counts = {"available": True, "added": 0, "updated": 0, "removed": 0, "total": 0}
    try:
        import sqlite3
        known = {row["path"]: row for row in
                 conn.execute("SELECT path, mtime, size, archived, doc FROM files")}
        seen = set()
        with conn:
            for path, archived in index_sources():
                rel = str(path.relative_to(ROOT))
                try:
                    stat = path.stat()
                except OSError:
                    continue
                seen.add(rel)
                prior = known.get(rel)
                if (prior and prior["mtime"] == stat.st_mtime
                        and prior["size"] == stat.st_size
                        and prior["archived"] == int(archived)):
                    continue
                doc = index_document(path, archived)
                if prior:
                    conn.execute("DELETE FROM tasks WHERE rowid = ?", (prior["doc"],))
                    counts["updated"] += 1
                else:
                    counts["added"] += 1
                cursor = conn.execute(
                    "INSERT INTO tasks(id, title, tags, project, body, path, status, archived)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc["id"], doc["title"], doc["tags"], doc["project"], doc["body"],
                     doc["path"], doc["status"], doc["archived"]))
                conn.execute(
                    "INSERT OR REPLACE INTO files(path, mtime, size, archived, doc)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (rel, stat.st_mtime, stat.st_size, int(archived), cursor.lastrowid))
            for rel, prior in known.items():
                if rel in seen:
                    continue
                # The file is gone. So is the row: an index that remembers
                # deleted tasks answers "have we done this?" with work that no
                # longer exists anywhere.
                conn.execute("DELETE FROM tasks WHERE rowid = ?", (prior["doc"],))
                conn.execute("DELETE FROM files WHERE path = ?", (rel,))
                counts["removed"] += 1
        counts["total"] = len(seen)
    except sqlite3.Error as exc:
        counts["available"] = False
        counts["error"] = str(exc)
    finally:
        conn.close()
    return counts


def fts_query(query: str) -> str:
    """A user's words as an FTS5 MATCH expression: every token must appear.

    Punctuation splits words rather than binding them: someone checking whether
    `retry-with-backoff` has been done before means the three words, not a
    phrase that has to appear contiguously, and slugs and ids are what people
    paste in when looking for prior art. Quoting each token separately is also
    what keeps FTS5 from reading `-`, `:` or `(` as operators and refusing the
    query. A trailing `*` survives, because "anything starting with this" is
    worth having and costs nothing. Exact phrases are still available through
    `--raw`, where the whole FTS5 grammar is.
    """
    parts = []
    for token in re.split(r"[^\w*]+", query.strip(), flags=re.UNICODE):
        prefix = token.endswith("*")
        core = (token[:-1] if prefix else token).replace('"', "")
        if not core:
            continue
        parts.append('"' + core + '"' + ("*" if prefix else ""))
    return " ".join(parts)


def _search_index(query: str, limit: int, include_archived: bool,
                  project: str | None, status: str | None,
                  raw: bool = False) -> list[dict] | None:
    conn = _connect_index(create=False)
    if conn is None:
        return None
    try:
        import sqlite3
        where = ["tasks MATCH ?"]
        params: list = [query if raw else fts_query(query)]
        if not include_archived:
            # An unindexed FTS5 column keeps the type it was given, and this one
            # was given an integer; comparing it to '0' silently matches nothing.
            where.append("CAST(archived AS INTEGER) = 0")
        if project:
            where.append("project = ?")
            params.append(project)
        if status:
            where.append("status = ?")
            params.append(status)
        sql = ("SELECT id, title, project, status, archived, path, tags,"
               " snippet(tasks, 4, '[', ']', ' … ', 12) AS snippet,"
               f" bm25(tasks, {', '.join(str(w) for w in INDEX_WEIGHTS)}) AS score"
               " FROM tasks WHERE " + " AND ".join(where) +
               " ORDER BY score LIMIT ?")
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # `--raw` was asked for and the query is not valid FTS5. Answering
            # with the words is more useful than answering with a stack trace.
            params[0] = fts_query(query)
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _search_files(query: str, limit: int, include_archived: bool,
                  project: str | None, status: str | None) -> list[dict]:
    """Search by reading the files. Slow, always correct, always available."""
    terms = [t.lower() for t in re.split(r"[^\w*]+", query.strip(), flags=re.UNICODE)
             if t.strip("*")]
    results = []
    for path, archived in index_sources():
        if archived and not include_archived:
            continue
        doc = index_document(path, archived)
        if project and doc["project"] != project:
            continue
        if status and doc["status"] != status:
            continue
        haystack = " ".join((doc["id"], doc["title"], doc["tags"],
                             doc["project"], doc["body"])).lower()
        if not all(term.rstrip("*") in haystack for term in terms):
            continue
        doc.pop("body")
        doc["snippet"] = ""
        doc["score"] = 0.0
        results.append(doc)
    results.sort(key=lambda d: (d["archived"], d["id"]))
    return results[:limit]


def search_tasks(query: str, limit: int = 20, include_archived: bool = True,
                 project: str | None = None, status: str | None = None,
                 use_index: bool = True, raw: bool = False) -> tuple[list[dict], bool]:
    """Find tasks matching `query`. Returns `(results, used_index)`.

    The index is an optimisation and is treated like one: it is refreshed first
    so it cannot answer from a stale board, and anything that goes wrong with it
    - missing FTS5, a locked or corrupt database - falls through to reading the
    files, which is the answer the index was only ever approximating.
    """
    if use_index:
        refresh_index()
        rows = _search_index(query, limit, include_archived, project, status, raw)
        if rows is not None:
            return rows, True
    return _search_files(query, limit, include_archived, project, status), False


# --------------------------------------------------------------------------
# notifications and git
# --------------------------------------------------------------------------

def notify(title: str, message: str, level: str = "info") -> None:
    config = load_config()
    if config.get("notify_file", True):
        NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = "" if NOTIFY_FILE.exists() else "# Notifications\n\nNewest entries are appended at the bottom.\n\n"
        with NOTIFY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{header}- **{utcnow()}** `{level}` **{title}** — {message}\n")
    # `notify_macos` is the old name for the same switch, from when a banner was
    # the only kind this board could raise. A config that still sets it keeps
    # working; what it now controls is whichever notifier the platform has.
    desktop = config.get("notify_desktop", config.get("notify_macos", True))
    if desktop:
        portable.notify_desktop("Project Tasks", title, message)


def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=check)


def git_commit(message: str) -> bool:
    """Commit the board. Serialised: concurrent ticks would collide on index.lock."""
    if not load_config().get("git_autocommit", True):
        return False
    # Embedded, the board has no `.git` of its own - the repository it lives in
    # does, and that is where its commits belong.
    if git_toplevel() is None:
        return False
    RUNTIME.mkdir(parents=True, exist_ok=True)
    handle = portable.open_lock_file(RUNTIME / "git.lock")
    try:
        deadline = time.monotonic() + 60
        while not portable.lock_exclusive(handle):
            if time.monotonic() > deadline:
                print("warning: gave up waiting for the board's git lock", file=sys.stderr)
                return False
            time.sleep(0.2)
        # Every git call runs with `-C ROOT`, and a pathspec is relative to
        # that, so `.` means "the board and nothing else" without anyone having
        # to work out where the board sits. Standalone, ROOT is the repository
        # root and `.` is the whole thing - exactly the `add -A` this replaced.
        # Embedded, ROOT is `<repo>/.tasks` and `.` stops at its edge, which is
        # the whole point: the loop commits the board on every tick, and a
        # board that swept up the working copy around it would sign the human's
        # half-finished code into a commit about a task.
        git("add", "--", ".")
        status = git("status", "--porcelain", "--", ".")
        if not status.stdout.strip():
            return False
        result = git("commit", "-m", message, "--no-verify", "--", ".")
        return result.returncode == 0
    finally:
        portable.unlock(handle)
        handle.close()
