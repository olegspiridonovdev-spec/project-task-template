You are grooming the task board. There is no work available right now, so this
run is about keeping the board honest. Do not start any project work.

Board root: {{TASKS_ROOT}}
Projects root: {{PROJECTS_ROOT}}

Read `{{TASKS_ROOT}}/AGENTS.md` first, and then this board's own rules, which are
binding on this copy in addition to it:

{{LOCAL_POLICY}}

Then, in order:

1. `bin/task doctor` — fix any invariant violation it reports.
2. `bin/task lint` — everything in `ready` must be fit to be worked. Whatever it
   reports is a defect in the task, so fix it in the task file: write the
   missing acceptance criteria, set `type`, replace a placeholder title with one
   that names an outcome, point `depends_on` at a task that exists. If you
   cannot fix it without a decision only the human can make, move the task to
   `needs-human` quoting what lint said. Do not use `--force` to make lint quiet.
3. `bin/task lint --status backlog` — the same report for what is next in line.
   A `backlog` task that lints clean and is unblocked moves to `ready`; one that
   does not stays where it is until you have fixed what lint named. This is what
   "fully specified" means now — it is a check, not a judgement call.
4. `bin/task list --verbose` — review the rest of the open board:
   - `blocked` tasks whose blocker has cleared move to `ready` (lint applies).
   - Tasks that duplicate each other get merged: cancel the duplicate with a
     log line pointing at the survivor. `bin/task search "<words from the title>"`
     is how to find them — it reads bodies and the archive, so it catches the
     pair whose titles do not look alike and the task that repeats work already
     finished and archived, neither of which reading the list can show you.
   - Tasks that no longer make sense get cancelled with a reason.
   - `review` and `needs-human` are left exactly as they are. They are the human's
     queues; a groomed board that empties them has only hidden the queue.
5. `bin/task journal` — what each project has decided lately, newest first.
   Read it against what is still open in that project. A task the journal has
   already settled is the drift this step exists to catch: the work landed
   under a different task, or a decision recorded since has made this task's
   criteria describe something that no longer exists. Fix the task — rewrite
   the criteria, or cancel it quoting the entry that removed its reason for
   existing. Do not write journal entries here; grooming decides nothing, and
   an entry for a task that never ran would be a record of an intention.
6. `bin/task archive` — retire long-closed tasks.
7. Append a short grooming summary to `{{TASKS_ROOT}}/NOTIFY.md` **only** if
   something needs the human's attention. Silence is the correct output when
   the board is healthy.

Do not create new project work during grooming. Changing task states, writing
acceptance criteria and cancelling dead tasks is the whole job.

If search ever returns something the files disagree with, the files are right:
the index under `.runtime/` is derived, and `bin/task index --rebuild` throws it
away and builds it again from the task files.
