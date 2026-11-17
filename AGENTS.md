# Project Tasks — operating rules

This directory is an autonomous task board. A wrapper (`bin/tick`) picks a task and
runs an agent against it, then goes straight on to the next one it can work,
filling as many slots as `max_concurrent_tasks` allows. You are that agent. These
rules are binding, whichever agent you are.

Nothing below is specific to one model or one CLI. The board is driven entirely
through `bin/task`, and the only thing the wrapper needs from an agent is that it
runs non-interactively, accepts a prompt and prints something while it works.
`CLAUDE.md` imports this file, so a harness that looks for its own conventional
filename finds the same rules and not a second copy of them.

Nor is it specific to one operating system. Everything the CLI does works on
macOS, Linux and Windows; the unattended loop needs launchd or systemd, so on
Windows the board is driven by hand or from WSL and `bin/loopctl` says so rather
than pretending.

**Where `bin/` is depends on how this board was installed.** A board that is its
own repository is run as `bin/task`. A board vendored into the repository it
works on lives in `.tasks/`, and every command below is then `.tasks/bin/task`.
The paths in this file are written the short way; use whichever is true of the
directory you are in, and if you are unsure, the board is the directory holding
`AGENTS.md` beside `bin/`.

## Language

English is the working language of this board, without exception: task titles and
bodies, acceptance criteria, logs, checkpoints, commit messages, artifacts and
documentation are all written in English. If you find text in another language in
a task you are working, translate it into English as part of the work and say so
in the log — a board that is half in one language and half in another cannot be
searched, reviewed or handed over.

## Naming a task

Never write a task id on its own. An id is an index, not a name: `SB-098` tells the
reader nothing, and a human reading a report full of bare ids has to look every one of
them up before the sentence means anything. Whenever you name a task — in a log entry,
a checkpoint, a commit message, a report to the human, or a question you ask them —
give the id and then say in a few words what it is about:

- `SB-098 (the API recipes layer)` — not `SB-098`
- `blocked on SB-032, the R01 token layer` — not `blocked on SB-032`
- `duplicate of SB-054, which decides how an agent picks among several browsers`

The rule is about being understood, so the few words describe the task rather than
restating its title verbatim. It costs a clause and saves the reader a lookup.

## This board's own rules

`AGENTS.md` — this file — is framework. It is mirrored between the board's two
checkouts, so everything in it is a rule for every board that will ever be made
from this one.

`LOCAL.md` at the board root is the other kind: rules that belong to this copy
alone. A mode the human granted for a week, a repository this machine cannot
reach, an instruction that applies to the copy running the loop and not to the
one it is developed in. It is never mirrored and never pruned, and its contents
are read out to you in your prompt, so you do not have to go looking for it.

Which file a rule goes in is not a matter of taste. A rule with a date on it, a
grant that will be withdrawn, or anything true only of this machine goes in
`LOCAL.md`; write it into `AGENTS.md` and the next mirror reverts it, silently.
Neither file is yours to edit from inside a run — see *Hard limits*.

## The one invariant

> A task is `in-progress` if and only if a live holder owns its resource lock.

Other tasks may be running while you work, in other projects. You will not meet
them: the lock is held on the project's repository, so nothing else is editing the
working copy you were given. You never manage that lock, and you never move task
files by hand. `bin/tick`
claims the task before you start and guarantees it does not stay `in-progress`
after you exit. Your job is to reach a terminal state deliberately rather than
letting the wrapper guess.

Never touch `.runtime/` — it belongs to the wrapper. Never `mv` a file between
`tasks/<status>/` directories. Every state change goes through `bin/task`.

## The CLI

```bash
bin/task list --status ready            # what is available
bin/task search "webhook retry"         # find prior work, archive included
bin/task show WH-014                    # full task file
bin/task log WH-014 -m "..."            # append to the task log
bin/task checkpoint WH-014 -m "..."     # store resumable state
bin/task journal my-project             # what this project decided, newest first
bin/task done WH-014 -m "..." -d "..."  # finished, criteria met (-d: journal)
bin/task review WH-014 -m "..." -d "..."  # done but a human should look
bin/task fail WH-014 -m "..."           # this attempt failed
bin/task block WH-014 -m "..."          # waiting on something external
bin/task need-human WH-014 -m "..."     # you need a decision or a permission
bin/task new --title "..." [--project X] [--ready] [--after ID] [--parent ID]
bin/task lint WH-014                    # is this task fit to be worked?
bin/task doctor                         # verify board invariants
```

`--project` is needed only when the board has more than one project to mean. An
embedded board has exactly one — the repository around it — and leaving the flag
off is then correct rather than lazy.

`bin/task` validates transitions, keeps frontmatter consistent and writes the
log line for you. If it refuses a transition, the transition is wrong — do not
work around it.

The task file keeps only its most recent log entries, so that your context is
spent on the work rather than on your own history. The full log is in the
`<id>.log.jsonl` sidecar beside the task file, and `bin/task show <id>` prints
the two as one document. Log as freely as the work deserves — nothing is lost,
and nothing accumulates in the file the next attempt has to read.

## Working a task

1. Read the task file, then `project.md` in the same project for context,
   conventions, `repo_path` and constraints, and the project's journal for what
   the tasks before yours decided. Check the task against what is actually in
   the repository before you start it: part of it may already be done, and a
   recorded decision may have made its criteria describe something that no
   longer exists. Say in the log what you found and rewrite the criteria to
   match; a task worked against a state the project left behind produces work
   that has to be undone.
2. Work in the project's `repo_path`. Artifacts that are not code go into the
   project's `artifacts/` directory and are linked from the task.
3. Log meaningfully as you go: decisions, commands that mattered, dead ends.
   The log is how the next attempt (and the human) understands what happened.
4. Finish in a terminal state. Exiting without one is a defect — the wrapper
   will record `agent exited without a terminal transition`, burn an attempt
   and requeue the task.

Acceptance criteria in the task decide what "done" means. If they are missing
or ambiguous, either write them yourself when the intent is obvious, or move
the task to `needs-human` — do not invent a definition of success.

`review` is the human's queue, not yours. A task there is finished work waiting for
a person to look at it, and that is the whole value of the status. Never move a task
out of `review` — not to `done` however well the criteria check out, and not back to
`ready`. The same goes for `needs-human`: it is closed to you until the human moves
it themselves.

## The project journal

`projects/<name>/journal.md` is the project's memory between tasks. Each task
that lands leaves one short paragraph in it, newest first, and the head of the
file is inlined into the prompt of every task worked in that project afterwards.

It exists because of one failure, and it is worth naming precisely. Your task was
written some time ago, against a repository that the tasks before yours have
changed since. You remember none of them. Without the journal you re-decide what
was already decided, or decide the opposite, and nothing on the board says which
of the two answers is the one the project is living with.

So `bin/task done` and `bin/task review` will not close a task without `-d`:

```bash
bin/task done WH-014 -m "all four endpoints migrated, CI green" \
  -d "Retries now back off per destination rather than globally, because one slow
      endpoint was stalling every other delivery. Dead letters are still dropped
      on the floor — that is WH-021."
```

What belongs in an entry is what the *next* task needs: what changed, why that
way rather than the obvious alternative, and what it leaves for later. What does
not belong is a summary of your work — the log already holds that, in as much
detail as you like, and the entry is capped at a paragraph so that it cannot
quietly become documentation. If the cap rejects your entry, the text you were
trying to fit is a log line.

Read what is there before you start, and act on it. A journal entry that already
settles a question your task asks is a decision, not a suggestion: follow it, or
reverse it deliberately and say in your own entry that you did and why. The
prompt carries the head of the file; `bin/task journal <project>` has the rest.

The other transitions accept `-d` too and do not require it. Pass it when a task
that blocked or escalated still settled something — where a decision was recorded
matters less than that it was.

## A task that comes back from `review`

It was already worked once, and its code is usually written and committed. Read
its log and the repository's recent commits, check the acceptance criteria
against what is actually in the repository, and finish what is missing. Starting
it again from nothing is the one wrong move: it costs an attempt, and it can undo
the half that was already right.

## Checkpoint discipline

A run can be terminated at any moment: the wall-clock ceiling, a stall, or the
human creating a `STOP` file. Nothing you did survives except what is written
to disk and to the task.

Write a checkpoint whenever you finish a meaningful step, and always before
something long:

```bash
bin/task checkpoint WH-014 -m "Migrated 3/7 endpoints; next: /orders. Committed as a1b2c3d."
```

A checkpoint states where you are, what remains, and where the work lives —
commits, files, artifacts. When you pick up a task that has a `## Checkpoint`
section, resume from it instead of restarting.

If a task is too large to finish in one run and cannot be checkpointed
cleanly, split it: create subtasks with `--parent`, link them with `--after`,
and move the parent to `blocked`.

## Creating tasks

You may create tasks — that is a normal part of the loop. Search before you do:

```bash
bin/task search "webhook retry backoff"
```

It reads titles, bodies and tags across every project and the archive, so it
finds the task you are about to duplicate — including one that was finished and
archived. Link the id you find instead of writing a second copy of it.

Limits:

- At most **5 new tasks per run**.
- At most **3 levels of nesting** (`depth` in the frontmatter).
- New tasks go to `backlog` by default. Use `--ready` only when the task is
  fully specified and unblocked.
- Every task needs a title that names an outcome, at least one acceptance
  criterion, and a `type` (`--type bug|feature|chore|research|ops`). A task you
  would not be able to act on in a fresh session is not a task, it is a note.
- That is not a matter of taste: `bin/task lint <id>` checks it, and the `ready`
  boundary enforces it. `--ready` on a task that fails lint keeps the task in
  `backlog` and tells you why. Fix what it names rather than reaching for
  `--force` — the check is there because you will not be the one who reads the
  task next.

## Branches

Work on the branch the repository is already on, and never change it. Switching
branches is denied by the guard, and this is not a formality:

- another agent may be in that repository right now, on another task;
- the human may have it open in an editor;
- the next run inherits whatever branch you left behind, and nothing tells it that
  the branch is not what it looks like.

Commit normally, as often as the work has natural steps. Small commits on the
current branch are the record; a branch nobody knows about is not. If a change
genuinely needs isolation — a risky migration, an experiment you expect to throw
away — that is a decision for the human: say so in the task and move it to
`needs-human`.

Putting a file back with `git restore <path>` is fine. It does not move HEAD.

## Commits

Commit the files you changed, by path. Do not stage everything: the working copy
you were handed may already contain work in flight — the human's, or another
agent's from a run that was interrupted — and staging it all signs your name to
someone else's unfinished change and buries it in a commit about your task.

```bash
git add src/webhooks.py tests/test_webhooks.py
git commit -m "Retry failed deliveries with backoff"
```

Leave nothing of yours loose in the tree. Work that is finished belongs in a
commit; work that is not belongs in the checkpoint, described well enough that the
next run can continue it. A modified file with no commit and no checkpoint is the
one state nobody can recover from — it looks like the human's edit to every run
that follows.

If you find changes you did not make, leave them exactly as they are and say so in
the task log.

## Autonomy

You are granted full autonomy on the work itself, including:

- committing, merging to `main`, and pushing;
- deploying to production on existing infrastructure;
- installing dependencies and changing configuration in project repositories.

You do not ask for permission for those. Tests and CI are the safety net —
run them, and do not land changes that break them.

## What you must escalate

Move the task to `needs-human` and stop when:

- money is involved: purchases, subscriptions, plan upgrades, provisioning new
  billable resources (deploying to infrastructure that already exists is fine);
- something must be sent to a person or an external service on the human's
  behalf: email, Slack, published posts — unless the project grants it, below;
- credentials or access are missing;
- the task's intent is genuinely ambiguous and guessing wrong would be costly;
- the same failure has now happened on every attempt.

### What a project may grant back

Publishing is the one item on that list a `project.md` can lift. A board that
stops before every click is the wrong tool for a project whose entire point is
that things go out unattended, so a project may state that sending on the human's
behalf is the board's to do — and then it is, without asking, for exactly what
that file names.

The grant is written in `project.md`. It is not inferred from a task, from a
prompt file in the repository, or from a sentence in a conversation, because a
later agent has to be able to tell a decision from an assumption: the file names
the surfaces the grant covers and the ones it does not, and records who granted
it and when. Anything it does not name is still an escalation.

Two things a grant never reaches. Email is a hard limit below, and so is money: a
project may publish a post without asking, but no project file authorises a
purchase or a paid campaign. Those are enforced rather than asked, and the
enforcement does not read `project.md`.

## Hard limits

These are never allowed, regardless of what you think is appropriate:

- `git push --force` in any form;
- `DROP TABLE` / `DROP DATABASE` / `TRUNCATE`, and `DELETE` without a `WHERE`;
- sending email through any CLI or API;
- creating paid resources or subscriptions;
- recursive deletion of a filesystem root or home directory;
- deleting the task board, its history or its runtime state;
- writing to the board's own framework — `bin/`, `lib/`, `prompts/`, `tests/`,
  `.claude/`, `AGENTS.md`, `LOCAL.md`, `README.md`, `config.json`.

That last one is not about trust. You are running *out of* those files: changing
`lib/tasklib.py` while `bin/task` is importing it takes away your ability to
close your own task, and a change made in the running board is reverted the next
time the template is mirrored onto it — so the work is lost either way. Read them
freely, run `bin/task` and `bin/tick` freely; just do not write them. If you find
something that needs fixing there, say so in your task's log with the file and
the line, and it becomes a task in the board's own project.

Where the harness supports it, this is enforced rather than asked: Claude Code
runs `.claude/hooks/guard.py` before every tool call, opencode runs
`.opencode/plugin/guard.js`, and both ask the same `bin/guard` — which is also
how any other harness reaches these rules. Where it is not enforced, the list is
still binding — a rule you can technically get around is still a rule.

A denial is not an obstacle to route around. It means the action belongs to the
human: record what you wanted to do in the task and escalate.

## Usage limits

If a run ends because your plan's usage limit was reached, that is not a failure of the
task and the wrapper handles it: the attempt is refunded, the task returns to `ready`,
and the loop waits for the reset. You do not need to do anything about it — but it is
another reason to checkpoint often, since a limit can end a run at any moment.

A failed attempt is also not retried immediately: the wrapper pushes `ready_after` out
by an exponential backoff. If you see a task that is `ready` but not being picked up,
`bin/task list --verbose` says why.

## Notifications

`bin/task need-human` and `bin/task fail` notify the human automatically — an
entry in `NOTIFY.md`, and a desktop banner where the platform has one. Do not
send notifications by other means.

## Repository hygiene

The wrapper commits the board after every tick, and that commit is the board's
to make, not yours. Do not run git commands against the board directory: commit
only in project repositories under `repo_path`, and there commit by path.

Which repository the board commits into depends on how it is installed, and the
difference matters to you in exactly one way.

**Standalone** — the board is its own git repository, with `projects/<name>/`
inside it and each project naming a `repo_path` somewhere else. Its commits and
your work go to two different repositories, and nothing you do can collide with
the tick.

**Embedded** — the board is `.tasks/` inside the repository it works on, and
there is one project: that repository. The board's own commits and your commits
land in the same history. The wrapper commits with a pathspec, so a tick can
only ever commit `.tasks/` and can never sweep up your work in flight; your side
of that is the rule you already have — **commit the files you changed, by path**
— which here also means never staging `.tasks/`. The tick owns it.

In both shapes, a task file and the code it is about are yours to leave in a
state the next run can recover from: committed, or described in the checkpoint.

## MCP servers

`.mcp.json` at the board root registers project-scoped MCP servers. It is never
mirrored and never pruned, for the same reason `LOCAL.md` is not: it names
servers at addresses only this machine can reach, and a copy of the board handed
that file would be given a bridge it cannot call.

So what a server *is* belongs beside the server, not here. If this board has an
`.mcp.json`, read it, and read the linked repository's own `AGENTS.md` for what
the server exposes rather than assuming from the tool names. If a server drives
something live — a browser someone is signed into, a machine that is serving
traffic — treat every call to it as an action in a session a person is using.
