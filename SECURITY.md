# Security

This is an internal repository. Report anything here through the usual internal
channel rather than by opening a public issue anywhere.

## What this software does, which is the part worth thinking about

The board runs an agent **unattended, with permissions bypassed by default**,
against real repositories, and lets it commit, push, deploy to existing
infrastructure and install dependencies. That is a deliberate choice recorded in
the README, not an accident — but it means the interesting security questions
here are not the usual ones about parsing untrusted input.

Three boundaries hold it, and each is worth knowing the limits of.

**`.claude/deny.json`, enforced before every tool call.** It denies force
pushes, branch switches, destructive SQL, sending email, creating paid
resources, recursive deletion of a root or home directory, deleting the board,
and — from inside a loop run only — writing to the board's own framework.

Its limit is honest and worth stating: it matches *patterns against commands*.
A deliberate `python3 -c` that opens a file for writing gets through the
framework rule, and a rule only knows the spellings it was written with.
`tests/test_board.py` covers each spelling the board is known by, including the
bare relative one, because the shortest way to destroy a board needs no path at
all.

**Enforcement is per harness.** `lib/guardlib.py` holds the check and every
harness reaches it through `bin/guard`. Claude Code and opencode each have a
pre-execution adapter in this repository, so a copy of the board carries its own
enforcement. **A harness with no such hook cannot be stopped by anything here** —
`AGENTS.md` still binds it, and the difference between "cannot" and "must not" is
real. It is the main thing to weigh before pointing a new agent at a board.

**The escalation list.** Money, sending anything to a person or an external
service, missing credentials, and genuinely ambiguous intent all stop the run and
wait for a human. A project may lift exactly one item — publishing — and only by
naming the surfaces in its own `project.md`. Email and money are enforced rather
than asked, and that enforcement does not read `project.md`.

## Running it more cautiously

Change the agent profile in `config.json` from `bypassPermissions` to
`acceptEdits`. A headless loop cannot answer a permission prompt, so anything
needing one stalls and is requeued rather than proceeding — which is the safe
direction. The hard limits stay enforced either way.

## Secrets

The board stores none and wants none. It runs whatever agent CLI is on the
`PATH` and uses whatever authentication that CLI already has. Task files,
`journal.md` and `NOTIFY.md` are committed, so anything written into them is
committed too — treat a task body as public to everyone who can read the
repository the board is in.
