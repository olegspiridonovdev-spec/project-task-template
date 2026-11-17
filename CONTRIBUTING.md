# Working on the board itself

This repository is the template. Boards made from it are copies, and the copying
is one-directional, which is the only thing here that is easy to get wrong.

## Where a change belongs

| Change | Where |
| --- | --- |
| the board's code, rules, prompts, guard | **here**, in this repository |
| a setting for one board (`config.json`) | that board, never here |
| a rule for one board (`LOCAL.md`) | that board, never here |
| a task about the board's own code | this repository's own project on the board |

Anything written into `AGENTS.md` is a rule for every board that will ever be
made from this one. A mode granted for a week, a machine only one copy can
reach, an instruction meant for the copy that runs the loop — those go in
`LOCAL.md`, which is never mirrored and never pruned. Written into `AGENTS.md`
such a rule diverges the copies by construction and the next `bin/mirror`
reverts it without saying so. That has already happened here once.

## Before you push

```sh
python3 -m unittest discover tests      # 163 tests, about 90 seconds
bin/task doctor                         # board invariants
sh install.sh --into /tmp/scratch-repo --check
```

CI runs the same suite on Ubuntu, macOS and Windows, installs a board on each,
and runs the installed board's own suite where it landed. It also runs
`shellcheck` on `install.sh` and PSScriptAnalyzer on `install.ps1`, which are the
two files that cannot be exercised from every machine.

**Every behaviour change needs a test.** Not for coverage — because this thing
runs unattended against real repositories, and the failure mode of an untested
change is a task closed wrongly, or a commit nobody meant to make, discovered a
week later.

## The commit messages

They say what changed and why that way rather than the obvious alternative. The
history is the design record here — a good deal of the reasoning in this
repository exists nowhere else — so a message that only names the files it
touched has thrown away the part worth keeping.

## Rolling a change out to a running board

```sh
bin/mirror /path/to/other-board --check   # what has drifted
bin/mirror /path/to/other-board           # bring it in line
```

It refuses to write into a board whose loop is running, refuses to overwrite
uncommitted work there, and refuses to revert: if the destination holds a version
this repository has never held, it stops rather than silently discarding it.
`--force` overrides that and almost never should.

For a board vendored into somebody's repository, the same job is `install.sh`
run again — the framework is replaced, the settings and the work are not.
