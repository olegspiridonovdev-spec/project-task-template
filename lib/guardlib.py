#!/usr/bin/env python3
"""The board's hard limits, in a form any harness can call.

The agent runs unattended with permissions bypassed, which makes this the only
real boundary: prompt rules can be forgotten or reasoned around, a check that
runs before the tool call cannot. Rules live in `.claude/deny.json` so they can
be edited without touching code.

Nothing here is specific to one agent. Claude Code wires it as a `PreToolUse`
hook (`.claude/hooks/guard.py`); any other harness can call `bin/guard check`
from whatever pre-execution hook it has, and a harness with no hooks at all can
at least run it in CI or in review. What must not happen is a second copy of
these rules living somewhere else.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = ROOT / ".claude" / "deny.json"
LOG_FILE = ROOT / ".runtime" / "guard.log"

# Used when deny.json cannot be read: fail closed on the things that cannot be
# undone rather than falling open because a file is missing.
FALLBACK_RULES = [{
    "name": "fallback",
    "tools": ["*"],
    "pattern": r"git\s+push[^|;&]*--force|drop\s+(table|database)|sendmail",
    "reason": "deny.json is unreadable; falling back to the minimal deny list.",
}]


def path_pattern(text: str) -> str:
    """A regex matching a filesystem path as it is actually typed.

    A path containing a space reaches the shell as `A\\ B`, `"A B"` or `'A B'`.
    A rule that knows only the plain form is not a boundary - and this board is
    routinely kept in a directory whose name has a space in it.
    """
    # Character by character: re.escape() escapes the space itself, so escaping
    # first and substituting after leaves a stray backslash and a broken class.
    return "".join(r"""[\s\\'"]+""" if ch.isspace() else re.escape(ch) for ch in text)


def placeholders() -> dict[str, str]:
    """How a rule may spell this board's own path.

    The same directory can be named absolutely, through `$HOME`, or by its own
    name in a relative path. A rule that matches one of those is not a boundary.
    """
    home = str(Path.home())
    root = str(ROOT)
    return {
        "{{ROOT}}": path_pattern(root),
        "{{ROOT_TILDE}}": path_pattern("~" + root[len(home):] if root.startswith(home) else root),
        "{{ROOT_NAME}}": path_pattern(ROOT.name),
    }


def load_rules() -> list[dict]:
    try:
        rules = json.loads(RULES_FILE.read_text(encoding="utf-8")).get("rules", [])
    except (OSError, json.JSONDecodeError):
        rules = [dict(rule) for rule in FALLBACK_RULES]
    expansions = placeholders()
    for rule in rules:
        pattern = rule.get("pattern", "")
        for placeholder, value in expansions.items():
            pattern = pattern.replace(placeholder, value)
        rule["pattern"] = pattern
    return rules


# Every harness names its tools differently: Claude Code's shell tool is `Bash`,
# opencode's is `bash`, and a rule that names one of them is a boundary for one
# agent and a suggestion for the other. Rules are written once, in Claude Code's
# vocabulary, and every other harness's names are folded onto it here - so adding
# a second agent does not mean a second copy of the deny list. Anything not named
# here passes through unchanged, which is what MCP tool names rely on.
TOOL_ALIASES = {
    "bash": "Bash",
    "shell": "Bash",
    "run_command": "Bash",
    "edit": "Edit",
    "patch": "Edit",
    "write": "Write",
    "read": "Read",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "glob": "Glob",
    "grep": "Grep",
    "list": "LS",
    "task": "Task",
}


def canonical_tool(tool_name: str) -> str:
    """The name the rules know this tool by, whichever harness is calling."""
    name = (tool_name or "").strip()
    return TOOL_ALIASES.get(name, name)


def subject_for(tool_name: str, tool_input: dict, cwd: str = "") -> str:
    """What the rules are matched against.

    A shell command is prefixed with the directory it runs in, because the most
    likely spelling of a destructive command is the shortest one: the wrapper
    starts the agent with the board as its working directory, so removing
    `projects` needs no path at all. Rules see the cwd on its own first line,
    before the command, so a path-matching rule anchored after the command name
    cannot match it by accident.
    """
    if canonical_tool(tool_name) == "Bash":
        return f"cwd={cwd}\n{tool_input.get('command', '')}"
    return json.dumps(tool_input, ensure_ascii=False)


def tool_matches(patterns, tool_name: str) -> bool:
    for pattern in patterns:
        if pattern == "*" or pattern == tool_name:
            return True
        try:
            if re.fullmatch(pattern, tool_name):
                return True
        except re.error:
            continue
    return False


def rule_applies(rule: dict) -> bool:
    """Whether a rule binds this caller at all.

    A rule may name an environment variable in `env`, and then binds only when
    that variable is set. One boundary needs it: the board's framework must be
    unwritable from inside a loop run and freely editable by the human whose job
    is to change it, and both are the same files in the same checkout. The
    wrapper marks its own runs; nothing else sets the variable, so an interactive
    session never sees the rule.
    """
    required = rule.get("env")
    return not required or bool(os.environ.get(required))


def check(tool_name: str, subject: str) -> dict | None:
    """The rule that forbids this call, or None if nothing does."""
    if not subject:
        return None
    tool_name = canonical_tool(tool_name)
    for rule in load_rules():
        if not rule_applies(rule):
            continue
        if not tool_matches(rule.get("tools", ["*"]), tool_name):
            continue
        try:
            # re.M so that a rule anchored with `$` matches at a line end, not
            # only at the end of the whole command.
            if re.search(rule["pattern"], subject, re.I | re.S | re.M):
                return rule
        except re.error:
            continue
    return None


def record(rule: dict, tool_name: str, subject: str) -> None:
    """Every denial is written down; a boundary nobody can audit is a rumour."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{rule['name']}\t{tool_name}\t{subject[:400]}\n")
    except OSError:
        pass
