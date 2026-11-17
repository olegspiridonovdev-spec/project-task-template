#!/usr/bin/env python3
"""Claude Code's PreToolUse entry point into the board's hard limits.

The rules and the checking live in `lib/guardlib.py` and are exposed to every
harness as `bin/guard`; this file only adapts Claude Code's hook protocol to
them, so that there is one boundary rather than one per agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

import guardlib as gl  # noqa: E402


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name", "")
    subject = gl.subject_for(tool_name, payload.get("tool_input", {}) or {},
                             str(payload.get("cwd") or ""))
    rule = gl.check(tool_name, subject)
    if rule is None:
        return 0

    gl.record(rule, tool_name, subject)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[{rule['name']}] {rule['reason']}",
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
