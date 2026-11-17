#!/usr/bin/env python3
"""Time the board scans that every command pays for, on a synthetic board.

Not a unit test (the name deliberately does not match `test*.py`, so
`unittest discover tests` skips it). It exists because "the board feels slow"
is not a claim anyone can check a year later: the numbers it prints are the
ones quoted in PT-007, and rerunning it is how the next person finds out
whether a change made things worse.

    python3 tests/bench_scan.py [--tasks 500] [--repeat 3]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent

# A real task file is mostly log. Scans only ever want the frontmatter, so the
# body size is exactly what separates "reads metadata" from "reads everything".
BODY_FILLER = ("\n".join(
    f"- 2026-01-{1 + (i % 28):02d}T0{i % 10}:00:00Z agent: "
    f"log line {i} recording a decision, a command that mattered and a dead end."
    for i in range(120)))


def build_board(root: Path) -> None:
    for item in ("bin", "lib", "prompts"):
        shutil.copytree(SOURCE_ROOT / item, root / item,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    (root / ".runtime").mkdir()
    (root / "archive").mkdir()
    (root / "projects").mkdir()
    (root / "config.json").write_text(json.dumps({
        "notify_macos": False, "notify_file": False, "git_autocommit": False,
    }), encoding="utf-8")


def seed(root: Path, count: int, projects: int = 4) -> None:
    """Write the task files directly: `bin/task new` would dominate the timing."""
    statuses = ["ready", "backlog", "done", "blocked"]
    for p in range(projects):
        name = f"proj{p}"
        pdir = root / "projects" / name
        (pdir / "tasks").mkdir(parents=True)
        for status in ("backlog", "ready", "in-progress", "blocked", "review",
                       "needs-human", "failed", "done", "cancelled"):
            (pdir / "tasks" / status).mkdir()
        pdir.joinpath("project.md").write_text(
            f"---\nname: {name}\nprefix: P{p}\n---\n\n## Context\n\nA synthetic project.\n",
            encoding="utf-8")
    per = count // projects
    for p in range(projects):
        name = f"proj{p}"
        for i in range(per):
            tid = f"P{p}-{i + 1:03d}"
            status = statuses[i % len(statuses)]
            # Every fourth task depends on an earlier one, so the dependency
            # resolution paths (doctor, blocking_reasons) are actually exercised.
            dep = f"[P{p}-{i:03d}]" if i and i % 4 == 0 else "[]"
            path = root / "projects" / name / "tasks" / status / f"{tid.lower()}-synthetic.md"
            path.write_text(
                f"---\nid: {tid}\ntitle: Synthetic task {i} for {name}\n"
                f"project: {name}\nstatus: {status}\ntype: chore\nsize: S\n"
                f"priority: P{i % 4}\nowner: agent\ncreated: 2026-01-01T00:00:00Z\n"
                f"updated: 2026-01-01T00:00:00Z\ndepends_on: {dep}\n"
                f"attempts: 0\nmax_attempts: 3\ntags: [bench, synthetic]\n---\n\n"
                f"## Goal\n\nKeep the scan honest.\n\n"
                f"## Acceptance criteria\n\n- [ ] The scan stays cheap\n\n"
                f"## Log\n{BODY_FILLER}\n",
                encoding="utf-8")


def timeit(root: Path, args: list[str], repeat: int) -> float:
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = subprocess.run([sys.executable, str(root / "bin" / "task"), *args],
                                capture_output=True, text=True)
        elapsed = time.perf_counter() - start
        if result.returncode not in (0, 1):
            raise SystemExit(f"{' '.join(args)} failed: {result.stderr or result.stdout}")
        best = elapsed if best is None else min(best, elapsed)
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=500)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "a board"
        root.mkdir()
        build_board(root)
        seed(root, args.tasks)
        total_bytes = sum(p.stat().st_size
                          for p in root.joinpath("projects").rglob("*.md"))
        commands = [
            ["list"], ["list", "--all"], ["next"], ["doctor"],
            ["show", "P0-013"], ["lint"],
            # First one pays for the whole index; the ones after it pay for the
            # mtime check. Both numbers matter, which is why both are timed.
            ["index", "--rebuild"], ["search", "scan honest"],
            ["search", "scan honest", "--no-index"],
        ]
        results = {}
        for command in commands:
            results[" ".join(command)] = round(timeit(root, command, args.repeat) * 1000, 1)
        if args.json:
            print(json.dumps({"tasks": args.tasks, "bytes": total_bytes,
                              "ms": results}, indent=2))
        else:
            print(f"{args.tasks} tasks, {total_bytes / 1024:.0f} KiB of task files, "
                  f"best of {args.repeat}")
            for command, ms in results.items():
                print(f"  bin/task {command:<28} {ms:>8.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
