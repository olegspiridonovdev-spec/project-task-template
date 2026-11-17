# Project Tasks

A local, file-backed task board that an agent works through on its own. A
scheduled job fires on an interval, picks an eligible task, runs an agent against
it, and puts the task into a terminal state before going straight on to the next
task it can work — so the board does not sit idle waiting for the next firing.
One task at a time by default; raise `max_concurrent_tasks` and it works on
several projects at once, never two runs in one working copy.

One Markdown file per task, and the task's status is the directory it lives in.
Which buys git history as a free audit log and undo, direct editability in any
editor, and an agent that reads and writes tasks with the same tools it uses for
everything else.

## Install it into your repository

This is the shape most people want. The board becomes `.tasks/` inside the
repository it works on, so an agent handed that one directory sees the code and
the tasks about the code together — the same clone, the same `git log`, the same
review — and there is no second place to go and no `repo_path` to configure.

```sh
sh /path/to/project-tasks/install.sh --into ~/code/myapp
```

```powershell
C:\src\project-tasks\install.ps1 -Into C:\code\myapp
```

That is the whole thing when you have a checkout: the installer uses the board it
is sitting in and touches the network for nothing. Without one, give it the
repository and it clones with the credentials already on the machine — an ssh key
or a credential helper, which is what a private repository needs and what an
unauthenticated download cannot offer:

```sh
sh install.sh --from https://github.com/olegspiridonovdev-spec/project-task-template.git \
              --into ~/code/myapp
```

With no `--from` at all, the address comes from the source checkout's own
`origin`, so it is whatever the repository says it is rather than a second copy
written down in the installer to go stale the day it moves.

What that leaves behind:

```
~/code/myapp/
├── src/  package.json  …                your code, untouched
├── AGENTS.md                            a pointer, appended between markers
├── .claude/settings.json                the hard limits, merged in
└── .tasks/
    ├── bin/  lib/  prompts/  scheduler/ the framework
    ├── config.json  LOCAL.md            this copy's settings and rules
    ├── project.md  journal.md           what the project is, and what it decided
    ├── tasks/<status>/*.md              the board — committed with the code
    └── .runtime/                        locks, leases, logs — gitignored
```

Then:

```sh
.tasks/bin/task new --title "Fix the flaky login test" --type bug --priority P1
.tasks/bin/task lint             # says what is still missing before it can be ready
$EDITOR .tasks/tasks/backlog/MYA-001-*.md    # write the acceptance criteria
.tasks/bin/task ready MYA-001
.tasks/bin/loopctl run           # one tick in the foreground, and watch it work
```

Two steps rather than one, on purpose. `--ready` puts a task in front of the loop,
and a task with no acceptance criteria makes the agent guess what done means — so
`--ready` refuses one that `bin/task lint` objects to. See
[Making `ready` mean something](#making-ready-mean-something).

**Upgrading** is the same command again. The framework is replaced; `config.json`,
`LOCAL.md`, every task, the journal and your own `AGENTS.md` are not. `--check`
says what would change and changes nothing.

```sh
sh /path/to/project-tasks/install.sh --into ~/code/myapp --check
```

## Or run one board across many repositories

The other shape, and the older one: the board is its own repository, with
`projects/<name>/` inside it and each project naming a `repo_path` somewhere else
on the machine. One loop, many codebases, work across all of them — and a board
whose history is separate from any product's.

```sh
git clone <this repo> project-tasks && cd project-tasks
git add -A && git commit -m "init"  # the loop commits after every tick, so it needs a repo
bin/loopctl doctor                  # checks python3, the agent CLI, git and the board

bin/task project-new myapp --repo ~/code/myapp --prefix MY
bin/task new --project myapp --title "Fix the flaky login test" --type bug
```

The board works out which shape it is in by asking git whether it is inside a
repository that is not itself, so nothing has to be configured and nothing can be
configured wrongly. Everything in this README applies to both unless it says
otherwise.

## Requirements, and what runs where

This is an internal repository. `CONTRIBUTING.md` says where a change belongs and
how one reaches a running board; `SECURITY.md` says what the guardrails actually
hold and where each of them stops.


`python3` 3.10 or newer — the standard library only, nothing to install — `git`,
and an agent CLI on your `PATH`: [Claude Code](https://claude.com/claude-code) by default, or
any other you give a profile to (see *Working with another agent*).

| | macOS | Linux | Windows |
| --- | --- | --- | --- |
| the board — `bin/task`, `board`, `guard`, `install`, the tests | yes | yes | yes |
| one tick by hand — `bin/loopctl run` | yes | yes | yes |
| the unattended loop — `bin/loopctl install` | launchd | systemd `--user` timer | — |

Every row is checked by CI on a real machine of each kind, including installing a
board on Windows with `install.ps1` and running the installed board's own suite
where it landed. Python 3.11 and 3.13 are both tested.

Windows has no scheduler this board can install into, and `bin/loopctl` says so
rather than guessing at a Scheduled Task nobody here can test. Everything else
works: drive the board by hand, or run the loop from WSL against the same files.
Because a shebang means nothing to `cmd.exe`, every entry point has a `.cmd`
beside it, and `PATHEXT` means `.tasks\bin\task list` works as written.

Everything OS-specific lives in `lib/portable.py` — locking, process control,
reading two pipes at once, notifications. Nothing else in the board knows what
platform it is on.

## Starting the loop

```sh
bin/loopctl install               # fires bin/tick every tick_interval_sec (default 600)
bin/loopctl status
```

`bin/loopctl uninstall` removes it again. Nothing in a checkout is
machine-specific: the label, the paths and the `PATH` are derived at install
time, and the label carries the board's own identity, so two checkouts on one
machine — one running the loop, one to develop in — install as two independent
jobs. Everything the loop does is committed, so `git log` is a complete record
and `git revert` undoes any of it.

### Read this before you install the loop

**The default configuration gives the agent full autonomy.** It runs with
`--permission-mode bypassPermissions`, which means it will commit, push, deploy and
install dependencies without asking. That is a deliberate choice
by the person who built this, not an oversight — but it should be *your* choice too.

To require approval instead, edit the agent profile in `config.json`:

```json
"agents": { "claude": { "command": ["claude", "--permission-mode", "acceptEdits",
                                    "--output-format", "stream-json", "--verbose"], ... } }
```

Note that a headless loop cannot answer permission prompts, so anything needing one
will stall and be requeued rather than silently proceeding. A gentler middle ground
is to keep full autonomy but tell the agent, in `AGENTS.md`, which classes of action
must go to `needs-human` — that file is where you describe how *your* agent should
work, and it is meant to be edited.

Whatever you choose, the hard limits in `.claude/deny.json` stay enforced either way.


## Working with another agent

The board is not tied to one model or one CLI. Everything an agent needs to do is
done through `bin/task`, and the rules it works under are in `AGENTS.md` — the
filename several harnesses now look for. `CLAUDE.md` imports it with Claude
Code's `@`-syntax, so Claude Code picks up the same file without a second copy to
keep in step — and without a symlink, which git on Windows would check out as an
ordinary file containing the text `AGENTS.md`.

Two things are worth knowing before pointing another agent at this directory.

**The hard limits are enforced per harness.** `.claude/deny.json` holds the rules and
`lib/guardlib.py` checks them, once, for everybody. What differs per agent is only the
adapter that puts the check in front of a tool call:

| harness   | how the limits are enforced                              | status                       |
|-----------|----------------------------------------------------------|------------------------------|
| Claude Code | `.claude/hooks/guard.py` as a `PreToolUse` hook         | enforced — the call is refused |
| opencode  | `.opencode/plugin/guard.js` on `tool.execute.before`      | enforced — the call is refused |
| anything else | nothing runs before the tool call                     | `AGENTS.md` only — advisory  |

Both adapters are a dozen lines that shell out to `bin/guard`, and both live in the
board, so a copy of the board carries its own enforcement rather than depending on how
the machine happens to be set up. A third harness needs the same shape and nothing
more — its own pre-execution hook, calling:

```sh
bin/guard check "some command"     # exit 1 and a reason when it is denied
bin/guard rules                    # what is enforced, and why
```

Two things it has to get right. Refuse the call when the exit code is non-zero, and
pass the harness's own tool name through `--tool`: most rules are scoped to the shell
tool by name, and `guardlib.canonical_tool` folds the names other agents use onto the
ones the rules are written in. A tool name nothing recognises is left alone, so only
the `*` rules apply to it — which is the failure worth knowing about, because a board
missing a mapping still *looks* guarded.

A harness with no hook at all cannot be stopped by anything here. `AGENTS.md` still
binds it, but the difference between "cannot" and "must not" is real, and it is the
main thing to weigh when choosing what to run unattended.

**Which agent the loop runs is a profile in `config.json`**, not something baked
into the wrapper:

```json
"agent": "claude",
"agents": {
  "claude":   { "command": ["claude", "--permission-mode", "bypassPermissions",
                            "--output-format", "stream-json", "--verbose"],
                "prompt": "flag:-p", "workdir_flag": "--add-dir", "stream": "claude-json" },
  "opencode": { "command": ["opencode", "run"],
                "prompt": "arg", "workdir_flag": null, "stream": "text" }
}
```

`prompt` says how the prompt reaches the agent — `arg` appends it, `flag:-p` puts it
behind a flag, `stdin` writes it to the process. `workdir_flag`, when set, is repeated
for the project's repository. `stream` says what stdout is: `claude-json` is parsed for
cost and usage limits, `text` is read only as a heartbeat.

That heartbeat is the whole contract. Any line the agent prints is evidence it is
alive, which is why an agent that prints prose works as well as one that prints
events — it just cannot report what it spent. `bin/loopctl doctor` checks the binary
of the profile you selected, and a profile name that does not exist is an error at
startup rather than a confusing failure later.

An older `config.json` that still has `claude_bin` and `claude_args` keeps working
unchanged; those keys describe the same thing and take precedence for the `claude`
profile.

Both profiles have driven a task from `ready` to `done`. Two details are worth knowing
if you add a third. Progress is any output on either stream, because agents differ in
where they narrate — Claude Code writes events to stdout, opencode writes to stderr,
and a heartbeat that watches only one of them reports a working agent as a stalled
one. And an agent that sandboxes directories has to be granted the project repository
as well as the board: that is what `opencode.json` does here, the same way
`.claude/settings.json` registers the guard hook — one small file per harness, in the
board, rather than a special case in the wrapper. `.opencode/plugin/guard.js` is the
same idea for the limits, and it is the only JavaScript in the board: adapting one
more harness is worth a file in a language nothing else here uses, writing the deny
list out a second time would not be.

## Keeping two copies in step

A board that runs the loop and a board you develop in are easier to keep apart than
to keep identical. `bin/mirror` does the copying:

```sh
bin/mirror ~/path/to/other-board --check   # what has drifted
bin/mirror ~/path/to/other-board           # bring it in line
```

It copies every tracked file except what belongs to each copy: the board's own work
(`projects/`, `inbox/`, `archive/`, `NOTIFY.md`, `STOP`) and its settings
(`config.json` — one board may run two tasks at once while the other runs one). It
preserves symlinks instead of dereferencing them into a second copy of what they
point at.
It refuses to write into a board whose loop is running, because replacing
`lib/tasklib.py` under a working agent takes away its ability to close its own task.
It refuses to overwrite uncommitted work in the destination, and it refuses to copy
anything not yet committed here — a mirrored file has to be a version this board can
point at afterwards. It does not commit for you.

Most importantly, it refuses to revert. Mirroring is one-directional, so a change
made in the other board and never brought back would simply disappear under a line
saying `synced`. Before overwriting anything it asks whether the destination's copy
is a version this board has ever held: a copy that is merely behind is safe to
update, a copy holding something else stops the sync. `--force` overrides that, and
almost never should.

## Why files and not a database

The filesystem is the source of truth: one Markdown file per task, and the
task's status is the directory it lives in. That buys three things a database
would take away — git history as a free audit log and undo, direct editability
in any editor, and an agent that reads and writes tasks with the same tools it
uses for everything else.

There is a SQLite index, and it changes none of that: `.runtime/index.db` is a
derived read model, gitignored, refreshed per file by mtime, and rebuilt from
the task files by `bin/task index --rebuild`. Deleting it is always safe. If it
ever disagrees with the files, the files are right — `bin/task search --no-index`
asks the files directly, and that is the answer the index approximates.

## The two shapes, and the one thing that differs

The board asks git whether its own directory is inside a repository that is not
itself. Inside one, it is **embedded**: `.tasks/` in somebody else's checkout,
one project — the repository around it — and the board root is that project's
directory. Not inside one, it is **standalone**: its own repository, with
`projects/<name>/` and a `repo_path` per project. Nothing is configured, so
nothing can be configured wrongly, and a board that is moved changes shape by
being moved.

Almost everything is the same either way. What is not is where the board's own
commits go.

The wrapper commits after every tick, which is the board's undo. Standalone, that
is its own repository, and staging everything is right — the whole directory is
the wrapper's to manage. Embedded, that same commit would land in the product's
history, and `git add -A` would sign the human's half-finished work into a commit
about somebody's task and bury it there.

The fix is one word rather than a mode. Every git call already runs with
`-C ROOT`, and a pathspec is relative to that, so the commit is scoped to `.`:

```python
git("add", "--", ".")
git("commit", "-m", message, "--no-verify", "--", ".")
```

Standalone, `ROOT` is the repository root and `.` is the whole thing — exactly
the `add -A` it replaced. Embedded, `ROOT` is `.tasks/` and `.` stops at its
edge. Neither case has to know which one it is in. A staged file outside the
board stays staged, an unstaged edit stays unstaged, and there is a test that
does both while a tick commits.

The other half is `repo_path`. An embedded project's is written as `..`, and a
relative one is resolved against the *board* rather than against whatever
directory a command was run from. That is what lets `project.md` be committed and
still be right after the repository is cloned to another machine, renamed, or
checked out by somebody else — which a path recorded as
`/Users/someone/code/myapp` is not.

## Search

```bash
bin/task search "webhook retry backoff"   # titles, bodies, tags — archive included
bin/task search "webho*" --status ready
bin/task search --raw 'title:webhook OR title:delivery'
```

It exists for one question: *have we already done this?* An agent about to
create a task could not ask it, so duplicates were caught by eye during
grooming — late, and never at all when the original had been archived. Both
`prompts/tick.md` and `prompts/groom.md` now tell the agent to search first.

Punctuation splits words rather than binding them, so an id or a slug pasted
straight in (`DM-002`, `retry-with-backoff`) is a query and not an FTS5 syntax
error. `--raw` hands the query to FTS5 as written when the full grammar is
wanted.

## Layout

```
project-tasks/
├── AGENTS.md              binding rules for the agent (CLAUDE.md imports it)
├── config.json            timeouts, limits, notification and git settings
├── LOCAL.md               rules belonging to this copy alone; never mirrored
├── install.sh             bootstrap: find a board, hand it to bin/install
├── install.ps1            the same, for Windows
├── bin/
│   ├── task               board CLI — every state change goes through it
│   ├── tick               one loop iteration: lock, watchdog, agent, settle
│   ├── board              render the board as a local web page
│   ├── guard              check an action against the hard limits, from any harness
│   ├── install            vendor a board into a repository, or upgrade one
│   ├── mirror             copy the framework onto another checkout of the board
│   ├── loopctl            install / pause / inspect the loop
│   └── *.cmd              the same entry points under a name cmd.exe will start
├── scheduler/
│   ├── launchd.plist.template     rendered per machine at install time (macOS)
│   ├── systemd.service.template   the same, for Linux
│   └── systemd.timer.template
├── opencode.json          permissions for the opencode agent profile
├── lib/
│   ├── tasklib.py         task parsing, transitions, selection, notifications
│   ├── guardlib.py        the hard limits, in a form any harness can call
│   └── portable.py        every OS difference: locks, processes, pipes, banners
├── tests/
│   ├── test_board.py      invariant tests: python3 -m unittest discover tests
│   └── bench_scan.py      what a board scan costs, on a synthetic 500-task board
├── prompts/
│   ├── tick.md            worker prompt
│   └── groom.md           hourly grooming prompt
├── .claude/
│   ├── settings.json      registers the PreToolUse guard
│   ├── deny.json          hard guardrails, editable without touching code
│   └── hooks/guard.py     Claude Code's adapter to bin/guard
├── .opencode/
│   └── plugin/guard.js    opencode's adapter to bin/guard
├── projects/<name>/       standalone only — one directory per project
│   ├── project.md         context, repo_path, conventions, definition of done
│   ├── journal.md         what each task decided, newest first — the project's memory
│   ├── artifacts/         non-code outputs
│   ├── tasks/<status>/    the board for this project
│   └── projects/<sub>/    subprojects, recursively
├── inbox/tasks/<status>/  quick tasks that belong to no project
├── archive/YYYY-MM/       retired tasks
└── .runtime/              slots, resource locks, worker state, run logs — not yours to edit
    └── index.db           derived search index; delete it any time, it rebuilds
```

An embedded board is the same tree with one level taken out: there is exactly one
project, so `project.md`, `journal.md`, `artifacts/` and `tasks/<status>/` sit at
the board root rather than under `projects/<name>/`, and there is no `projects/`
directory at all. A directory is a project on this board because it contains
`tasks/`, which is as true of the root as of anywhere else.

Statuses: `backlog`, `ready`, `in-progress`, `blocked`, `needs-human`,
`review`, `done`, `failed`, `cancelled`.

## Concurrency: leases, not locks

The wrapper holds the lock, never the agent. An agent can forget to close a
task or die mid-run; `bin/tick` cannot — it has a `finally` block, and the
kernel releases its `flock` when the process ends, including `kill -9` and
reboot. Hence the invariant:

> A task is `in-progress` if and only if a live holder owns its resource lock.
> After the wrapper exits, no task is left `in-progress`.

Three independent signals answer three different questions:

| Signal | Question | Mechanism |
| --- | --- | --- |
| Ownership | Is this resource taken? | `flock` on `.runtime/locks/<resource>.lock` |
| Liveness | Does the process exist? | the kernel, via that same `flock` |
| Progress | Is it actually working? | the agent's output stream |

The third is the interesting one. The agent is launched with
`--output-format stream-json`, and the wrapper reads its stdout line by line.
Every tool call and token is a line, so progress is evidence the agent cannot
forget to emit — silence is a real stall rather than something we have to
trust it to report. Each line updates the heartbeat in that run's
`.runtime/workers/<generation>.json`.

Ages are measured with a monotonic clock. On macOS that clock does not advance
while the machine is asleep, so a laptop that slept for six hours does not look
like a six-hour stall. `boot_id` guards against comparing values across boots.

The agent's two streams are read by a thread each rather than by `select()`,
which refuses a pipe on Windows. The heartbeat, the `STOP` check and the wall
clock run on a one-second timer of their own and not on the reader going idle: an
agent narrating without a pause never goes idle, and hanging the brakes off
idleness would have made them unreachable for exactly the runs most worth
stopping.

### Failure modes

| Symptom | How it is caught | What happens |
| --- | --- | --- |
| Agent legitimately works for 40 minutes | `flock` held, events flowing | Tick exits quietly — this is normal |
| Agent forgets to close the task | `finally` sees `in-progress` | attempt burned, back to `ready`, logged |
| Process killed with `kill -9` | kernel released the `flock` | Next tick reclaims it as an orphan |
| Machine rebooted | `flock` did not survive | Same |
| Machine slept for hours | monotonic clock did not advance | Nothing happens; work continues |
| Agent wedged on a network call | no stdout for `stall_timeout_sec` | SIGTERM, then SIGKILL, back to `ready` |
| Task running for hours | `max_wall_sec` | Terminated; resumes from the last checkpoint |
| Killed process revives and writes | `generation` no longer matches | Exits without touching the task (fencing) |
| You create `STOP` mid-run | checked in the read loop | Agent terminated cleanly, attempt refunded, task first in line on resume |
| Plan usage limit reached | limit wording in the CLI's stderr or failed result event | Attempt refunded, task requeued, loop waits for the reset |
| No network, sign-in gone, disk full | the tool's own wording in that same evidence | Attempt refunded, task requeued behind the retry backoff |
| The project's working copy is gone | the wrapper looks for `repo_path` itself | Same |

The running wrapper enforces the stall and wall-clock limits on itself. The
watchdog in the *next* tick is the backstop for a wrapper that is itself
wedged, and it requires two consecutive silent ticks before killing anything so
that a long build or a wake-from-sleep boundary is not mistaken for a stall.

## Task selection

`bin/task next` returns a single task, and only the CLI decides which. A task
is eligible when it is `ready`, every `depends_on` id is closed, `ready_after`
has passed, `attempts < max_attempts`, and it is not reserved for a human. Ties
break by priority (`P0` … `P3`), then creation time. The agent never picks its
own work.

A dependency counts as closed when it is `done`, `cancelled`, **or archived** —
archiving a finished task must not deadlock everything that depended on it. For
the same reason `archive/` is consulted when a new id is allocated: ids are never
reissued, so a reference in a commit message or a log always names one task.

### Work the loop must not take

Two fields keep work out of the loop's hands, and both are checks rather than
conventions:

- `owner: human` on a task. It is eligible for nobody but you, wherever it sits.
- `manual: true` in a project's `project.md`. The whole project is worked by
  hand; the loop neither picks its tasks up nor promotes them out of `backlog`.

`bin/task ready` refuses either of them, because `ready` means available to the
loop and this work never is — it would land there and never move, eligible for
nobody, in the one queue whose whole meaning is eligibility. `--force` is the
door if you meant it.

The second one guards the board's own project. Its tasks change `bin/tick` and
`lib/tasklib.py`, and the agent that would work them is running `bin/tick` and
importing `lib/tasklib.py` at the time — a run that rewrites the code holding its
own lease loses the ability to close its own task. That used to be a paragraph in
`project.md` asking the loop not to, which is a request, not a boundary.

A requeued attempt does not run again immediately. Each failure pushes
`ready_after` out by an exponential backoff (`retry_backoff_sec`, doubling, capped
at `retry_backoff_max_sec`), so a systemic failure — no network, a broken checkout —
cannot burn every attempt within a few ticks and park a healthy task in `needs-human`.

## Autonomy and guardrails

The agent runs with permissions bypassed and may commit, push, deploy to existing
production infrastructure, and install dependencies. It works on whatever branch
the repository is already on and may not change it.
Its boundary is `.claude/hooks/guard.py`, which runs before every tool call and
denies, unconditionally:

- `git push --force` in any form
- changing branches: `git switch`, `git checkout <branch>`
- `DROP TABLE` / `DROP DATABASE` / `TRUNCATE`, and `DELETE` without a `WHERE`
- sending email through any CLI or API
- creating paid resources, subscriptions or plan upgrades
- recursive deletion of a filesystem root or home directory
- deleting the board, its history or its runtime state — by any name, including a
  bare relative one

A prompt rule can be forgotten or reasoned around; a `PreToolUse` hook cannot.
Rules live in `.claude/deny.json` and can be edited without touching code.
Denials are recorded in `.runtime/guard.log`.

A path-matching rule is only as good as the spellings it knows, so a rule may name
this board as `{{ROOT}}`, `{{ROOT_TILDE}}` (the `~` form) or `{{ROOT_NAME}}` (the
directory name), and each of those matches whether the shell escaped or quoted the
spaces in it. Bash commands are matched with their working directory on a first
line, because the wrapper starts the agent *in* the board: the shortest way to
destroy it needs no path at all, and a rule that only knows absolute paths never
sees it. `tests/test_board.py` covers each of these spellings.

`STOP` is the manual brake: create the file and the loop pauses immediately,
including the tick that is currently running.

## What a task looks like

`bin/task new` writes this skeleton; you fill it in, or the agent does. The
frontmatter is managed by the CLI — the only fields worth setting by hand are
`type`, `size`, `due`, `priority`, `depends_on`, `ready_after` and `tags`.

```markdown
---
id: MY-014
title: Retry failed webhook deliveries with backoff
project: myapp
status: ready
type: bug
size: M
priority: P1
owner: agent
created: 2026-01-04T09:12:00Z
depends_on: [MY-012]
ready_after:
due: 2026-01-31
attempts: 0
max_attempts: 3
tags: [webhooks]
---

## Goal

One or two sentences: what should be true afterwards that is not true now.

## Acceptance criteria

- [ ] A failed delivery is retried with exponential backoff, at most 5 times
- [ ] Retries are visible in the delivery log
- [ ] `pytest tests/test_webhooks.py` passes

## Context

Links to the files, artifacts and decisions that matter. Not a summary of the
code — the agent can read the code.

## Constraints

- No new dependencies

## Attempts

- 2/3 2026-01-05T11:40:00Z failed: the staging queue was empty, so nothing retried

## Checkpoint

Written by the agent as it works: where it got to, what remains, which commits.

## Log

_31 entries in `MY-014.log.jsonl`; the 8 most recent are kept here._

- 2026-01-04T09:12:00Z agent: created in ready
```

The acceptance criteria are the whole contract: they decide when the task is done,
and a task whose criteria are empty is a note, not a task. `## Goal` and `##
Acceptance criteria` are what an agent reads first; `## Checkpoint` is how a run
that was interrupted picks up where it left off rather than starting over.

The last three sections are written by machines and are all bounded, because the
task file is what an agent reads on every attempt. `## Attempts` keeps one line
per attempt, newest first — what was tried and what broke. `## Log` keeps only
the most recent entries; the full history lives in a `<id>.log.jsonl` sidecar
beside the task file, which follows it between statuses and into the archive.
`bin/task show` puts the two back together for a human, and `--raw` prints the
file as it is on disk. How much is kept is `task_log_kept` and
`task_attempts_kept` in `config.json`.

Left unbounded, `## Log` grew forever inside the one file every attempt reads: by
the tenth tick most of the agent's context was its own "checkpoint saved" lines.

`type` is one of `bug`, `feature`, `chore`, `research`, `ops`; `size` is `S`, `M`
or `L`; `due` is a date. They are a fixed vocabulary rather than free tags because
`bin/task lint`, `bin/task list --json` and the web board all read them — a field
that means whatever each author felt like cannot be filtered or checked.

## What the worker prompt already knows

`prompts/tick.md` is rendered per run, and it carries what the wrapper knows so
that the agent does not spend tool calls — or context — rediscovering it:

- which attempt this is, out of how many, and what happens if this one fails;
- the wall-clock ceiling for the run, and how long silence may last before the
  watchdog kills it;
- the current `## Checkpoint`, inline, so a resuming run starts from it;
- what earlier attempts tried and what broke, from `## Attempts`;
- a bounded brief of the project's `project.md`;
- the head of the project's journal — what the tasks before this one decided.

The placeholders are plain `{{NAME}}` substitutions, so an edited prompt keeps
working as long as the names stay. An agent still has the task file and
`project.md` for anything the brief cut off.

## What the project remembers between tasks

A board that runs for a month has a problem that a board running for a day does
not. The task an agent is handed was written some time ago, against a repository
that the tasks since have changed, by an agent that remembers none of them. It
re-answers questions this project has already answered, or answers them the other
way, and nothing on the board says which of the two answers the project is
actually living with.

`projects/<name>/journal.md` is the fix, and it is deliberately the smallest one
that works: one short paragraph per task that landed, newest first.

```bash
bin/task done MY-014 -m "all four endpoints migrated, CI green" \
  -d "Retries back off per destination rather than globally, because one slow
      endpoint was stalling every other delivery. Dead letters are still
      dropped on the floor — that is MY-021."

bin/task journal myapp          # the whole file, newest first
bin/task journal myapp --brief  # exactly what the next prompt will be given
```

Three things keep it from becoming documentation nobody maintains:

- **It is written at the only moment an agent is guaranteed to reach.** `done`
  and `review` refuse to close a task without `-d`, and `move <id> done` is
  guarded from the same side, so the requirement has no way out but `--force`.
  Asked for at any other point it would simply be skipped.
- **An entry is capped at a paragraph** (`journal_entry_chars`, 700 by default)
  and refused if it is one word. A rejection says where the text belongs: the
  task log is unbounded and is where detail goes. Nothing is truncated — half an
  entry keeps the claim and drops the reason for it.
- **Only the head of the file is ever inlined** (`journal_brief_chars`, 1600),
  cut at an entry boundary, with a line saying how many older entries were left
  out and where they are. The file grows; what the agent reads does not.

The prompt then asks for the other half of it: check the task against what is
actually there before starting it. An entry that already settles a question the
task asks is a decision to follow or to reverse deliberately, not a suggestion;
criteria that describe something no longer in the repository get rewritten and
logged. `prompts/groom.md` does the same sweep across the open board while it is
idle. The entry outlives the task — a task archived after thirty days takes its
log with it and leaves its paragraph behind.

## Making `ready` mean something

`ready` is a promise: whatever is in it, an agent can act on in a fresh session
with nothing but the task file in front of it. `bin/task lint` is that promise
written down.

```bash
bin/task lint                  # everything in ready; non-zero if any is unfit
bin/task lint MY-014           # one task
bin/task lint --status backlog # what is next in line
bin/task lint --all --json     # every open task, for a script
```

It reports a task whose title names no outcome, whose `## Acceptance criteria`
holds nothing but the empty `- [ ]` that `task new` writes, whose `type` is unset
or outside the vocabulary, whose `depends_on` points at a task that does not
exist, or whose project has no `repo_path` — an agent would not know where to work.

Lint is advisory in `backlog` and binding at the `ready` boundary. `bin/task
ready`, `bin/task move <id> ready` and `bin/task new --ready` all refuse a task
that fails it; `--force` is the deliberate override, and `new --ready` on a failing
task keeps the task in `backlog` with its body intact rather than throwing away
what you typed. Grooming (`prompts/groom.md`) works from lint instead of from
judgement: what lint reports gets fixed in the task, or the task moves to
`needs-human`.

One path is deliberately left open. When a run dies and the wrapper reclaims its
task, `bin/tick` calls `Task.move()` in the library, below the CLI and below lint.
A recovery path that lint could shut would strand the task `in-progress` with
nobody behind it — exactly the orphan `bin/task doctor` exists to catch.

## Usage

```bash
bin/task project-new myapp --repo ~/code/myapp --prefix MY
bin/task new --project myapp --title "Retry failed webhook deliveries" \
  --type bug --size M --priority P1 --body-file /tmp/criteria.md --ready
bin/task list --verbose
bin/task search "webhook"      # find prior work before writing a duplicate
bin/task lint                  # is everything in ready fit to be worked?
bin/task board                 # ASCII
bin/board serve                # live web board on 127.0.0.1:7777
bin/task doctor                # verify invariants
bin/task index --rebuild       # throw the search index away and build it again

bin/install --into ~/code/other --check   # vendor a board there, or say what would change

bin/loopctl doctor             # check this machine can run the loop
bin/loopctl run                # one tick, in the foreground
bin/loopctl install            # render the scheduler job and start the loop
bin/loopctl unit               # print that job without installing it
bin/loopctl stop / start       # pause, and release again
bin/loopctl status
bin/loopctl logs -f
```

## Subscription, usage and limits

Ticks run through the `claude` CLI, so they use whatever authentication it already
has. On a Claude subscription (`claude auth status` reports `authMethod: claude.ai`)
there is no per-run charge: runs draw on the plan's usage allowance. With an API key
in the environment they are billed per token instead. Check which one you are on
before letting the loop run unattended:

```sh
claude auth status
```

Each run's `total_cost_usd` is accumulated per day in `.runtime/spend.json` and shown
on the board as **usage today**. On a subscription that figure is a *gauge*, not a
bill — it is the equivalent value of the tokens used, and it is the number to watch
for anomalies. There is no automatic ceiling; `STOP` is the brake.

### Failures that say nothing about the task

The wrapper cannot tell *this task is broken* from *this machine could not work right
now*, and the attempts counter only makes sense for the first. So the second is a class
of its own — an **infra outcome** — and every cause in it is handled the same way:

- the attempt is refunded, so a bad hour never pushes a healthy task toward `needs-human`;
- the task goes straight back to `ready`, with the reason in its `last_error`;
- exactly one wait is installed, and the outcome says where it lives.

Five causes are recognised today: the plan's usage limit, no network, missing or expired
credentials, a working copy that is not there, and a full disk. Adding the next one is a
row in `INFRA_CAUSES` in `bin/tick`, not another branch in `settle_task` — which is how
that file would otherwise turn into a pile of special cases.

**Where the wait lives** is the one thing that differs, and it follows from the cause. A
cause that names its own end — the plan limit, which reports a reset — holds the *whole
loop* until then: `.runtime/usage_limit.json` records the moment (parsed from the message
when it carries one, otherwise `usage_limit_backoff_sec` from now), every tick until then
exits without claiming work, the board shows a banner, `bin/task doctor` reports the hold,
`bin/loopctl start` releases it early, and you are notified once per window rather than
once per tick. A cause that names no end holds only the *task*, on the ordinary retry
backoff, so the loop can go on working other projects while it clears. Never both: a
`ready_after` on the task would outlive the loop-wide hold and make `bin/loopctl start`
not actually release anything.

Nothing is lost while waiting: work already done sits in the task's checkpoint, and the
loop simply resumes when the window reopens.

An infra outcome is inferred **only** from the CLI's own stderr and from its final
`result` event when that event reports an error — never from the agent's transcript. The
distinction is not cosmetic. The transcript echoes back every file the agent read, and the
board's own README, `NOTIFY.md` and task logs all contain this wording; a detector that
reads the transcript eventually fires on its own output, and one real outage teaches the
loop to keep refunding forever. Two things bound the damage when a pattern is wrong
anyway: every note the handler writes is worded so that it does not match any pattern
(there is a test for exactly that), and the refund is capped by `infra_max_refunds` —
**one cap for the whole class, not one per cause**, because a task cannot tell which
detector was wrong about it, and per-cause caps would let causes take turns and never
reach the ceiling. The missing working copy is the one cause with better evidence than
text: the wrapper knows where `repo_path` is meant to be, so it looks.

## Tuning

Everything lives in `config.json`. The values that matter most:

| Key | Default | Meaning |
| --- | --- | --- |
| `stall_timeout_sec` | 600 | silence on the event stream before a run counts as stalled |
| `stall_strikes` | 2 | consecutive silent ticks before the watchdog kills |
| `max_wall_sec` | 7200 | hard ceiling for one attempt |
| `max_tick_wall_sec` | 21600 | hard ceiling for one tick's run of tasks; 0 means none |
| `groom_interval_sec` | 3600 | the most often an idle loop may groom; it grooms only when there is something to groom |
| `default_max_attempts` | 3 | failures before a task escalates to `needs-human` |
| `retry_backoff_sec` | 600 | delay before a requeued attempt becomes eligible; doubles per attempt |
| `retry_backoff_max_sec` | 21600 | ceiling for that backoff |
| `task_log_kept` | 8 | log entries left in the task file; the rest live in the sidecar |
| `task_attempts_kept` | 5 | lines kept in a task's `## Attempts` section |
| `project_brief_chars` | 1400 | how much of `project.md` the worker prompt inlines |
| `local_policy_chars` | 2000 | how much of `LOCAL.md` a prompt inlines, cut at a line |
| `journal_brief_chars` | 1600 | how much of `journal.md` the worker prompt inlines, cut at an entry |
| `journal_entry_chars` | 700 | the most one journal entry may hold; a longer one is refused, never trimmed |
| `daily_spend_limit_usd` | 0 | the day's ceiling on what the loop may spend; `0` means none, and holds new work rather than killing a run |
| `usage_limit_backoff_sec` | 3600 | how long to wait when the plan's usage limit is hit and the message carries no reset time |
| `infra_max_refunds` | 3 | refunded attempts per task, for the whole infra class; the old name `usage_limit_max_refunds` is still honoured |
| `tick_interval_sec` | 600 | how often the scheduler fires a tick; reinstall after changing |
| `notify_desktop` | true | raise a desktop banner; the old name `notify_macos` is still honoured |
| `agent` | `claude` | which profile in `agents` the wrapper runs |
| `max_concurrent_tasks` | 1 | how many runs may be in flight, one per resource |

## Rules that belong to one copy

`AGENTS.md` is mirrored, so a rule written there is a rule for every board made
from this one. Not every rule is like that: a mode granted for a week, a machine
this copy cannot reach, an instruction meant for the copy that actually runs the
loop. Written into `AGENTS.md` such a rule diverges the two checkouts by
construction, and the next `bin/mirror` reverts it without saying so — which is
exactly how a granted mode was lost here once.

`LOCAL.md` at the board root is where those go. It sits in `bin/mirror`'s
`CONTENT` list beside `config.json` and `.mcp.json`: never copied in either
direction, never removed by `--prune`. Both prompts inline it, bounded by
`local_policy_chars`, so a run is told this copy's rules without being told to go
and look for them.

## The framework is not the run's to edit

`.claude/deny.json` refuses writes to `bin/`, `lib/`, `prompts/`, `tests/`,
`.claude/`, `.opencode/plugin/`, `AGENTS.md`, `README.md`, `config.json` and
`LOCAL.md` — but only from inside a loop run. `LOCAL.md` is not framework and is
never mirrored; it is on the list because it is the rules binding on that run,
and a run that can rewrite its own rules has none. `bin/tick` sets `PROJECT_TASKS_RUN` on every agent it starts, and
`guardlib.rule_applies` skips any rule whose `env` names a variable that is not
set, so the same rule in the same checkout is silent for a human editing those
files by hand. That conditional is what makes the rule possible at all: the
deny list is mirrored, and in the template these are exactly the files the work
is about.

It closes the way this board's two checkouts drifted apart. Three commits on the
running board changed `AGENTS.md` and `prompts/tick.md` from inside a run working
an unrelated project's task, each landing under that task's commit message; every
one of them had to be found by hand and carried back into the template.

Reading is untouched, and so is running `bin/task` — an agent that could not call
it could not close its own task. The shell rule covers redirection, `tee`,
`sed -i`, `cp` and `mv`; a deliberate `python3 -c` that opens a file for writing
will still get through, and that is the honest limit of a pattern.

## A ceiling on the day

`bin/tick` has always recorded what each run cost into `.runtime/spend.json`, and
nothing ever read it back — the numbers said afterwards what a day had cost and
never before. `daily_spend_limit_usd` is that number turned into a limit: above it
the tick starts no new work and says so, and because `spend.json` is keyed by UTC
day the hold lifts itself when the day turns. Nothing has to remember to clear it
and a restart cannot lose it.

It is a gate on starting work, never a kill switch. A run cut off halfway has
already paid for the attempt it was in the middle of, and the task would be
started again from nothing tomorrow. Grooming is behind the same gate.

The default is `0`, which is no ceiling — a board that has never watched its own
spending should not discover a limit it did not set.

## Grooming an idle board

Grooming is an agent run, so an interval alone is the wrong thing to spend it on:
a board with nothing ready stays that way for as long as the human is busy, and
`groom_interval_sec` on its own buys one agent an hour to be told so. The wrapper
answers the question in Python first and only then hands it to an agent. It grooms
when a `ready` task exists to lint, when a `backlog` or `blocked` task could move
to `ready`, or when a task file has changed since the last grooming — the last one
because a duplicate of work closed months ago is something only an agent can see.

Archiving is deliberately not on that list: `housekeeping()` retires closed tasks
on every tick without an agent, so it can never be the reason to spend one. A board
that is genuinely drained does nothing and costs nothing, and starts grooming again
by itself the moment a task appears, a blocker clears or a `ready_after` comes due.

## Working on several projects at once

Set `max_concurrent_tasks` above 1 and a tick that finds one project busy takes a
task from another instead of standing still. Each run needs two things:

- **a slot** — `flock` on `.runtime/slots/<n>.lock`, one per permitted concurrent
  run. This is what bounds the total: without it a board with ten projects would
  start ten agents at once, on one machine and one usage plan.
- **its resource** — `flock` on `.runtime/locks/<resource>.lock`, where the
  resource is the project's `repo_path`, resolved. Two agents editing and
  committing in the same working copy would ruin each other's work, so a busy
  repository is skipped and the tick moves on down the eligible list. A project
  with no `repo_path` shares nothing, so its tasks are exclusive only with
  themselves.

The unit is deliberately the *working copy*, not the project: two projects can
point at one repository, and then they must not run at the same time. The
corollary is worth saying plainly — **several tasks in one project do not run in
parallel**, because they are one checkout. Parallelism here is across projects.

Nothing else about the model changes. The invariant is the same sentence asked
once per resource; a task is fenced by the `lease_generation` written on it, so a
run that was reclaimed and re-claimed elsewhere cannot settle a task it no longer
owns; and reclaiming an orphan is still "the lock is free while the task says
in-progress", now asked per task rather than for the board as a whole.

Two shared files are serialised because several ticks now write them: the board's
git commits (`.runtime/git.lock` — concurrent commits collide on `index.lock`) and
the generation counter, which would otherwise hand the same number to two runs.
