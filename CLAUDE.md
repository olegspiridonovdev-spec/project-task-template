# Project Tasks

@AGENTS.md

<!--
This file used to be a symlink to AGENTS.md, so that Claude Code found the rules
under the name it looks for without a second copy to keep in step. A symlink is
the wrong tool for a board that has to be checked out on Windows: git there
writes the link as an ordinary file containing the text `AGENTS.md` unless the
user has Developer Mode on and `core.symlinks=true`, and an agent handed that
file reads one line of nonsense and works with no rules at all.

`@AGENTS.md` is Claude Code's own import syntax and does the same job as a plain
file that every platform can check out. There is still exactly one copy of the
rules, and it is still AGENTS.md.
-->
