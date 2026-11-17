#!/usr/bin/env python3
"""Tests for the invariants the loop depends on.

Run with `python3 -m unittest discover tests` (stdlib only, no packages).

Each test builds a throwaway copy of the board in a temp directory and drives
it through the real `bin/task` CLI, so what is tested is the system as it
actually runs rather than a re-implementation of it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def build_board(tmp: Path) -> Path:
    """A board copy with no projects, no runtime state and no notifications.

    The directory name contains a space on purpose: the real board is usually kept
    in one, and a path with a space is where quoting bugs live.
    """
    root = tmp / "a board"
    root.mkdir(parents=True)
    for item in ("bin", "lib", "prompts", "scheduler", ".claude", ".opencode"):
        # __pycache__ is skipped: the source tree gitignores it, this board does
        # not, and the mirror tests then refuse to run because a .pyc looks like
        # uncommitted work. Copying build output into a fixture never helps, and
        # opencode's own node_modules under .opencode is the same kind of thing.
        shutil.copytree(SOURCE_ROOT / item, root / item,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules",
                                                      "package*.json", "bun.lock"))
    for item in ("AGENTS.md", "opencode.json"):
        shutil.copy(SOURCE_ROOT / item, root / item)
    shutil.copy(SOURCE_ROOT / "CLAUDE.md", root / "CLAUDE.md")
    (root / ".runtime").mkdir()
    (root / "archive").mkdir()
    (root / "projects").mkdir()
    (root / "config.json").write_text(json.dumps({
        # Deliberately the *old* name, as with the refund cap below: a board
        # written before this could raise a banner on anything but a Mac still
        # has its setting honoured, and every test here proves it in passing.
        "notify_macos": False,
        "notify_file": False,
        "git_autocommit": False,
        "retry_backoff_sec": 600,
        "retry_backoff_max_sec": 21600,
        # Deliberately the *old* name for the refund cap. Every test that leans
        # on the cap therefore also proves a board written before the infra
        # class existed still has its setting honoured.
        "usage_limit_max_refunds": 3,
    }), encoding="utf-8")
    return root


class BoardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_board(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def task(self, *args: str) -> str:
        result = subprocess.run([sys.executable, str(self.root / "bin" / "task"), *args],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def new_project(self, name: str = "demo", prefix: str = "DM") -> None:
        self.task("project-new", name, "--repo", "/tmp/demo", "--prefix", prefix)

    def new_task(self, title: str, *args: str) -> str:
        """A fixture task, exempt from lint.

        `--force` is here so that every test about scheduling can keep writing
        one-word titles and no acceptance criteria: those tests are about
        dependencies, leases and backoff, and making each of them author a
        well-formed task would hide what they are actually asserting. Lint's own
        tests call the CLI without it, which is the only place it should matter.
        """
        out = self.task("new", "--project", "demo", "--title", title, "--force", *args)
        return out.split()[0]

    def summary(self, task_id: str) -> dict:
        return json.loads(self.task("show", task_id, "--json"))

    def load_tasklib(self):
        """The temp board's own tasklib, for the paths that run below the CLI.

        Imported against this board rather than the source tree, because the
        module resolves ROOT and reads config.json at import time.
        """
        sys.path.insert(0, str(self.root / "lib"))
        self.addCleanup(sys.path.remove, str(self.root / "lib"))
        sys.modules.pop("tasklib", None)
        self.addCleanup(lambda: sys.modules.pop("tasklib", None))
        loader = SourceFileLoader("tasklib", str(self.root / "lib" / "tasklib.py"))
        spec = importlib.util.spec_from_loader("tasklib", loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["tasklib"] = module
        loader.exec_module(module)
        return module

    def close_and_age(self, task_id: str, status: str = "done") -> None:
        self.task("move", task_id, "in-progress", "--force", "-m", "test")
        # `-d` because closing to done or review demands a journal entry; harmless
        # on the statuses that only accept one.
        self.task("move", task_id, status, "-m", "test",
                  "-d", "a fixture closing a fixture, which decided nothing at all")
        path = self.root / self.summary(task_id)["path"]
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(self.summary(task_id)["updated"], "2020-01-01T00:00:00Z")
                        .replace("closed: ", "closed: 2020-01-01T00:00:00Z #", 1),
                        encoding="utf-8")
        # rewrite `closed` cleanly rather than relying on the replace above
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            lines.append("closed: 2020-01-01T00:00:00Z" if line.startswith("closed:") else line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestIdentity(BoardTestCase):
    def test_archived_ids_are_never_reissued(self) -> None:
        """An archived id still names a real piece of work, in logs and commits."""
        self.new_project()
        first = self.new_task("First")
        self.close_and_age(first)
        self.task("archive", "--days", "1")
        self.assertEqual(self.task("list"), "no tasks\n")

        second = self.new_task("Second")
        self.assertNotEqual(second, first)
        self.assertEqual(second, "DM-002")


class TestDependencies(BoardTestCase):
    def test_dependency_on_an_archived_task_does_not_block(self) -> None:
        """Archiving a finished dependency must not deadlock its dependants."""
        self.new_project()
        first = self.new_task("First")
        second = self.new_task("Second", "--ready", "--after", first)
        self.close_and_age(first)
        self.task("archive", "--days", "1")

        rows = json.loads(self.task("list", "--json"))
        blocked = {row["id"]: row["blocked_by"] for row in rows}
        self.assertEqual(blocked[second], [])
        self.assertIn(second, self.task("next"))

    def test_dependency_that_never_existed_still_blocks(self) -> None:
        self.new_project()
        task_id = self.new_task("Second", "--ready", "--after", "DM-999")
        rows = json.loads(self.task("list", "--json"))
        reasons = [row["blocked_by"] for row in rows if row["id"] == task_id][0]
        self.assertEqual(reasons, ["unknown dependency DM-999"])


class TestLint(BoardTestCase):
    """`ready` is a promise: whatever is in it, an agent can act on cold.

    Before lint, `task new --project x --title "Fix" --ready` put a task with an
    empty `- [ ]` in front of the loop, and the first thing the agent had to
    decide was what done meant. Every test here names one way that happened.
    """

    GOOD_BODY = ("## Goal\n\nDeliveries that fail are retried.\n\n"
                 "## Acceptance criteria\n\n"
                 "- [ ] a failed delivery is retried three times\n\n"
                 "## Context\n\nThe webhook sender drops on the first 5xx.\n")

    def lint(self, *args: str):
        """Run lint without the assertion that it succeeded; it is meant to fail."""
        return subprocess.run([sys.executable, str(self.root / "bin" / "task"), "lint", *args],
                              capture_output=True, text=True)

    def new_raw(self, *args: str):
        """`task new` with no --force, so the ready boundary is really tested."""
        return subprocess.run([sys.executable, str(self.root / "bin" / "task"), "new",
                               "--project", "demo", *args], capture_output=True, text=True)

    def fit_task(self, title: str = "Retry failed deliveries with backoff",
                 *args: str) -> str:
        result = self.new_raw("--title", title, "--type", "bug",
                              "--body", self.GOOD_BODY, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()[0]

    # --- the task that passes ------------------------------------------------

    def test_a_well_formed_task_lints_clean_and_goes_straight_to_ready(self) -> None:
        self.new_project()
        task_id = self.fit_task("Retry failed deliveries with backoff",
                                "--size", "M", "--due", "2030-01-01", "--ready")
        self.assertEqual(self.summary(task_id)["status"], "ready")
        result = self.lint(task_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1/1", result.stdout)

    def test_the_schema_survives_a_round_trip_through_the_file(self) -> None:
        """type/size/due are read back from the file, not just accepted."""
        self.new_project()
        task_id = self.fit_task("Retry failed deliveries with backoff",
                                "--size", "L", "--due", "2030-01-01")
        summary = self.summary(task_id)
        self.assertEqual((summary["type"], summary["size"], summary["due"]),
                         ("bug", "L", "2030-01-01"))
        rows = {row["id"]: row for row in json.loads(self.task("list", "--json"))}
        self.assertEqual(rows[task_id]["type"], "bug")
        self.assertEqual(rows[task_id]["size"], "L")
        self.assertEqual(rows[task_id]["due"], "2030-01-01")

    # --- one test per check --------------------------------------------------

    def test_a_placeholder_title_names_no_outcome(self) -> None:
        self.new_project()
        result = self.new_raw("--title", "TODO", "--type", "chore",
                              "--body", self.GOOD_BODY)
        task_id = result.stdout.split()[0]
        self.assertIn("placeholder", "\n".join(self.lint(task_id).stdout.splitlines()))

    def test_a_title_too_short_to_be_an_outcome_is_reported(self) -> None:
        self.new_project()
        result = self.new_raw("--title", "Webhooks", "--type", "chore",
                              "--body", self.GOOD_BODY)
        task_id = result.stdout.split()[0]
        self.assertIn("too short to name an outcome", self.lint(task_id).stdout)

    def test_the_empty_checkbox_task_new_writes_is_not_a_criterion(self) -> None:
        """The exact shape lint exists for: a template nobody filled in."""
        self.new_project()
        result = self.new_raw("--title", "Rewrite the delivery scheduler",
                              "--type", "feature")
        task_id = result.stdout.split()[0]
        self.assertIn("no acceptance criterion", self.lint(task_id).stdout)

    def test_a_project_with_no_repo_path_cannot_be_worked(self) -> None:
        self.task("project-new", "paperwork", "--prefix", "PW")
        result = subprocess.run([sys.executable, str(self.root / "bin" / "task"), "new",
                                 "--project", "paperwork", "--type", "ops",
                                 "--title", "Write the deployment runbook",
                                 "--body", self.GOOD_BODY],
                                capture_output=True, text=True)
        task_id = result.stdout.split()[0]
        self.assertIn("no repo_path", self.lint(task_id).stdout)

    def test_a_task_with_no_type_is_reported(self) -> None:
        self.new_project()
        result = self.new_raw("--title", "Rewrite the delivery scheduler",
                              "--body", self.GOOD_BODY)
        task_id = result.stdout.split()[0]
        self.assertIn("type is not set", self.lint(task_id).stdout)

    def test_a_value_outside_the_vocabulary_is_reported(self) -> None:
        """Hand-edited frontmatter: the CLI's choices cannot catch this one."""
        self.new_project()
        task_id = self.fit_task()
        path = self.root / self.summary(task_id)["path"]
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("type:"):
                line = "type: defect"
            elif line.startswith("size:"):
                line = "size: XL"
            elif line.startswith("due:"):
                line = "due: next tuesday"
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        out = self.lint(task_id).stdout
        self.assertIn("type 'defect' is not one of", out)
        self.assertIn("size 'XL' is not one of", out)
        self.assertIn("due 'next tuesday' is not a date", out)

    def test_a_dependency_that_resolves_to_nothing_is_reported(self) -> None:
        self.new_project()
        task_id = self.fit_task("Retry failed deliveries with backoff",
                                "--after", "DM-999")
        self.assertIn("depends_on DM-999 does not resolve", self.lint(task_id).stdout)

    # --- the boundary --------------------------------------------------------

    def test_lint_exits_non_zero_when_a_task_is_not_fit(self) -> None:
        self.new_project()
        task_id = self.new_task("Vague")
        self.assertEqual(self.lint(task_id).returncode, 1)
        self.assertEqual(self.lint(self.fit_task()).returncode, 0)

    def test_new_ready_keeps_an_unfit_task_in_backlog(self) -> None:
        self.new_project()
        result = self.new_raw("--title", "Fix", "--ready")
        self.assertEqual(result.returncode, 2, result.stdout)
        task_id = result.stdout.split()[0]
        self.assertEqual(self.summary(task_id)["status"], "backlog")
        self.assertIn("kept " + task_id + " in backlog", result.stderr)
        # the body is kept, so nothing has to be retyped to fix it
        self.assertIn("Acceptance criteria",
                      (self.root / self.summary(task_id)["path"]).read_text(encoding="utf-8"))

    def test_promoting_an_unfit_task_is_refused_by_both_doors(self) -> None:
        self.new_project()
        task_id = self.new_task("Vague")
        for command in (["ready", task_id], ["move", task_id, "ready"]):
            result = subprocess.run([sys.executable, str(self.root / "bin" / "task"),
                                     *command, "-m", "go"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("not fit to be ready", result.stderr)
            self.assertEqual(self.summary(task_id)["status"], "backlog")

    def test_force_is_the_deliberate_override(self) -> None:
        """Lint is a check on the author, not a lock. Saying so must still work."""
        self.new_project()
        task_id = self.new_task("Vague")
        self.task("ready", task_id, "-m", "I know", "--force")
        self.assertEqual(self.summary(task_id)["status"], "ready")
        # and the board still says what is wrong with it
        self.assertEqual(self.lint().returncode, 1)

    def test_lint_with_no_id_covers_the_column_ready_promises_about(self) -> None:
        self.new_project()
        fit = self.fit_task("Retry failed deliveries with backoff", "--ready")
        unfit = self.new_task("Vague", "--ready")
        result = self.lint()
        self.assertEqual(result.returncode, 1)
        self.assertIn(unfit, result.stdout)
        self.assertIn("1/2", result.stdout)
        # a clean task is not listed; only what needs fixing is
        self.assertNotIn(fit + " ", result.stdout)

    def test_lint_json_reports_every_task_it_looked_at(self) -> None:
        self.new_project()
        self.fit_task("Retry failed deliveries with backoff", "--ready")
        self.new_task("Vague", "--ready")
        result = self.lint("--json")
        rows = {row["id"]: row["problems"] for row in json.loads(result.stdout)}
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for problems in rows.values() if problems), 1)

    def test_the_wrapper_can_still_requeue_a_task_lint_would_reject(self) -> None:
        """Recovery goes through the library, under the CLI. It must stay open.

        Otherwise a task that was forced ready, claimed, and then abandoned by a
        dead run could never be put back, and would sit in-progress with nobody
        behind it - the one thing `doctor` calls an orphan.
        """
        self.new_project()
        task_id = self.new_task("Vague", "--ready")
        self.task("claim", task_id, "--generation", "1")
        task = self.load_tasklib().find_task(task_id)
        task.move("ready", "reclaimed after holder disappeared", actor="tick")
        self.assertEqual(self.summary(task_id)["status"], "ready")


class TestArchive(BoardTestCase):
    def test_cancelled_tasks_are_archived_too(self) -> None:
        self.new_project()
        task_id = self.new_task("Dropped")
        self.task("cancel", task_id, "-m", "not needed")
        path = self.root / self.summary(task_id)["path"]
        lines = ["closed: 2020-01-01T00:00:00Z" if line.startswith("closed:") else line
                 for line in path.read_text(encoding="utf-8").splitlines()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.task("archive", "--days", "1")
        self.assertEqual(list((self.root / "archive").rglob("*.md"))[0].stem.split("-")[0], "DM")


class TestCheapScans(BoardTestCase):
    """Reading the board must cost the number of tasks, not the size of their logs.

    Every one of these was a real cost: `doctor` resolved each dependency with
    `find_task`, which is a walk of the board, so checking a board cost the
    square of its size. The tests below pin the shapes that made that possible.
    """

    def test_a_task_body_is_read_only_when_it_is_asked_for(self) -> None:
        self.new_project()
        self.new_task("First", "--body", "a body nobody scanning the board wants")
        tl = self.load_tasklib()

        task = next(iter(tl.iter_tasks()))
        self.assertIsNone(task._body, "constructing a Task read the body")
        self.assertEqual(task.title, "First")
        self.assertIn("a body nobody scanning", task.body)

    def test_find_task_still_finds_a_file_renamed_by_hand(self) -> None:
        """The filename is a hint. The frontmatter is the answer."""
        self.new_project()
        task_id = self.new_task("First")
        path = self.root / self.summary(task_id)["path"]
        path.rename(path.parent / "nothing-to-do-with-the-id.md")

        self.assertIn("First", self.task("show", task_id))

    def test_next_id_skips_an_id_a_renamed_file_still_holds(self) -> None:
        """Allocating from filenames alone would hand out a live id twice."""
        self.new_project()
        first = self.new_task("First")
        path = self.root / self.summary(first)["path"]
        path.rename(path.parent / "renamed.md")

        self.assertEqual(self.new_task("Second"), "DM-002")

    def test_doctor_accepts_a_dependency_that_has_been_archived(self) -> None:
        """`doctor` and `blocking_reasons` have to agree on what resolves.

        They did not: scheduling treated an archived dependency as closed while
        doctor called it unknown, so a healthy board reported a problem that
        nothing could fix except deleting the reference.
        """
        self.new_project()
        first = self.new_task("First")
        self.new_task("Second", "--after", first)
        self.close_and_age(first)
        self.task("archive", "--days", "1")

        result = subprocess.run([sys.executable, str(self.root / "bin" / "task"), "doctor"],
                                capture_output=True, text=True)
        self.assertNotIn("unknown dependency", result.stdout)

    def test_doctor_still_reports_a_dependency_that_never_existed(self) -> None:
        self.new_project()
        self.new_task("Second", "--after", "DM-999")
        result = subprocess.run([sys.executable, str(self.root / "bin" / "task"), "doctor"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown dependency DM-999", result.stdout)


class TestSearch(BoardTestCase):
    """Search exists so that "have we already done this?" has an answer.

    The index behind it is derived and disposable, and every test here is about
    one way it is allowed to be wrong: stale, missing, damaged, or holding a
    task that no longer exists. In all of them the files win.
    """

    def search(self, *args: str) -> list[dict]:
        result = subprocess.run(
            [sys.executable, str(self.root / "bin" / "task"), "search", *args, "--json"],
            capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return json.loads(result.stdout)["results"]

    def seed(self) -> None:
        self.new_project()
        self.new_task("Retry failed webhook deliveries",
                      "--body", "The sender drops a delivery on the first 5xx.",
                      "--tags", "webhooks,reliability")
        self.new_task("Rotate the signing keys",
                      "--body", "Keys have never been rotated.", "--tags", "security")

    def test_a_word_in_the_body_finds_the_task(self) -> None:
        self.seed()
        found = self.search("5xx")
        self.assertEqual([r["id"] for r in found], ["DM-001"])

    def test_every_word_has_to_match(self) -> None:
        self.seed()
        self.assertEqual(self.search("webhook rotated"), [])

    def test_a_slug_is_a_query_and_not_a_syntax_error(self) -> None:
        """What an agent pastes in is an id or a slug, full of FTS5 operators."""
        self.seed()
        self.assertEqual([r["id"] for r in self.search("retry-failed-webhook")], ["DM-001"])
        self.assertEqual([r["id"] for r in self.search("DM-002")], ["DM-002"])

    def test_archived_work_is_still_findable(self) -> None:
        """The whole point: finished work is what leaves the board."""
        self.seed()
        self.close_and_age("DM-001")
        self.task("archive", "--days", "1")

        found = self.search("webhook")
        self.assertEqual([r["id"] for r in found], ["DM-001"])
        self.assertEqual(self.search("webhook", "--no-archived"), [])

    def test_the_index_follows_an_edit(self) -> None:
        self.seed()
        self.search("webhook")  # builds the index
        self.task("log", "DM-002", "-m", "tried the elephant approach")

        self.assertEqual([r["id"] for r in self.search("elephant")], ["DM-002"])

    def test_the_index_forgets_a_task_whose_file_is_gone(self) -> None:
        """An index that answers with work nobody can open is worse than none."""
        self.seed()
        self.search("webhook")
        (self.root / self.summary("DM-001")["path"]).unlink()

        self.assertEqual(self.search("webhook"), [])

    def test_deleting_the_index_is_safe(self) -> None:
        self.seed()
        self.search("webhook")
        db = self.root / ".runtime" / "index.db"
        self.assertTrue(db.exists())
        db.unlink()

        self.assertEqual([r["id"] for r in self.search("webhook")], ["DM-001"])
        self.assertTrue(db.exists(), "the index was not rebuilt")

    def test_a_damaged_index_answers_from_the_files_and_then_heals(self) -> None:
        self.seed()
        db = self.root / ".runtime" / "index.db"
        self.search("webhook")
        db.write_text("this is not a database", encoding="utf-8")

        self.assertEqual([r["id"] for r in self.search("webhook")], ["DM-001"])
        self.assertEqual([r["id"] for r in self.search("webhook")], ["DM-001"])

    def test_rebuild_reconstructs_the_index_from_the_files_alone(self) -> None:
        self.seed()
        db = self.root / ".runtime" / "index.db"
        self.search("webhook")
        before = db.stat().st_size
        self.assertIn("2 document(s) indexed", self.task("index", "--rebuild"))
        self.assertGreater(db.stat().st_size, 0)
        self.assertGreater(before, 0)

    def test_reading_the_files_gives_the_same_answer_as_the_index(self) -> None:
        """`--no-index` is the definition search only ever approximates."""
        self.seed()
        self.assertEqual([r["id"] for r in self.search("webhook")],
                         [r["id"] for r in self.search("webhook", "--no-index")])

    def test_the_index_is_never_committed(self) -> None:
        ignored = (SOURCE_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("index.db", ignored)


class TickTestCase(BoardTestCase):
    """A board whose `bin/tick` is imported, so its logic can be called directly.

    The module reads config.json at import, so anything that changes the config
    has to do it before `load_tick()`.
    """

    def load_tick(self) -> None:
        sys.path.insert(0, str(self.root / "lib"))
        self.addCleanup(sys.path.remove, str(self.root / "lib"))
        for name in ("tasklib", "tick"):
            sys.modules.pop(name, None)
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in ("tasklib", "tick")])
        loader = SourceFileLoader("tick", str(self.root / "bin" / "tick"))
        spec = importlib.util.spec_from_loader("tick", loader)
        self.tick = importlib.util.module_from_spec(spec)
        sys.modules["tick"] = self.tick
        loader.exec_module(self.tick)
        self.tl = sys.modules["tasklib"]


class TestTickBehaviour(TickTestCase):
    """Tests that drive bin/tick's own logic against the temp board."""

    def setUp(self) -> None:
        super().setUp()
        self.load_tick()

    def claim(self, task_id: str, generation: int = 1) -> None:
        self.task("claim", task_id, "--generation", str(generation))

    # -- usage limit detection ------------------------------------------------

    def test_limit_is_detected_from_the_cli_stderr(self) -> None:
        until = self.tick.detect_usage_limit(
            {"stderr": "Claude AI usage limit reached|%d\n" % int(time.time() + 1800),
             "result": None}, code=1)
        self.assertIsNotNone(until)
        self.assertAlmostEqual(until - time.time(), 1800, delta=30)

    def test_limit_is_detected_from_a_failed_result_event(self) -> None:
        payload = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "API Error: rate_limit_error"}
        self.assertIsNotNone(self.tick.detect_usage_limit({"stderr": "", "result": payload}, code=1))

    def test_transcript_echo_is_not_evidence(self) -> None:
        """The handler must not trigger on the words it writes itself.

        The requeue note, the NOTIFY.md line and this repository's own README all
        contain limit wording; the agent reads those files and the reader echoes
        them back on stdout. Matching them would make one real limit teach the
        loop to pause forever.
        """
        echoes = [
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result",
                 "content": "- tick: usage limit reached; requeued without burning an attempt"}]}}),
            "- **2026-01-01T00:00:00Z** `warn` **Usage limit reached** - Loop paused until 11:00.",
            "| Plan usage limit reached | limit phrases in the agent's output |",
            "I will add a retry when the vendor returns rate_limit_error.",
        ]
        for text in echoes:
            with self.subTest(text=text[:40]):
                # transcript arrives as a successful result event, never as stderr
                payload = {"type": "result", "subtype": "success", "is_error": False,
                           "result": text}
                self.assertIsNone(self.tick.detect_usage_limit({"stderr": "", "result": payload},
                                                               code=0))
                self.assertIsNone(self.tick.detect_usage_limit({"stderr": "", "result": payload},
                                                               code=1))

    def test_a_successful_run_is_never_a_limit(self) -> None:
        self.assertIsNone(self.tick.detect_usage_limit(
            {"stderr": "usage limit reached", "result": None}, code=0))

    def test_the_reworded_session_limit_is_detected(self) -> None:
        """The wording the CLI actually used on 2026-08-26.

        No pattern matched it, so a whole limited window was recorded as task
        failures and seventeen healthy tasks were parked in needs-human with
        their attempts spent. Kept as prose *and* as the two structured signals
        below, because the prose is the part that drifts.
        """
        payload = {"type": "result", "subtype": "success", "is_error": True,
                   "result": "You've hit your session limit \u00b7 resets 1:10pm "
                             "(America/New_York)"}
        self.assertIsNotNone(self.tick.detect_usage_limit(
            {"stderr": "", "result": payload}, code=1))

    def test_a_429_is_a_limit_whatever_the_wording(self) -> None:
        payload = {"type": "result", "subtype": "success", "is_error": True,
                   "api_error_status": 429, "result": "some wording nobody has seen yet"}
        self.assertIsNotNone(self.tick.detect_usage_limit(
            {"stderr": "", "result": payload}, code=1))

    def test_a_rejected_rate_limit_event_carries_its_own_reset(self) -> None:
        resets = int(time.time() + 1800)
        event = {"type": "rate_limit_event",
                 "rate_limit_info": {"status": "rejected", "resetsAt": resets,
                                     "rateLimitType": "five_hour"}}
        until = self.tick.detect_usage_limit(
            {"stderr": "", "result": None, "rate_limit": event}, code=1)
        self.assertIsNotNone(until)
        self.assertAlmostEqual(until - time.time(), 1800, delta=30)

    def test_an_allowed_rate_limit_event_is_not_a_limit(self) -> None:
        """The CLI reports the window on healthy runs too; only 'rejected' counts."""
        event = {"type": "rate_limit_event",
                 "rate_limit_info": {"status": "allowed", "resetsAt": int(time.time() + 1800)}}
        self.assertIsNone(self.tick.detect_usage_limit(
            {"stderr": "", "result": None, "rate_limit": event}, code=1))

    def test_an_implausible_reset_falls_back_to_the_backoff(self) -> None:
        far = int(time.time() + 400 * 24 * 3600)
        until = self.tick.detect_usage_limit(
            {"stderr": f"usage limit reached|{far}", "result": None}, code=1)
        self.assertLess(until - time.time(), 2 * 3600)

    # -- settle_task ----------------------------------------------------------

    # -- the infra outcome class ---------------------------------------------

    def infra_evidence_cases(self) -> list[tuple[str, dict, str | None]]:
        """One cause per category, as the tool that hit it would report it."""
        return [
            ("usage limit",
             {"stderr": "Claude AI usage limit reached|%d\n" % int(time.time() + 1800)},
             None),
            ("network",
             {"stderr": "request to https://api.anthropic.com failed, reason: "
                        "getaddrinfo EAI_AGAIN api.anthropic.com\n"},
             None),
            ("credentials",
             {"stderr": '{"type":"error","error":{"type":"authentication_error",'
                        '"message":"invalid api key"}}\n'},
             None),
            ("disk",
             {"stderr": "fatal: cannot write index file: "
                        "No space left on device (ENOSPC)\n"},
             None),
            # Not text at all: the wrapper knows where the working copy is meant
            # to be, so an unmounted volume is a fact it can check rather than
            # a phrase it has to recognise.
            ("workspace", {"stderr": "some unremarkable failure\n"},
             "/nonexistent/volume/repo"),
        ]

    def test_every_category_is_recognised_from_cli_evidence(self) -> None:
        for cause, evidence, repo_path in self.infra_evidence_cases():
            with self.subTest(cause=cause):
                outcome = self.tick.detect_infra({"stderr": evidence.get("stderr", ""),
                                                  "result": None},
                                                 code=1, repo_path=repo_path)
                self.assertIsNotNone(outcome, "this run said nothing about the task")
                self.assertEqual(outcome.cause, cause)

    def test_a_successful_run_is_never_an_infra_outcome(self) -> None:
        """Whatever the machine printed, a run that finished did the work."""
        for cause, evidence, repo_path in self.infra_evidence_cases():
            with self.subTest(cause=cause):
                self.assertIsNone(
                    self.tick.detect_infra({"stderr": evidence.get("stderr", ""),
                                            "result": None},
                                           code=0, repo_path=repo_path))

    def test_infra_evidence_is_never_the_transcript(self) -> None:
        """The class inherits PT-001's rule: only the CLI may name a cause.

        Every string here is something an agent could plausibly print while
        working on a task *about* infrastructure. Arriving on stdout as a result
        event, it is transcript, and transcript is not evidence however exactly
        it matches.
        """
        transcript = [
            "The deploy fails with getaddrinfo EAI_AGAIN; I will add a retry.",
            "Reproduced: the API returns authentication_error when the key is unset.",
            "The CI box died with ENOSPC, no space left on device.",
            "- tick: plan limit hit; attempt refunded and task requeued",
        ]
        for text in transcript:
            with self.subTest(text=text[:40]):
                payload = {"type": "result", "subtype": "success",
                           "is_error": False, "result": text}
                self.assertIsNone(self.tick.detect_infra({"stderr": "", "result": payload},
                                                         code=1))

    def test_the_notes_the_handler_writes_are_not_evidence(self) -> None:
        """No cause may recognise its own wording.

        Every note lands in the task log, in `last_error` and in NOTIFY.md, all
        of which the next agent reads and echoes back. A note that matched its
        own detector would make one real outage teach the loop to keep refunding
        forever - the PT-001 failure, generalised to the whole class.
        """
        notes = [self.tick.InfraOutcome("usage limit", "plan limit hit").note]
        notes += [note for _, note, _ in self.tick.INFRA_CAUSES]
        notes.append("the project's working copy is not there (/tmp/demo)")
        for note in notes:
            for suffix in ("", "; attempt refunded and task requeued"):
                text = note + suffix
                with self.subTest(note=text[:50]):
                    self.assertIsNone(
                        self.tick.detect_infra({"stderr": text, "result": None}, code=1),
                        "the handler recognised a line it writes itself")

    def test_the_refund_cap_is_one_cap_for_the_whole_class(self) -> None:
        """Per task, not per cause: three causes may not take turns forever.

        A cap per cause would mean five categories times three refunds before a
        task that is quietly broken ever meets its ceiling, and adding a sixth
        category would silently raise it again.
        """
        self.new_project()
        task_id = self.new_task("Work", "--ready", "--max-attempts", "5")
        causes = [self.tick.InfraOutcome("usage limit", "plan limit hit",
                                         time.time() + 900),
                  self.tick.InfraOutcome("network", "no route out"),
                  self.tick.InfraOutcome("disk", "the volume is full")]
        for round_number, infra in enumerate(causes, start=1):
            self.claim(task_id, generation=round_number)
            self.assertEqual(self.summary(task_id)["attempts"], 1)
            self.tick.settle_task(task_id, round_number, code=1, reason=None, infra=infra)
            summary = self.summary(task_id)
            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["attempts"], 0, "the attempt must be given back")
            self.assertEqual(summary["refunds"], round_number)
            self.assertIn(infra.cause, summary["last_error"],
                          "the reason belongs in last_error, not only in the log")

        # the cap is what stops a misdetection from removing the ceiling entirely
        self.claim(task_id, generation=4)
        self.tick.settle_task(task_id, 4, code=1, reason=None,
                              infra=self.tick.InfraOutcome("network", "no route out"))
        summary = self.summary(task_id)
        self.assertEqual(summary["attempts"], 1, "past the cap the attempt is kept")
        self.assertEqual(summary["refunds"], 3)

    def test_an_infra_outcome_installs_exactly_one_wait(self) -> None:
        """Where the wait lives is the outcome's to say, and it says one place.

        A cause that names its own end holds the whole loop, and putting a
        second hold on the task would outlive `bin/loopctl start` and make that
        escape hatch not escape. A cause that names no end holds only the task,
        so the loop can go on working other projects while it clears.
        """
        self.new_project()

        held = self.new_task("Limited", "--ready", "--max-attempts", "5")
        self.claim(held)
        self.tick.settle_task(held, 1, code=1, reason=None,
                              infra=self.tick.InfraOutcome("usage limit", "plan limit hit",
                                                           time.time() + 900))
        self.assertEqual(self.summary(held)["ready_after"], "",
                         "the loop-wide hold is already the wait")

        backed_off = self.new_task("Offline", "--ready", "--max-attempts", "5")
        self.claim(backed_off)
        self.tick.settle_task(backed_off, 1, code=1, reason=None,
                              infra=self.tick.InfraOutcome("network", "no route out"))
        self.assertTrue(self.summary(backed_off)["ready_after"],
                        "a cause with no known end holds the task, not the loop")

    def test_a_genuine_task_failure_is_still_counted(self) -> None:
        """The whole point of the class is that this path is left alone."""
        self.new_project()
        task_id = self.new_task("Work", "--ready", "--max-attempts", "5")
        for round_number in range(1, 4):
            self.claim(task_id, generation=round_number)
            self.tick.settle_task(task_id, round_number, code=1, reason=None)
            summary = self.summary(task_id)
            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["attempts"], round_number,
                             "a failure that is the task's own must burn an attempt")
            self.assertEqual(summary["refunds"], 0, "nothing was refunded")

    def test_a_failed_run_is_held_back_by_a_growing_backoff(self) -> None:
        self.new_project()
        task_id = self.new_task("Work", "--ready", "--max-attempts", "5")
        self.claim(task_id)
        self.tick.settle_task(task_id, 1, code=1, reason=None)
        summary = self.summary(task_id)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["attempts"], 1)
        self.assertTrue(summary["ready_after"], "a requeued task must wait before retrying")
        self.assertIn("code 1", summary.get("last_error", "") or
                      self.summary(task_id).get("last_error", ""))

        rows = json.loads(self.task("list", "--json"))
        reasons = [row["blocked_by"] for row in rows if row["id"] == task_id][0]
        self.assertTrue(any("scheduled for" in reason for reason in reasons))
        self.assertEqual(self.tl.retry_delay(1), 600)
        self.assertEqual(self.tl.retry_delay(2), 1200)
        self.assertEqual(self.tl.retry_delay(99), 21600)

    def test_a_stop_is_a_pause_not_a_failure(self) -> None:
        """`STOP` is the human's brake; the task it interrupted did nothing wrong."""
        self.new_project()
        task_id = self.new_task("Work", "--ready")
        self.claim(task_id)
        self.tick.settle_task(task_id, 1, code=-15, reason=self.tick.STOP_REASON)
        summary = self.summary(task_id)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["attempts"], 0)
        self.assertEqual(summary["ready_after"], "", "a stopped task must not be held back")

    def test_attempts_exhausted_still_escalates(self) -> None:
        self.new_project()
        task_id = self.new_task("Work", "--ready", "--max-attempts", "1")
        self.claim(task_id)
        self.tick.settle_task(task_id, 1, code=1, reason=None)
        self.assertEqual(self.summary(task_id)["status"], "needs-human")


class TestTaskAfterTask(TickTestCase):
    """One tick works until there is nothing left, not until it has done one task.

    A tick used to run a single task and exit, so the board sat idle until
    launchd fired again -- a whole tick interval of dead air after every task,
    even with the next task ready and its repository free.
    """

    def agent_that_closes_whatever_it_is_given(self) -> Path:
        """A stand-in agent: it finds the claimed task and closes it."""
        agent = self.root / "closing-agent"
        agent.write_text(
            "#!/bin/sh\n"
            f'id=$("{self.root}/bin/task" list --status in-progress | awk \'{{print $1}}\' | head -1)\n'
            "echo \"closing $id\"\n"
            f'"{self.root}/bin/task" done "$id" -m "closed by the stand-in agent"'
            f' -d "the stand-in agent closed this task and changed nothing at all" >/dev/null\n',
            encoding="utf-8")
        agent.chmod(0o755)
        return agent

    def setUp(self) -> None:
        super().setUp()
        agent = self.agent_that_closes_whatever_it_is_given()
        config = json.loads((self.root / "config.json").read_text())
        config["agent"] = "closing"
        config["agents"] = {"closing": {"command": [str(agent)], "prompt": "arg",
                                        "workdir_flag": None, "stream": "text"}}
        config["git_autocommit"] = False
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.load_tick()
        # Grooming would otherwise run once on a board that has never groomed,
        # which is real behaviour but not what this test is about.
        self.tick.LAST_GROOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.tick.LAST_GROOM_FILE.write_text(str(time.time()), encoding="utf-8")
        self.new_project()

    def ready_task(self, title: str) -> str:
        return self.task("new", "--project", "demo", "--title", title,
                         "--ready", "--force").split()[0]

    def test_one_tick_works_every_ready_task(self) -> None:
        first, second, third = (self.ready_task("First"), self.ready_task("Second"),
                                self.ready_task("Third"))

        self.assertEqual(self.tick.main(), 0)

        for task_id in (first, second, third):
            self.assertEqual(self.summary(task_id)["status"], "done",
                             f"{task_id} was ready and the tick had time for it")

    def test_the_wall_ceiling_ends_the_tick_between_tasks(self) -> None:
        """A ceiling stops the tick cleanly, and never mid-task."""
        first, second = self.ready_task("First"), self.ready_task("Second")
        self.tick.CONFIG["max_tick_wall_sec"] = 1
        # A task that outlasts the ceiling. The check happens after the task, so
        # the one in flight finishes and the next one is never claimed.
        original = self.tick.run_work
        self.tick.run_work = lambda generation: (time.sleep(1.2),
                                                 original(generation))[1]

        self.assertEqual(self.tick.main(), 0)

        finished = [self.summary(t)["status"] for t in (first, second)]
        self.assertEqual(finished.count("done"), 1,
                         "the ceiling is checked after a task, so exactly one runs")
        self.assertIn("ready", finished, "the other is left claimable, not half-run")

    def test_a_stop_file_ends_the_tick_between_tasks(self) -> None:
        first = self.ready_task("First")
        self.ready_task("Second")
        original = self.tick.run_work

        def stop_after_one(generation: int) -> bool:
            worked = original(generation)
            self.tick.STOP_FILE.write_text("", encoding="utf-8")
            return worked

        self.tick.run_work = stop_after_one
        self.assertEqual(self.tick.main(), 0)
        self.assertEqual(self.summary(first)["status"], "done")
        self.assertEqual([t.id for t in self.tl.iter_tasks(status="in-progress")], [],
                         "nothing may be left in-progress when a tick stops")

    def spend(self, usd: float, day: str | None = None) -> None:
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.tick.SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.tick.SPEND_FILE.write_text(
            json.dumps({day: {"runs": 1, "usd": usd}}), encoding="utf-8")

    def test_the_day_s_ceiling_stops_new_work(self) -> None:
        """Recorded and never read was the whole problem: the numbers only ever
        said afterwards what a day had cost."""
        task_id = self.ready_task("First")
        self.tick.CONFIG["daily_spend_limit_usd"] = 10
        self.spend(10.0)

        self.assertEqual(self.tick.main(), 0)

        self.assertEqual(self.summary(task_id)["status"], "ready",
                         "the ceiling holds work rather than failing it")

    def test_under_the_ceiling_the_loop_works_as_usual(self) -> None:
        task_id = self.ready_task("First")
        self.tick.CONFIG["daily_spend_limit_usd"] = 10
        self.spend(9.99)

        self.assertEqual(self.tick.main(), 0)

        self.assertEqual(self.summary(task_id)["status"], "done")

    def test_a_ceiling_of_zero_is_no_ceiling(self) -> None:
        """The default. A board that never set a limit must not acquire one."""
        task_id = self.ready_task("First")
        self.tick.CONFIG["daily_spend_limit_usd"] = 0
        self.spend(10_000.0)

        self.assertEqual(self.tick.main(), 0)

        self.assertEqual(self.summary(task_id)["status"], "done")

    def test_the_ceiling_lifts_itself_when_the_day_turns(self) -> None:
        """Nothing has to remember to clear it, and a restart cannot lose it."""
        task_id = self.ready_task("First")
        self.tick.CONFIG["daily_spend_limit_usd"] = 10
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        self.spend(500.0, day=yesterday)

        reached, spent, _ = self.tick.spend_ceiling_reached()
        self.assertFalse(reached)
        self.assertEqual(0.0, spent, "yesterday's spending is not today's")
        self.assertEqual(self.tick.main(), 0)
        self.assertEqual(self.summary(task_id)["status"], "done")

    def test_a_run_in_flight_is_never_killed_by_the_ceiling(self) -> None:
        """The check is a gate on starting, not a kill switch: a run cut off
        halfway has burned the attempt it just paid for."""
        first, second = self.ready_task("First"), self.ready_task("Second")
        self.tick.CONFIG["daily_spend_limit_usd"] = 10
        original = self.tick.run_work

        def spend_it_all(generation: int) -> bool:
            worked = original(generation)
            self.spend(10.0)
            return worked

        self.tick.run_work = spend_it_all
        self.assertEqual(self.tick.main(), 0)

        finished = [self.summary(t)["status"] for t in (first, second)]
        self.assertEqual(finished.count("done"), 1,
                         "the task that was already running finished")
        self.assertIn("ready", finished, "the next one was never started")

    def test_a_sibling_tick_never_starts_siblings_of_its_own(self) -> None:
        """The fan-out is one level deep, whatever max_concurrent_tasks says."""
        self.tick.CONFIG["max_concurrent_tasks"] = 4
        os.environ[self.tick.SIBLING_ENV] = "1"
        self.addCleanup(os.environ.pop, self.tick.SIBLING_ENV, None)
        self.assertEqual(self.tick.start_siblings(), 0)

    def test_a_single_slot_board_starts_no_siblings(self) -> None:
        self.tick.CONFIG["max_concurrent_tasks"] = 1
        self.assertEqual(self.tick.start_siblings(), 0)


class TestOtherAgents(TickTestCase):
    """The loop must not be tied to one agent CLI.

    What it needs is narrow: a command that runs non-interactively, takes a
    prompt, and prints something while it works. Everything else - the flags, how
    the prompt is delivered, whether the output is JSON - belongs to a profile.
    """

    def test_the_launch_command_follows_the_profile(self) -> None:
        self.load_tick()
        cases = [
            ({"command": ["claude", "--verbose"], "prompt": "flag:-p",
              "workdir_flag": "--add-dir", "stream": "claude-json"},
             (["claude", "--verbose", "--add-dir", "/repo", "-p", "PROMPT"], None)),
            ({"command": ["opencode", "run"], "prompt": "arg"},
             (["opencode", "run", "PROMPT"], None)),
            ({"command": ["some-agent"], "prompt": "stdin"},
             (["some-agent"], "PROMPT")),
        ]
        for profile, expected in cases:
            with self.subTest(profile=profile["command"][0]):
                resolved = dict(self.tl.DEFAULT_PROFILE, **profile)
                self.assertEqual(self.tl.agent_command(resolved, "PROMPT", ["/repo"]), expected)

    def test_an_unknown_profile_is_a_clear_error(self) -> None:
        self.load_tick()
        with self.assertRaises(SystemExit) as caught:
            self.tl.agent_profile({"agent": "nope", "agents": {}})
        self.assertIn("no agent profile named 'nope'", str(caught.exception))

    def test_an_agent_that_narrates_on_stderr_is_not_a_stalled_agent(self) -> None:
        """Progress is any output, on either stream.

        Found by running the loop on a second CLI: opencode writes its progress to
        stderr, so a heartbeat that counted stdout alone saw a busy agent as a
        silent one and the run was settled as "exited without a terminal
        transition" after zero events.
        """
        agent = self.root / "stderr-agent"
        agent.write_text("#!/bin/sh\n"
                         "echo 'reading files' >&2\n"
                         "echo 'editing' >&2\n"
                         "echo 'done' >&2\n", encoding="utf-8")
        agent.chmod(0o755)
        config = json.loads((self.root / "config.json").read_text())
        config["agent"] = "quiet"
        config["agents"] = {"quiet": {"command": [str(agent)], "prompt": "arg",
                                      "workdir_flag": None, "stream": "text"}}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.load_tick()

        holder = self.tick.Holder(1, "none")
        code, reason, _ = self.tick.run_agent(
            "prompt", holder, [], self.root / ".runtime" / "runs" / "stderr.jsonl")

        self.assertEqual(code, 0)
        self.assertIsNone(reason, "a talking agent must not be killed as stalled")
        self.assertGreaterEqual(holder.events, 3)

    def test_a_ceaselessly_talking_agent_is_still_stopped(self) -> None:
        """The brakes are on a clock of their own, not on the agent going quiet.

        The reader that replaced `select()` yields an idle tick when nothing has
        arrived, and it was tempting to hang STOP, the stall timer and the wall
        clock off that tick. An agent narrating without a pause never produces
        one, so the run would have been unstoppable for exactly the runs most
        worth stopping.
        """
        agent = self.root / "chatty-agent"
        agent.write_text("#!/bin/sh\nwhile true; do echo 'thinking'; done\n",
                         encoding="utf-8")
        agent.chmod(0o755)
        config = json.loads((self.root / "config.json").read_text())
        config["agent"] = "chatty"
        config["agents"] = {"chatty": {"command": [str(agent)], "prompt": "arg",
                                       "workdir_flag": None, "stream": "text"}}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.load_tick()
        self.tick.STOP_FILE.write_text("stop", encoding="utf-8")

        holder = self.tick.Holder(1, "none")
        started = time.monotonic()
        _, reason, _ = self.tick.run_agent(
            "prompt", holder, [], self.root / ".runtime" / "runs" / "chatty.jsonl")

        self.assertEqual(reason, self.tick.STOP_REASON)
        self.assertLess(time.monotonic() - started, 60,
                        "STOP has to be seen while the agent is still talking")

    def test_a_plain_text_agent_drives_a_task_to_done(self) -> None:
        """An agent that prints prose, not JSON, still runs the loop end to end.

        Its lines are the heartbeat exactly as a JSON stream's are; what is lost
        is only what the JSON carried - the reported cost.
        """
        agent = self.root / "text-agent"
        agent.write_text(
            "#!/bin/sh\n"
            "echo 'reading the task'\n"
            "echo 'doing the work'\n"
            f'"{self.root}/bin/task" done DM-001 -m "finished by a text agent"'
            f' -d "a text agent finished this and decided nothing worth recording" >/dev/null\n'
            "echo 'done'\n", encoding="utf-8")
        agent.chmod(0o755)

        config = json.loads((self.root / "config.json").read_text())
        config["agent"] = "text"
        config["agents"] = {"text": {"command": [str(agent)], "prompt": "arg",
                                     "workdir_flag": None, "stream": "text"}}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")

        self.new_project()
        task_id = self.new_task("Work for a text agent", "--ready")
        self.assertEqual(task_id, "DM-001")
        self.load_tick()
        self.task("claim", task_id, "--generation", "1")

        holder = self.tick.Holder(1, task_id)
        code, reason, evidence = self.tick.run_agent(
            "prompt", holder, [], self.root / ".runtime" / "runs" / "text.jsonl")

        self.assertEqual(code, 0)
        self.assertIsNone(reason)
        self.assertGreaterEqual(holder.events, 3, "every printed line is a heartbeat")
        self.assertIsNone(evidence["result"], "a text stream carries no result event")
        self.assertEqual(self.summary(task_id)["status"], "done")

        self.tick.settle_task(task_id, 1, code, reason)
        self.assertEqual(self.summary(task_id)["status"], "done")


class TestParallelWork(TickTestCase):
    """Several runs at once, one per resource.

    The invariant does not change - a task is in-progress only while a live
    holder owns its lock - it is just asked once per resource instead of once
    for the whole board.
    """

    def setUp(self) -> None:
        super().setUp()
        config = json.loads((self.root / "config.json").read_text())
        config["max_concurrent_tasks"] = 2
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.load_tick()
        self.task("project-new", "alpha", "--repo", "/tmp/repo-a", "--prefix", "AL")
        self.task("project-new", "beta", "--repo", "/tmp/repo-b", "--prefix", "BE")

    def make(self, project: str, title: str) -> str:
        # --force for the same reason as new_task(): this is about which
        # resource a task needs, not about whether the task is well written.
        return self.task("new", "--project", project, "--title", title,
                         "--ready", "--force").split()[0]

    def key_of(self, task_id: str) -> str:
        return self.tl.resource_key(self.tl.find_task(task_id))

    def test_the_resource_is_the_working_copy_not_the_project(self) -> None:
        """Two projects pointing at one checkout must not run at the same time."""
        self.task("project-new", "gamma", "--repo", "/tmp/repo-a", "--prefix", "GA")
        alpha, gamma, beta = (self.make("alpha", "A"), self.make("gamma", "G"),
                              self.make("beta", "B"))
        self.assertEqual(self.key_of(alpha), self.key_of(gamma))
        self.assertNotEqual(self.key_of(alpha), self.key_of(beta))

    def test_a_task_with_no_repository_is_exclusive_only_with_itself(self) -> None:
        self.task("project-new", "notes", "--prefix", "NO")
        first, second = self.make("notes", "One"), self.make("notes", "Two")
        self.assertNotEqual(self.key_of(first), self.key_of(second))

    def test_a_busy_repository_is_skipped_for_work_elsewhere(self) -> None:
        first = self.make("alpha", "Alpha one")
        self.make("alpha", "Alpha two")
        third = self.make("beta", "Beta one")

        busy = self.tick.resource_lock(self.key_of(first))
        self.assertTrue(busy.acquire())
        try:
            task, lock = self.tick.claim_a_task(7)
            self.assertIsNotNone(task, "a busy project must not stall the whole board")
            self.assertEqual(task.id, third, "both alpha tasks share the busy repository")
            self.assertEqual(task.status, "in-progress")
            lock.release()
        finally:
            busy.release()

    def test_slots_bound_how_many_runs_start(self) -> None:
        held = [self.tick.acquire_slot(), self.tick.acquire_slot()]
        try:
            self.assertTrue(all(held), "the cap in this board is two")
            self.assertIsNone(self.tick.acquire_slot(), "the third tick starts nothing")
        finally:
            for slot in held:
                if slot:
                    slot.release()
        self.assertIsNotNone(self.tick.acquire_slot())

    def test_a_live_run_is_never_reclaimed_from_under_itself(self) -> None:
        task_id = self.make("alpha", "Alpha one")
        self.task("claim", task_id, "--generation", "1")

        holder = self.tick.resource_lock(self.key_of(task_id))
        self.assertTrue(holder.acquire())
        try:
            self.assertEqual(self.tick.reclaim_orphans(), 0)
            self.assertEqual(self.summary(task_id)["status"], "in-progress")
        finally:
            holder.release()

        # the same task, once nobody holds its resource, is an orphan
        self.assertEqual(self.tick.reclaim_orphans(), 1)
        self.assertEqual(self.summary(task_id)["status"], "ready")

    def test_the_lease_on_the_task_is_what_fences_a_late_run(self) -> None:
        """A run that was reclaimed and re-claimed elsewhere must not settle it."""
        task_id = self.make("alpha", "Alpha one")
        self.task("claim", task_id, "--generation", "1")
        # --force stands in for what reclaiming actually does: `bin/tick` calls
        # Task.move() in the library, below the CLI and below lint. A recovery
        # path lint could shut would strand the task in-progress forever.
        self.task("move", task_id, "ready", "-m", "reclaimed", "--force")
        self.task("claim", task_id, "--generation", "2")

        self.tick.settle_task(task_id, 1, code=1, reason=None)
        self.assertEqual(self.summary(task_id)["status"], "in-progress",
                         "generation 1 no longer owns this task")


class TestBoundedContext(BoardTestCase):
    """The task file is what an agent reads on every attempt, so it stays small."""

    def path_of(self, task_id: str) -> Path:
        return self.root / self.summary(task_id)["path"]

    def sidecar_of(self, task_id: str) -> Path:
        path = self.path_of(task_id)
        return path.parent / f"{task_id}.log.jsonl"

    def test_the_body_keeps_only_recent_entries_and_the_sidecar_keeps_all(self) -> None:
        self.new_project()
        task_id = self.new_task("Bounded")
        for n in range(20):
            self.task("log", task_id, "-m", f"entry {n}")

        body = self.path_of(task_id).read_text(encoding="utf-8")
        self.assertIn("entry 19", body)
        self.assertNotIn("entry 5", body)
        # 20 log lines plus the "created in backlog" line the CLI writes
        entries = [json.loads(line) for line
                   in self.sidecar_of(task_id).read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(entries), 21)
        self.assertEqual(entries[0]["message"], "created in backlog")
        self.assertEqual(entries[-1]["message"], "entry 19")

    def test_show_puts_the_task_and_its_whole_log_back_together(self) -> None:
        self.new_project()
        task_id = self.new_task("Bounded")
        for n in range(20):
            self.task("log", task_id, "-m", f"entry {n}")

        shown = self.task("show", task_id)
        for n in range(20):
            self.assertIn(f"entry {n}", shown)
        self.assertNotIn("entry 5", self.task("show", task_id, "--raw"))

    def test_a_log_written_before_the_sidecar_existed_is_migrated_not_lost(self) -> None:
        """The board has live tasks whose only history is inside the file."""
        self.new_project()
        task_id = self.new_task("Legacy")
        path = self.path_of(task_id)
        sidecar = self.sidecar_of(task_id)
        sidecar.unlink()
        text = path.read_text(encoding="utf-8")
        head, _, _ = text.partition("## Log")
        path.write_text(head + "## Log\n\n"
                        + "- 2020-01-01T00:00:00Z human: written by hand\n"
                        + "- 2020-01-02T00:00:00Z agent: and never lost\n",
                        encoding="utf-8")

        self.task("log", task_id, "-m", "the first entry after the change")

        entries = [json.loads(line) for line
                   in sidecar.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual([e["message"] for e in entries],
                         ["written by hand", "and never lost",
                          "the first entry after the change"])
        self.assertEqual(entries[0]["actor"], "human")

    def test_the_sidecar_follows_the_task_between_statuses(self) -> None:
        """A sidecar left behind would silently start a second, empty history."""
        self.new_project()
        task_id = self.new_task("Travelling")
        before = self.sidecar_of(task_id)
        self.task("move", task_id, "in-progress", "--force", "-m", "test")
        after = self.sidecar_of(task_id)

        self.assertNotEqual(before, after)
        self.assertFalse(before.exists())
        self.assertTrue(after.exists())
        self.assertIn("backlog -> in-progress",
                      "".join(after.read_text(encoding="utf-8").splitlines()))

    def test_archiving_takes_the_sidecar_along(self) -> None:
        self.new_project()
        task_id = self.new_task("Old")
        self.close_and_age(task_id)
        self.task("archive", "--days", "1")

        found = list((self.root / "archive").rglob(f"{task_id}.log.jsonl"))
        self.assertEqual(len(found), 1, "the archived task lost its history")

    def test_attempts_are_recorded_one_bounded_line_each(self) -> None:
        self.new_project()
        task_id = self.new_task("Flaky", "--ready")
        for n in range(1, 4):
            self.task("claim", task_id, "--generation", str(n))
            self.task("fail", task_id, "-m", f"broke on step {n}")
            if n < 3:
                self.task("move", task_id, "ready", "--force", "-m", "retry")

        body = self.path_of(task_id).read_text(encoding="utf-8")
        section = body.partition("## Attempts")[2].partition("\n## ")[0]
        lines = [l for l in section.splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 3)
        self.assertIn("broke on step 3", lines[0])   # newest first
        self.assertIn("3/3", lines[0])
        self.assertIn("broke on step 1", lines[2])

    def test_the_attempt_history_is_capped(self) -> None:
        tl = self.load_tasklib()
        self.new_project()
        task_id = self.new_task("Noisy")
        task = tl.find_task(task_id)
        for n in range(10):
            task.note_attempt("failed", f"reason {n}")
        section = task.body.partition("## Attempts")[2].partition("\n## ")[0]
        lines = [l for l in section.splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), tl.load_config()["task_attempts_kept"])
        self.assertIn("reason 9", lines[0])

    def test_checkpoint_stays_above_the_log_however_long_the_log_gets(self) -> None:
        """A resuming run should not have to scroll past the log to find it."""
        self.new_project()
        task_id = self.new_task("Resumable")
        for n in range(5):
            self.task("log", task_id, "-m", f"entry {n}")
        self.task("checkpoint", task_id, "-m", "half way; next is /orders")

        body = self.path_of(task_id).read_text(encoding="utf-8")
        self.assertLess(body.index("## Checkpoint"), body.index("## Log"))
        self.assertIn("half way; next is /orders", body)


class TestPromptBrief(TickTestCase):
    """What the wrapper knows, the prompt says - no tool call needed to learn it."""

    def setUp(self) -> None:
        super().setUp()
        self.load_tick()

    def test_the_prompt_carries_attempt_budget_checkpoint_failure_and_brief(self) -> None:
        self.new_project()
        task_id = self.new_task("Briefed", "--ready")
        self.task("claim", task_id, "--generation", "1")
        self.task("checkpoint", task_id, "-m", "three of seven endpoints migrated")
        self.task("fail", task_id, "-m", "the fixture repo was missing")
        self.task("move", task_id, "ready", "--force", "-m", "retry")
        self.task("claim", task_id, "--generation", "2")

        task = self.tl.find_task(task_id)
        prompt = self.tick.build_prompt(self.root / "prompts" / "tick.md", {
            "TASK_ID": task.id,
            "TASK_TITLE": task.title,
            "TASK_PATH": str(task.path),
            "PROJECT_NAME": task.project,
            "PROJECT_DIR": str(task.project_dir),
            "PROJECT_FILE": str(task.project_dir / "project.md"),
            "REPO_PATH": "/tmp/demo",
            "TASKS_ROOT": str(self.root),
            "GENERATION": 2,
            "ATTEMPT": self.tick.attempt_line(task),
            "WALL_BUDGET": self.tick.wall_budget_line(),
            "CHECKPOINT": task.checkpoint_text(),
            "LAST_FAILURE": self.tick.failure_brief(task),
            "LOCAL_POLICY": self.tl.local_policy() or "(none)",
            "PROJECT_BRIEF": self.tl.project_brief(task.project_dir) or "(none)",
            "PROJECT_JOURNAL": self.tl.project_journal(task.project_dir) or "(none)",
        })

        self.assertNotIn("{{", prompt)
        self.assertIn("2 of 3", prompt)
        self.assertIn("min of wall clock", prompt)
        self.assertIn("three of seven endpoints migrated", prompt)
        self.assertIn("the fixture repo was missing", prompt)

    def test_the_last_attempt_says_so(self) -> None:
        self.new_project()
        task_id = self.new_task("Final", "--ready")
        for n in (1, 2):
            self.task("claim", task_id, "--generation", str(n))
            self.task("fail", task_id, "-m", "no")
            self.task("move", task_id, "ready", "--force", "-m", "retry")
        self.task("claim", task_id, "--generation", "3")
        line = self.tick.attempt_line(self.tl.find_task(task_id))
        self.assertIn("3 of 3", line)
        self.assertIn("needs-human", line)

    def test_a_wrapper_kill_is_written_into_the_attempt_history(self) -> None:
        self.new_project()
        task_id = self.new_task("Killed", "--ready")
        self.task("claim", task_id, "--generation", "1")
        self.tick.settle_task(task_id, 1, 143, "stalled for 20 min")

        brief = self.tick.failure_brief(self.tl.find_task(task_id))
        self.assertIn("interrupted", brief)
        self.assertIn("stalled for 20 min", brief)

    def test_the_project_brief_is_bounded(self) -> None:
        self.new_project()
        project_file = self.root / "projects" / "demo" / "project.md"
        project_file.write_text(project_file.read_text(encoding="utf-8")
                                + "\n" + "\n".join(f"line {n}" for n in range(500)),
                                encoding="utf-8")
        brief = self.tl.project_brief(self.root / "projects" / "demo")
        self.assertLessEqual(len(brief), self.tl.load_config()["project_brief_chars"] + 200)
        self.assertIn("truncated", brief)


class TestProjectJournal(BoardTestCase):
    """What one task decided, the next task in that project is told.

    The failure this covers is the one nothing else on the board catches: a task
    written three weeks ago, worked by an agent that remembers none of the four
    tasks that changed the repository since, against criteria that describe a
    state the project has left behind.
    """

    DECISION = ("Retries back off per destination rather than globally, because one "
                "slow endpoint was stalling every other delivery. Dead letters are "
                "still dropped - that is the next task.")

    def journal(self) -> Path:
        return self.root / "projects" / "demo" / "journal.md"

    def raw(self, *args: str):
        """The CLI without the assertion that it succeeded."""
        return subprocess.run([sys.executable, str(self.root / "bin" / "task"), *args],
                              capture_output=True, text=True)

    def working_task(self, title: str = "Retry failed deliveries") -> str:
        task_id = self.new_task(title)
        self.task("move", task_id, "in-progress", "--force", "-m", "start")
        return task_id

    # --- the requirement ----------------------------------------------------

    def test_a_task_cannot_close_without_a_line_for_the_next_one(self) -> None:
        self.new_project()
        task_id = self.working_task()

        result = self.raw("done", task_id, "-m", "all four endpoints migrated")

        self.assertEqual(result.returncode, 1)
        self.assertIn("project journal", result.stderr)
        self.assertEqual(self.summary(task_id)["status"], "in-progress",
                         "a refused entry must not leave the task half-transitioned")

    def test_the_same_door_is_guarded_from_the_other_side(self) -> None:
        """`move <id> done` would otherwise be the requirement's way out."""
        self.new_project()
        task_id = self.working_task()

        self.assertEqual(self.raw("move", task_id, "done", "-m", "x").returncode, 1)
        self.assertEqual(self.raw("move", task_id, "review", "-m", "x").returncode, 1)
        self.assertEqual(self.summary(task_id)["status"], "in-progress")

    def test_the_transitions_that_decided_nothing_are_not_asked(self) -> None:
        self.new_project()
        for status in ("blocked", "needs-human", "failed"):
            task_id = self.working_task(f"Work that ends {status}")
            self.task("move", task_id, status, "-m", "no entry owed")
            self.assertEqual(self.summary(task_id)["status"], status)

    def test_force_closes_a_task_that_genuinely_decided_nothing(self) -> None:
        self.new_project()
        task_id = self.working_task()
        self.task("done", task_id, "-m", "a no-op", "--force")
        self.assertEqual(self.summary(task_id)["status"], "done")
        self.assertFalse(self.journal().exists())

    # --- what gets written --------------------------------------------------

    def test_the_newest_entry_is_the_one_read_first(self) -> None:
        self.new_project()
        first = self.working_task("The older task")
        self.task("done", first, "-m", "ok", "-d", "The older decision, made first "
                  "and therefore the one further from what is true now.")
        second = self.working_task("The newer task")
        self.task("done", second, "-m", "ok", "-d", self.DECISION)

        text = self.journal().read_text(encoding="utf-8")
        self.assertLess(text.index("Retries back off"), text.index("The older decision"),
                        "the head of the file is what a prompt inlines and a person reads")
        self.assertIn("# Journal - demo", text)
        for task_id in (first, second):
            self.assertIn(task_id, text)
        self.assertIn("(done)", text)

    def test_an_entry_is_a_paragraph_neither_a_word_nor_a_report(self) -> None:
        self.new_project()
        task_id = self.working_task()

        word = self.raw("done", task_id, "-m", "ok", "-d", "done")
        self.assertEqual(word.returncode, 1)
        self.assertIn("not a word", word.stderr)

        essay = self.raw("done", task_id, "-m", "ok", "-d", "why. " * 400)
        self.assertEqual(essay.returncode, 1)
        self.assertIn("capped", essay.stderr)
        self.assertIn("log", essay.stderr, "the refusal has to say where the text belongs")

        self.assertEqual(self.summary(task_id)["status"], "in-progress")

    def test_a_heading_inside_an_entry_cannot_start_a_new_one(self) -> None:
        self.new_project()
        task_id = self.working_task()
        self.task("done", task_id, "-m", "ok",
                  "-d", "## 2026-01-01 FAKE-001 (done) - not an entry\n\n" + self.DECISION)

        tl = self.load_tasklib()
        entries = tl.journal_entries(self.root / "projects" / "demo")
        self.assertEqual(len(entries), 1)
        self.assertIn("#### 2026-01-01", entries[0])

    def test_one_task_leaves_one_entry_however_often_it_moves(self) -> None:
        """A human moving work on from `review` is not a second decision."""
        self.new_project()
        task_id = self.working_task()
        self.task("review", task_id, "-m", "worked", "-d", self.DECISION)

        self.task("done", task_id, "-m", "looked at it")

        self.assertEqual(self.summary(task_id)["status"], "done")
        self.assertEqual(self.journal().read_text(encoding="utf-8").count(task_id), 1)

    def test_the_entry_outlives_the_task_that_wrote_it(self) -> None:
        """The reason the journal is its own file and not a section of the task."""
        self.new_project()
        task_id = self.new_task("Retry failed deliveries")
        self.task("move", task_id, "in-progress", "--force", "-m", "start")
        self.task("done", task_id, "-m", "ok", "-d", self.DECISION)
        path = self.root / self.summary(task_id)["path"]
        lines = ["closed: 2020-01-01T00:00:00Z" if line.startswith("closed:") else line
                 for line in path.read_text(encoding="utf-8").splitlines()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.task("archive")

        self.assertEqual([t.id for t in self.load_tasklib().iter_tasks()], [])
        self.assertIn("Retries back off", self.journal().read_text(encoding="utf-8"))

    # --- what the prompt is given -------------------------------------------

    def test_the_prompt_gets_whole_entries_and_says_what_it_left_out(self) -> None:
        self.new_project()
        for n in range(12):
            task_id = self.working_task(f"Task number {n}")
            self.task("done", task_id, "-m", "ok",
                      "-d", f"Decision number {n}, taken because the alternative was "
                            f"worse in a way this sentence pads out to a realistic "
                            f"length. {'padding words ' * 20}")

        tl = self.load_tasklib()
        project_dir = self.root / "projects" / "demo"
        brief = tl.project_journal(project_dir)

        self.assertIn("Decision number 11", brief, "newest first")
        self.assertNotIn("Decision number 0,", brief)
        self.assertIn("older entries in", brief, "what was left out has to be findable")
        self.assertLess(len(brief), tl.load_config()["journal_brief_chars"] + 400)
        self.assertEqual(brief.count("Decision number"),
                         brief.count("### 20"),
                         "entries are cut whole; half an entry keeps the claim "
                         "and drops the reason for it")

    def test_a_project_with_no_history_says_so_rather_than_nothing(self) -> None:
        self.new_project()
        tl = self.load_tasklib()
        self.assertEqual(tl.project_journal(self.root / "projects" / "demo"), "")
        self.assertIn("no journal entries yet", self.task("journal"))


class TestLocalPolicy(BoardTestCase):
    """LOCAL.md is read out to every run, bounded like every other inlined file."""

    def setUp(self) -> None:
        super().setUp()
        self.tl = self.load_tasklib()

    def test_a_board_without_local_rules_says_nothing(self) -> None:
        self.assertEqual("", self.tl.local_policy())

    def test_the_rules_are_inlined_with_their_headings_demoted(self) -> None:
        (self.root / "LOCAL.md").write_text(
            "## Finish mode\n\nOpen nothing new.\n", encoding="utf-8")
        text = self.tl.local_policy()
        self.assertIn("Open nothing new.", text)
        self.assertIn("### Finish mode", text,
                      "inlined as-is, a heading would close the prompt's section")

    def test_the_inlined_rules_are_bounded(self) -> None:
        """A file that may grow without limit would grow into every prompt."""
        (self.root / "LOCAL.md").write_text("x" * 5000, encoding="utf-8")
        text = self.tl.local_policy(limit=200)
        self.assertLess(len(text), 400)
        self.assertIn("truncated", text)


class TestMirror(BoardTestCase):
    """Keeping a second checkout in step, without the ways copying goes wrong."""

    def git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com",
                        "-c", "user.name=test", *args], capture_output=True, text=True,
                       check=True)

    def setUp(self) -> None:
        super().setUp()
        self.other = build_board(Path(self._tmp.name) / "second")
        for root in (self.root, self.other):
            self.git(root, "init", "-q")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-qm", "initial")

    def change_here(self, text: str = "changed here\n") -> None:
        """A committed change in the source: mirror refuses to copy loose edits."""
        (self.root / "prompts" / "tick.md").write_text(text, encoding="utf-8")
        self.git(self.root, "add", "prompts/tick.md")
        self.git(self.root, "commit", "-qm", "change")

    def mirror(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / "bin" / "mirror"),
                               str(self.other), *args], capture_output=True, text=True)

    def test_local_policy_is_never_mirrored_and_never_pruned(self) -> None:
        """The board's own rules belong to the copy that wrote them.

        AGENTS.md is mirrored, so a rule written there is a rule for every board
        made from this one. A mode granted for a week is not, and putting it in a
        mirrored file is how this board lost one: the next mirror reverted it.
        """
        (self.other / "LOCAL.md").write_text(
            "# This copy only\n\nFinish what is open; open nothing new.\n",
            encoding="utf-8")
        self.git(self.other, "add", "LOCAL.md")
        self.git(self.other, "commit", "-qm", "local policy")

        result = self.mirror("--check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("in sync", result.stdout)
        self.assertNotIn("LOCAL.md", result.stdout)

        self.mirror("--prune")
        self.assertTrue((self.other / "LOCAL.md").is_file(),
                        "--prune must not take the running board's own rules away")

        (self.root / "LOCAL.md").write_text("the source's own rules\n", encoding="utf-8")
        self.git(self.root, "add", "LOCAL.md")
        self.git(self.root, "commit", "-qm", "source policy")
        self.mirror()
        self.assertNotIn("the source's own rules",
                         (self.other / "LOCAL.md").read_text(encoding="utf-8"),
                         "one copy's rules must not be handed to the other")

    def test_check_reports_drift_without_changing_anything(self) -> None:
        self.change_here()
        result = self.mirror("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("prompts/tick.md", result.stdout)
        self.assertNotEqual((self.other / "prompts" / "tick.md").read_text(), "changed here\n")

    def test_mirroring_copies_the_change_and_keeps_symlinks(self) -> None:
        self.change_here()
        result = self.mirror()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.other / "prompts" / "tick.md").read_text(), "changed here\n")
        # A symlink is copied as a symlink, never dereferenced into a second
        # copy of what it points at. CLAUDE.md was the reason for this and is a
        # plain file now - `@AGENTS.md`, because Windows cannot check out a
        # link - but the property is the mirror's, not that one file's, and a
        # board is free to keep links of its own.
        (self.root / "linked.md").symlink_to("AGENTS.md")
        self.git(self.root, "add", "linked.md")
        self.git(self.root, "commit", "-qm", "a link")
        self.mirror()
        self.assertTrue((self.other / "linked.md").is_symlink())
        self.assertEqual(os.readlink(self.other / "linked.md"), "AGENTS.md")
        # settings belong to each board: one may run two tasks at once, the other one
        local = json.loads((self.other / "config.json").read_text())
        local["max_concurrent_tasks"] = 3
        (self.other / "config.json").write_text(json.dumps(local), encoding="utf-8")
        self.mirror()
        self.assertEqual(json.loads((self.other / "config.json").read_text())
                         ["max_concurrent_tasks"], 3)
        self.assertEqual(self.mirror("--check").returncode, 0)

    def test_it_refuses_while_the_destination_loop_is_running(self) -> None:
        """Replacing tasklib under a working agent takes away its way to close up."""
        self.change_here()
        workers = self.other / ".runtime" / "workers"
        workers.mkdir(parents=True, exist_ok=True)
        (workers / "1.json").write_text(
            json.dumps({"pid": os.getpid(), "task_id": "DM-001"}), encoding="utf-8")

        result = self.mirror()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the loop is running", result.stderr)
        self.assertNotEqual((self.other / "prompts" / "tick.md").read_text(), "changed here\n")

    def test_it_refuses_to_overwrite_uncommitted_work(self) -> None:
        self.change_here()
        (self.other / "prompts" / "tick.md").write_text("edited there\n", encoding="utf-8")
        result = self.mirror()
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertEqual((self.other / "prompts" / "tick.md").read_text(), "edited there\n")

    def test_it_refuses_to_copy_something_not_committed_here(self) -> None:
        """A mirrored copy has to be a version the source can point at afterwards."""
        (self.root / "prompts" / "tick.md").write_text("loose edit\n", encoding="utf-8")
        result = self.mirror()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not committed here yet", result.stderr)

    def test_it_refuses_to_revert_work_that_only_exists_there(self) -> None:
        """The incident this guard exists for: the destination improved a file.

        Mirroring is one-directional, so without this the improvement is reverted
        with no more ceremony than a line saying "synced".
        """
        (self.other / "prompts" / "tick.md").write_text("improved there\n", encoding="utf-8")
        self.git(self.other, "add", "prompts/tick.md")
        self.git(self.other, "commit", "-qm", "improved over there")
        self.change_here()

        result = self.mirror()
        self.assertEqual(result.returncode, 1)
        self.assertIn("has never had", result.stderr)
        self.assertEqual((self.other / "prompts" / "tick.md").read_text(), "improved there\n")

        forced = self.mirror("--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual((self.other / "prompts" / "tick.md").read_text(), "changed here\n")


class TestGuard(BoardTestCase):
    """The PreToolUse guard is the only real boundary, so it gets tests too.

    SQL keywords are assembled from fragments on purpose: these strings pass
    through the guard when this file is written or read by an agent, and a
    literal would deny the tool call that creates the test.
    """

    DELETE = "DEL" + "ETE"

    def guard(self, command: str, tool: str = "Bash", cwd: str = "/tmp",
              marker: bool = False) -> str | None:
        return self.guard_input(tool, {"command": command}, cwd=cwd, marker=marker)

    def guard_input(self, tool: str, tool_input: dict, cwd: str = "/tmp",
                    marker: bool = False) -> str | None:
        """`marker` starts the hook as a loop run would: with PROJECT_TASKS_RUN set."""
        payload = json.dumps({"tool_name": tool, "cwd": cwd, "tool_input": tool_input})
        env = dict(os.environ)
        env.pop("PROJECT_TASKS_RUN", None)
        if marker:
            env["PROJECT_TASKS_RUN"] = "1"
        result = subprocess.run([str(self.root / ".claude" / "hooks" / "guard.py")],
                                input=payload, capture_output=True, text=True, env=env)
        if "deny" not in result.stdout:
            return None
        reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        return reason.split("]")[0].lstrip("[")

    def test_a_loop_run_cannot_edit_the_framework(self) -> None:
        """The drift this board spent two reconciliations undoing came in here.

        Three commits on the live board changed AGENTS.md and prompts/tick.md
        from inside a run working an unrelated project's task, and each landed
        under that task's commit message.
        """
        root = str(self.root.resolve())
        for path in (f"{root}/bin/tick", f"{root}/lib/tasklib.py",
                     f"{root}/prompts/tick.md", f"{root}/AGENTS.md",
                     f"{root}/README.md", f"{root}/config.json",
                     f"{root}/.claude/deny.json", f"{root}/LOCAL.md",
                     f"{root}/.opencode/plugin/guard.js"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.guard_input("Write", {"file_path": path, "content": "x"},
                                     marker=True),
                    "framework-edit")

    def test_the_same_edit_by_hand_is_allowed(self) -> None:
        """The rule binds a run, not a checkout: this is where the work belongs."""
        root = str(self.root.resolve())
        self.assertIsNone(
            self.guard_input("Write", {"file_path": f"{root}/bin/tick", "content": "x"}))
        self.assertIsNone(self.guard(f"echo x > bin/tick", cwd=root))

    def test_a_run_may_still_write_everything_that_is_not_the_framework(self) -> None:
        root = str(self.root.resolve())
        for path in (f"{root}/NOTIFY.md", f"{root}/.opencode/node_modules/x.js",
                     f"{root}/projects/demo/tasks/ready/DM-001.md",
                     "/tmp/somewhere/else/bin/thing"):
            with self.subTest(path=path):
                self.assertIsNone(
                    self.guard_input("Write", {"file_path": path, "content": "x"},
                                     marker=True))

    def test_a_run_may_read_the_framework_and_call_the_cli(self) -> None:
        """The agent closes its own task with bin/task; denying that strands it."""
        root = str(self.root.resolve())
        for command in ("cat bin/tick", "bin/task done DM-001 -m x",
                        f'"{root}/bin/task" done DM-001 -m x',
                        "python3 -m unittest discover tests",
                        "grep -rn tick lib/"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command, cwd=root, marker=True))

    def test_the_shell_route_into_the_framework_is_closed_too(self) -> None:
        root = str(self.root.resolve())
        for command in ("echo x > bin/tick", "echo x >> AGENTS.md",
                        'sed -i "" s/a/b/ lib/tasklib.py',
                        "tee prompts/tick.md < /tmp/x",
                        "cp /tmp/x lib/guardlib.py"):
            with self.subTest(command=command):
                self.assertEqual(self.guard(command, cwd=root, marker=True),
                                 "framework-write-shell")

    def test_forced_push_is_denied(self) -> None:
        self.assertEqual(self.guard("git push --" + "force origin main"), "force-push")
        self.assertIsNone(self.guard("git push origin main"))

    def test_unscoped_delete_is_denied_and_a_scoped_one_is_not(self) -> None:
        deny = self.DELETE
        self.assertEqual(self.guard(f'psql -c "{deny} FROM users"'), "unscoped-delete")
        self.assertEqual(self.guard(f'psql -c "{deny} FROM users;"'), "unscoped-delete")
        self.assertIsNone(self.guard(f'psql -c "{deny} FROM users WHERE id = 1"'))
        # the clause is regularly written on its own line, and that is still scoped
        self.assertIsNone(self.guard(f'psql <<SQL\n{deny} FROM users\nWHERE id = 1;\nSQL'))
        # a scoped statement elsewhere in the command does not excuse an unscoped one
        self.assertEqual(self.guard(f'psql <<SQL\n{deny} FROM a;\n{deny} FROM b WHERE x;\nSQL'),
                         "unscoped-delete")

    def test_the_board_itself_cannot_be_deleted(self) -> None:
        resolved = self.root.resolve()
        escaped = str(resolved).replace(" ", "\\ ")     # as a shell would receive it
        for target in (f"{resolved}/projects", f'"{resolved}"', escaped,
                       f'"{resolved.name}/.runtime"', resolved.name.replace(" ", "\\ ")):
            with self.subTest(target=target):
                self.assertEqual(self.guard(f"rm -rf {target}"), "tasks-dir-rewrite")
        self.assertIsNone(self.guard("rm -rf node_modules"))
        self.assertIsNone(self.guard("rm -rf /tmp/scratch/projects"))

    def test_changing_branches_is_denied(self) -> None:
        """Runs share a working copy, so the branch is not one run's to move.

        Assembled from fragments for the same reason the SQL is: a literal here
        would deny the tool call that writes this file.
        """
        switch = "git swi" + "tch "
        checkout = "git check" + "out "
        for command in (checkout + "-b feature/x", checkout + "main",
                        switch + "main", switch + "-c task/AB-1",
                        "cd /repo && " + checkout + "develop",
                        "git -C /repo " + "check" + "out develop"):
            with self.subTest(command=command):
                self.assertEqual(self.guard(command), "branch-switching")

        # prose that merely names the command is not the command: this rule is
        # matched at a command position, or writing this file would deny itself
        for command in ("echo 'is not a git " + "check" + "out'",
                        "python3 -c \"print('git " + "swi" + "tch failed')\""):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command))

        # putting a file back does not move HEAD, and stays allowed
        for command in (checkout + "-- src/app.py", "git restore src/app.py",
                        "git add -A && git commit -m fix", "git push origin main",
                        "gh pr " + "check" + "out 123"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command))

    def test_the_board_cannot_be_deleted_from_inside_itself(self) -> None:
        """The agent's working directory *is* the board, so the path can be omitted.

        `bin/tick` starts the agent with `cwd` set to the board root, which makes
        the relative form both the shortest way to destroy the board and the one a
        rule matching absolute paths never sees.
        """
        board = str(self.root.resolve())
        remove = "rm -rf "
        for target in ("projects", "./archive", ".runtime", "*", ".git"):
            with self.subTest(target=target):
                self.assertEqual(self.guard(remove + target, cwd=board),
                                 "board-relative-rm")
        # from inside a subdirectory of the board, too
        self.assertEqual(self.guard(remove + "tasks", cwd=f"{board}/projects"),
                         "board-relative-rm")
        # a board directory named as a later argument counts too
        self.assertEqual(self.guard(remove + "build .runtime", cwd=board),
                         "board-relative-rm")
        # ...but the flags of one command must not meet the argument of another
        two_commands = ("find . -name '__pycache__' -type d -exec " + remove.rstrip("f ")
                        + " {} +\n" + "rm -f .runtime/guard.log")
        self.assertIsNone(self.guard(two_commands, cwd=board))
        # but the same words elsewhere are somebody else's business: the target has
        # to *be* a board directory, not merely end with the same name
        for command, cwd in ((remove + "projects", "/tmp/other-repo"),
                             (remove + "/tmp/copy/board/.runtime", board),
                             (remove + "../other-checkout/projects", board),
                             (remove + "node_modules", board),
                             (remove + "build/ && pnpm build", board)):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command, cwd=cwd))

    def test_any_harness_can_run_the_same_check(self) -> None:
        """`bin/guard` is how an agent that is not Claude Code reaches these rules.

        The point of the CLI is that the boundary has one implementation. If this
        drifts from the hook, the board has two sets of limits and only one of
        them is the real one.
        """
        board = str(self.root.resolve())
        forbidden = "rm -rf " + "projects"
        result = subprocess.run([sys.executable, str(self.root / "bin" / "guard"),
                                 "check", "--cwd", board, forbidden],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("board-relative-rm", result.stderr)

        allowed = subprocess.run([sys.executable, str(self.root / "bin" / "guard"),
                                  "check", "--cwd", board, "pnpm test"],
                                 capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0)

        # the hook and the CLI agree, because they are the same check
        self.assertEqual(self.guard(forbidden, cwd=board), "board-relative-rm")

    def test_the_rules_are_readable_without_running_anything(self) -> None:
        result = subprocess.run([sys.executable, str(self.root / "bin" / "guard"),
                                 "rules", "--json"], capture_output=True, text=True)
        names = {rule["name"] for rule in json.loads(result.stdout)}
        self.assertIn("force-push", names)
        self.assertIn("board-relative-rm", names)

    def cli(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / "bin" / "guard"), *args],
                              input=stdin, capture_output=True, text=True)

    def denials(self) -> list[list[str]]:
        """The rows of `.runtime/guard.log`.

        A row is `stamp, rule, tool, subject`, and the subject of a shell call
        carries its own newline - the cwd sits on the first line - so the log is
        read by finding the rows rather than by counting lines.
        """
        log = self.root / ".runtime" / "guard.log"
        if not log.exists():
            return []
        return [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()
                if re.match(r"\d{4}-\d\d-\d\dT[\d:]+Z\t", line)]

    def test_another_harnesss_tool_names_hit_the_same_rules(self) -> None:
        """opencode calls its shell tool `bash`; Claude Code calls it `Bash`.

        Most of the deny list is scoped to the shell tool by name. If the name a
        harness reports is not folded onto the one the rules use, those rules
        silently stop applying to that agent - which is worse than having no
        guard, because the board looks guarded.
        """
        forbidden = "git push --" + "force origin main"
        for tool in ("Bash", "bash", "shell", "run_command"):
            with self.subTest(tool=tool):
                result = self.cli("check", "--tool", tool, forbidden)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("force-push", result.stderr)
        # a name nothing knows is left alone rather than mapped onto something
        self.assertEqual(self.cli("check", "--tool", "some_mcp_tool",
                                  forbidden).returncode, 0)

    def test_a_tool_input_can_be_larger_than_an_argument_list(self) -> None:
        """opencode's `write` carries the whole file, so the JSON goes over stdin.

        Passing it as an argument works until the day the agent writes a large
        file, and then the check fails open on exactly the call that matters.
        """
        deny = "D" + "ROP TA" + "BLE users;"
        payload = json.dumps({"filePath": "/tmp/x.sql",
                              "content": ("x " * 1_500_000) + deny})
        result = self.cli("check", "--tool", "write", "--input", "-", stdin=payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn("destructive-sql", result.stderr)

        allowed = self.cli("check", "--tool", "write", "--input", "-",
                           stdin=json.dumps({"filePath": "/tmp/x.txt", "content": "hello"}))
        self.assertEqual(allowed.returncode, 0)

    def test_the_opencode_plugin_does_not_carry_its_own_copy_of_the_rules(self) -> None:
        """The plugin is an adapter, not a second deny list.

        A rule spelled out in JavaScript would drift from `deny.json` the first
        time a rule changed, and the board would then have two answers to the
        same question with no way to tell which one was live.
        """
        plugin = (self.root / ".opencode" / "plugin" / "guard.js").read_text(encoding="utf-8")
        self.assertIn("bin", plugin)
        self.assertIn("guard", plugin)
        rules = json.loads((self.root / ".claude" / "deny.json").read_text(encoding="utf-8"))
        for rule in rules["rules"]:
            with self.subTest(rule=rule["name"]):
                self.assertNotIn(rule["pattern"], plugin)

    @unittest.skipUnless(shutil.which("node"), "no node to run the plugin outside opencode")
    def test_the_opencode_plugin_refuses_the_call_and_records_it(self) -> None:
        """The hook opencode actually calls, driven directly.

        opencode runs plugins under Bun; this runs the same module under node,
        which is enough because the plugin uses nothing but node built-ins. What
        it proves is the contract: `tool.execute.before` throws with the rule's
        own reason, and the denial lands in the same log the Claude Code path
        writes to.
        """
        board = str(self.root.resolve())
        driver = self.root / "drive-plugin.mjs"
        driver.write_text(
            "const { guard } = await import(%s)\n"
            "const hooks = await guard({ directory: %s, worktree: %s })\n"
            "try {\n"
            "  await hooks['tool.execute.before'](\n"
            "    { tool: process.argv[2], sessionID: 's', callID: 'c' },\n"
            "    { args: JSON.parse(process.argv[3]) })\n"
            "  console.log('ALLOWED')\n"
            "} catch (e) { console.log('DENIED ' + e.message) }\n"
            % (json.dumps(str(self.root / ".opencode" / "plugin" / "guard.js")),
               json.dumps(board), json.dumps(board)),
            encoding="utf-8")

        def drive(tool: str, args: dict) -> str:
            result = subprocess.run(["node", str(driver), tool, json.dumps(args)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        verdict = drive("bash", {"command": "git push --" + "force origin main"})
        self.assertTrue(verdict.startswith("DENIED"), verdict)
        self.assertIn("force-push", verdict)
        # the rule's own words reach the agent, not a generic failure
        self.assertIn("Land the change as a normal commit", verdict)

        self.assertEqual(drive("bash", {"command": "pnpm test"}), "ALLOWED")

        # ... and the refusal is in the log, under the name the harness used, so
        # an audit says which agent hit the rule rather than guessing
        denied = [row for row in self.denials() if row[1] == "force-push"]
        self.assertEqual(len(denied), 1, self.denials())
        self.assertEqual(denied[0][2], "bash")

    @unittest.skipUnless(shutil.which("node"), "no node to run the plugin outside opencode")
    def test_the_opencode_plugin_fails_closed_when_the_guard_is_missing(self) -> None:
        """No boundary means no tool call, loudly.

        The alternative is an unattended agent running with the deny list
        silently switched off because a file moved.
        """
        (self.root / "bin" / "guard").unlink()
        driver = self.root / "drive-missing.mjs"
        driver.write_text(
            "const { guard } = await import(%s)\n"
            "const hooks = await guard({ directory: %s, worktree: %s })\n"
            "try {\n"
            "  await hooks['tool.execute.before']({ tool: 'bash', sessionID: 's', callID: 'c' },\n"
            "    { args: { command: 'ls' } })\n"
            "  console.log('ALLOWED')\n"
            "} catch (e) { console.log('DENIED ' + e.message) }\n"
            % (json.dumps(str(self.root / ".opencode" / "plugin" / "guard.js")),
               json.dumps(str(self.root.resolve())), json.dumps(str(self.root.resolve()))),
            encoding="utf-8")
        result = subprocess.run(["node", str(driver)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard-unavailable", result.stdout)

    def test_ordinary_work_is_not_denied(self) -> None:
        for command in ("pnpm test && git commit -m fix",
                        "git push origin main",
                        "python3 -m unittest discover tests",
                        "rm -rf build/"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command))


class TestWorkedByHand(BoardTestCase):
    """Work reserved for a human is invisible to the loop, not merely discouraged.

    Both rules lived in prose before they were checks - `owner: human` was
    recorded and displayed but never consulted, and a project.md saying "the loop
    must not pick these up" was honoured only for as long as an agent read it and
    agreed. The second one is the dangerous half: the tasks it guards are the ones
    that rewrite `bin/tick`, and the loop that would have picked them up is the
    one running `bin/tick` at the time.
    """

    def setUp(self) -> None:
        super().setUp()
        self.new_project()
        self.tl = self.load_tasklib()

    def reasons(self, task_id: str) -> list[str]:
        task = self.tl.find_task(task_id)
        return self.tl.blocking_reasons(task)

    def test_a_human_owned_task_is_never_eligible(self) -> None:
        task_id = self.new_task("Decide the pricing model", "--owner", "human",
                                "--ready")
        self.assertIn("owned by a human", self.reasons(task_id))
        self.assertEqual([], [t.id for t in self.tl.eligible_tasks()])

    def test_an_agent_owned_task_in_the_same_project_still_runs(self) -> None:
        task_id = self.new_task("Ordinary work", "--ready")
        self.assertEqual([], self.reasons(task_id))
        self.assertEqual([task_id], [t.id for t in self.tl.eligible_tasks()])

    def test_reserved_work_cannot_be_put_in_the_loop_s_queue(self) -> None:
        """`ready` means available to the loop, and this work never is."""
        task_id = self.new_task("Decide the pricing model", "--owner", "human")
        result = subprocess.run(
            [sys.executable, str(self.root / "bin" / "task"), "ready", task_id],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("owned by a human", result.stderr)
        self.assertEqual("backlog", self.summary(task_id)["status"])

        self.task("ready", task_id, "--force")
        self.assertEqual("ready", self.summary(task_id)["status"],
                         "--force is the door for a human who means it")

    def test_a_manual_project_takes_its_whole_board_off_the_loop(self) -> None:
        task_id = self.new_task("Rewrite the wrapper", "--ready")
        project_file = self.root / "projects" / "demo" / "project.md"
        project_file.write_text(
            project_file.read_text(encoding="utf-8").replace(
                "name: demo", "name: demo\nmanual: true", 1),
            encoding="utf-8")
        self.tl.project_meta(project_file.parent, refresh=True)
        self.assertIn("demo is worked by hand", self.reasons(task_id))
        self.assertEqual([], [t.id for t in self.tl.eligible_tasks()])


class TestEmbeddedBoard(unittest.TestCase):
    """A board vendored into somebody else's repository as `.tasks/`.

    The shape an agent actually wants: handed one directory it sees the code and
    the tasks about that code together. Everything here is about the seam
    between the two - what the board may touch in the repository around it, and
    what it may not.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # A space in the path, as everywhere else here: it is where quoting bugs live.
        self.repo = Path(self._tmp.name) / "my app"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.git("add", "-A")
        self.git("commit", "-qm", "the code")

        self.board = self.repo / ".tasks"
        self.board.mkdir()
        for item in ("bin", "lib", "prompts", "scheduler", ".claude"):
            shutil.copytree(SOURCE_ROOT / item, self.board / item,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        for item in ("AGENTS.md", "CLAUDE.md", "opencode.json"):
            shutil.copy(SOURCE_ROOT / item, self.board / item)
        (self.board / "config.json").write_text(json.dumps({
            "notify_desktop": False, "notify_file": False, "git_autocommit": True,
        }), encoding="utf-8")
        (self.board / ".gitignore").write_text(".runtime/\n__pycache__/\n", encoding="utf-8")

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True)

    def task(self, *args: str) -> str:
        result = subprocess.run([sys.executable, str(self.board / "bin" / "task"), *args],
                                capture_output=True, text=True, cwd=str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def tasklib(self):
        spec = importlib.util.spec_from_file_location(
            f"tl_{id(self)}", self.board / "lib" / "tasklib.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_init_makes_the_board_root_the_one_project(self) -> None:
        out = self.task("init")
        self.assertIn("my app", out)
        self.assertTrue((self.board / "project.md").exists())
        self.assertTrue((self.board / "tasks" / "ready").is_dir())
        self.assertFalse((self.board / "projects").exists(),
                         "one project needs no directory to tell several apart")

    def test_a_task_needs_no_project_named(self) -> None:
        """`--project` names a decision. With one project there is none to make."""
        self.task("init")
        out = self.task("new", "--title", "Retry failed deliveries", "--type", "bug")
        # `my app` is two words, so the prefix is its initials. A one-word name
        # takes three letters instead - `M-001` is not an id anyone recognises
        # in a commit message six months later.
        self.assertTrue(out.startswith("MA-001 "), out)

    def test_repo_path_is_relative_and_resolves_against_the_board(self) -> None:
        """`..`, not an absolute path: the repository will be cloned elsewhere.

        `project.md` is committed with the code, so an absolute path in it is a
        path on the machine that ran the installer and nowhere else.
        """
        self.task("init")
        meta, _ = self.tasklib().read_frontmatter(self.board / "project.md")
        self.assertEqual(meta["repo_path"], "..")
        self.assertEqual(self.tasklib().resolve_repo("..").resolve(), self.repo.resolve())
        self.assertIn("no invariant violations", self.task("doctor"))

    def test_the_projects_root_is_derived_and_not_one_machine_s_guess(self) -> None:
        """It shipped as `~/Documents/Projects`, which is true of one machine.

        Only the grooming prompt reads it, and only to say where to look — but a
        default that is a fact about the author's laptop tells every other board
        something untrue about itself, in a prompt an agent is asked to believe.
        """
        self.task("init")
        self.assertEqual(self.tasklib().projects_root(), str(self.repo.resolve()))

    def test_the_board_commits_itself_and_never_the_code_around_it(self) -> None:
        """The one property that makes vendoring safe at all.

        The loop commits the board after every tick. Standalone that is the
        whole repository and correct. Embedded, `git add -A` would sign the
        human's half-finished work into a commit about somebody's task and bury
        it there - so the commit carries a pathspec, and `.` under `-C ROOT`
        means the board and stops at its edge.
        """
        self.task("init")
        self.task("new", "--title", "Retry failed deliveries", "--type", "bug")

        # Work in flight in the repository around the board, in both states it
        # can be in when a tick fires.
        (self.repo / "src" / "wip.py").write_text("half finished\n", encoding="utf-8")
        self.git("add", "src/wip.py")
        (self.repo / "src" / "app.py").write_text("print('edited')\n", encoding="utf-8")

        tl = self.tasklib()
        self.assertTrue(tl.is_embedded())
        self.assertTrue(tl.git_commit("board: a tick"))

        committed = self.git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertTrue(committed, "the tick has to have committed the board")
        self.assertTrue(all(f.startswith(".tasks/") for f in committed),
                        f"the board committed something outside itself: {committed}")

        status = dict((line[3:], line[:2]) for line in
                      self.git("status", "--porcelain").stdout.splitlines())
        self.assertEqual(status.get("src/wip.py"), "A ", "a staged file stays staged")
        self.assertEqual(status.get("src/app.py"), " M", "an edit stays uncommitted")

    def test_a_whole_tick_runs_inside_a_repository(self) -> None:
        """The end-to-end claim: ready task in, done task and a commit out.

        Everything else here tests one seam. This runs the wrapper the way the
        scheduler does, with a stand-in agent, and asks only what a person would
        ask -- did the work happen, and is the repository still in one piece.
        """
        self.task("init")
        # Outside the repository on purpose. Written inside it, the stub would
        # be an untracked file at the end of the run - which is the board
        # behaving correctly, and a confusing way for this test to fail.
        agent = Path(self._tmp.name) / "closing-agent"
        board = self.board
        agent.write_text(
            "#!/bin/sh\n"
            f'id=$("{board}/bin/task" list --status in-progress | awk \'{{print $1}}\' | head -1)\n'
            'echo "working $id"\n'
            # The agent's own work, in the repository around the board, committed
            # by path exactly as AGENTS.md requires of it.
            f'echo "rate limited" > "{self.repo}/src/limits.py"\n'
            f'git -C "{self.repo}" add src/limits.py\n'
            f'git -C "{self.repo}" commit -qm "add the limiter" >/dev/null\n'
            f'"{board}/bin/task" done "$id" -m "done by the stand-in"'
            ' -d "the stand-in agent closed this and decided nothing at all" >/dev/null\n',
            encoding="utf-8")
        agent.chmod(0o755)

        config = json.loads((board / "config.json").read_text())
        config.update({"agent": "closing", "git_autocommit": True,
                       "agents": {"closing": {"command": [str(agent)], "prompt": "arg",
                                              "workdir_flag": None, "stream": "text"}}})
        (board / "config.json").write_text(json.dumps(config), encoding="utf-8")

        task_id = self.task("new", "--title", "Add a rate limiter",
                            "--ready", "--force").split()[0]
        tick = subprocess.run([sys.executable, str(board / "bin" / "tick")],
                              capture_output=True, text=True, cwd=str(self.repo))
        self.assertEqual(tick.returncode, 0, tick.stdout + tick.stderr)

        moved = list((board / "tasks" / "done").glob(f"{task_id}-*.md"))
        self.assertEqual(len(moved), 1, f"{task_id} should be done:\n{tick.stdout}")
        self.assertTrue((self.repo / "src" / "limits.py").exists(),
                        "the agent's actual work has to be there")

        # Nothing left behind, in either half of the repository.
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "",
                         "a finished tick leaves a clean tree")
        subjects = self.git("log", "--format=%s").stdout.splitlines()
        self.assertIn("add the limiter", subjects, "the agent's commit")
        self.assertTrue(any(task_id in line for line in subjects),
                        f"and the board's own, naming the task: {subjects}")

    def test_a_standalone_board_still_commits_everything(self) -> None:
        """The same pathspec, one directory up, means the whole board.

        Worth asserting rather than assuming: the embedded fix would be a
        regression if it had quietly narrowed what a standalone board commits.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = build_board(Path(tmp.name))
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
        (root / "config.json").write_text(json.dumps({
            "notify_desktop": False, "notify_file": False, "git_autocommit": True,
        }), encoding="utf-8")

        spec = importlib.util.spec_from_file_location(
            f"tl_alone_{id(self)}", root / "lib" / "tasklib.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        self.assertFalse(module.is_embedded())
        self.assertTrue(module.git_commit("init"))
        committed = subprocess.run(["git", "-C", str(root), "show", "--name-only",
                                    "--format=", "HEAD"], capture_output=True,
                                   text=True).stdout.split()
        self.assertIn("AGENTS.md", committed)
        self.assertIn("bin/task", committed)


class TestCheckout(unittest.TestCase):
    """What arrives when somebody clones this, on a machine that is not this one."""

    def test_a_windows_clone_still_has_runnable_entry_points(self) -> None:
        """core.autocrlf=true is the default of Git for Windows, not an exotic setting.

        Without `.gitattributes` every text file in such a clone gets CRLF, and
        the carriage return becomes part of the interpreter's name:

            $ ./bin/task doctor
            env: python3\r: No such file or directory

        `sh install.sh` fails the same way, one line at a time. The board's whole
        Windows story - a .cmd shim and a portable library - is delivered by the
        checkout that would have broken it.
        """
        if not (SOURCE_ROOT / ".git").exists():
            self.skipTest("not a git checkout, so there is nothing to clone")
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "as on windows"
            result = subprocess.run(
                ["git", "clone", "-q", "--config", "core.autocrlf=true",
                 str(SOURCE_ROOT), str(clone)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

            for name in ("bin/task", "bin/tick", "install.sh"):
                first = (clone / name).read_bytes().split(b"\n")[0]
                self.assertFalse(first.endswith(b"\r"),
                                 f"{name} was checked out with CRLF")

            doctor = subprocess.run([sys.executable, str(clone / "bin" / "task"), "doctor"],
                                    capture_output=True, text=True)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)

            # And the two files Windows itself runs keep Windows' endings.
            for name in ("bin/task.cmd", "install.ps1"):
                self.assertIn(b"\r\n", (clone / name).read_bytes(),
                              f"{name} should be CRLF")


class TestInstaller(unittest.TestCase):
    """Vendoring a board into a repository, and doing it a second time.

    The second time is the interesting one. An installer that is not safe to
    re-run is not an upgrade path, and without an upgrade path a vendored board
    is a fork of the framework the day after it is installed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "my app"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        for args in (("init", "-q"), ("config", "user.email", "t@e.com"),
                     ("config", "user.name", "T"), ("add", "-A"),
                     ("commit", "-qm", "the code")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                           capture_output=True)

    def install(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "bin" / "install"),
             "--source", str(SOURCE_ROOT), "--into", str(self.repo), *args],
            capture_output=True, text=True)

    def test_it_installs_a_working_board(self) -> None:
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        board = self.repo / ".tasks"
        for expected in ("bin/task", "lib/tasklib.py", "AGENTS.md", "config.json",
                         ".gitignore", "project.md", "tasks/ready"):
            self.assertTrue((board / expected).exists(), f"{expected} is missing")

        made = subprocess.run(
            [sys.executable, str(board / "bin" / "task"), "new",
             "--title", "Add rate limiting", "--type", "feature"],
            capture_output=True, text=True, cwd=str(self.repo))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertTrue(made.stdout.startswith("MA-001 "), made.stdout)

    def test_the_vendored_board_is_a_board(self) -> None:
        """Whatever it ships has to be enough to be one, including the tests.

        Found by running the suite from inside an installed copy: three tests
        failed there and passed here, because the installer's own .gitignore and
        its file list were not the ones the board is written against. A board
        that cannot check itself where it has been installed is a board nobody
        can check.
        """
        if os.environ.get("PROJECT_TASKS_NESTED_SUITE"):
            self.skipTest("already inside the nested run this test starts")
        self.install()
        board = self.repo / ".tasks"

        ignored = (board / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("index.db", ignored,
                      "the derived index must be named, not merely covered")

        for name in ("install.sh", "install.ps1", "tests/test_board.py"):
            self.assertTrue((board / name).exists(),
                            f"a vendored board ships {name}")

        # The nested run is told it is nested, and this test excuses itself
        # there. Without that it discovers itself, installs another board, runs
        # the suite again, and recurses until something gives out.
        env = dict(os.environ, PROJECT_TASKS_NESTED_SUITE="1")
        suite = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", str(board / "tests")],
            capture_output=True, text=True, cwd=str(self.repo), env=env)
        self.assertEqual(suite.returncode, 0, suite.stderr[-4000:])

    def test_the_entry_points_are_executable(self) -> None:
        """A tarball can arrive without permission bits, and then nothing runs."""
        self.install()
        for name in ("task", "tick", "board", "guard"):
            path = self.repo / ".tasks" / "bin" / name
            self.assertTrue(os.access(path, os.X_OK), f"bin/{name} is not executable")

    def test_it_never_replaces_the_repository_s_own_rules(self) -> None:
        rules = self.repo / "AGENTS.md"
        rules.write_text("# My app\n\nRun the linter before committing.\n", encoding="utf-8")
        self.install()
        text = rules.read_text(encoding="utf-8")
        self.assertIn("Run the linter before committing.", text,
                      "the human's own rules must survive an install")
        self.assertIn(".tasks/bin/task", text, "and the pointer must be added")

    def test_installing_twice_changes_nothing_the_second_time(self) -> None:
        self.install()
        (self.repo / ".tasks" / "config.json").write_text(
            json.dumps({"max_concurrent_tasks": 3}), encoding="utf-8")
        made = subprocess.run(
            [sys.executable, str(self.repo / ".tasks" / "bin" / "task"), "new",
             "--title", "Work that must survive", "--type", "chore"],
            capture_output=True, text=True, cwd=str(self.repo))
        self.assertEqual(made.returncode, 0, made.stderr)

        second = self.install()
        self.assertEqual(second.returncode, 0, second.stderr)

        # Settings belong to the copy that has them, and the work is the whole
        # point of the board. Neither is the installer's to replace.
        self.assertEqual(json.loads((self.repo / ".tasks" / "config.json")
                                    .read_text())["max_concurrent_tasks"], 3)
        self.assertEqual(len(list((self.repo / ".tasks" / "tasks" / "backlog")
                                  .glob("*.md"))), 1)
        self.assertEqual((self.repo / "AGENTS.md").read_text(encoding="utf-8")
                         .count("project-tasks:begin"), 1,
                         "a second install must refresh the pointer, not add another")

        check = self.install("--check")
        self.assertEqual(check.returncode, 0,
                         f"nothing should have drifted:\n{check.stdout}")

    def test_the_guard_is_wired_into_the_repository(self) -> None:
        """Claude Code reads settings from the repository, not from `.tasks/`."""
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"MINE": "1"}}), encoding="utf-8")
        self.install()

        settings = json.loads((self.repo / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["env"]["MINE"], "1", "merged, never overwritten")
        commands = [h["command"] for entry in settings["hooks"]["PreToolUse"]
                    for h in entry["hooks"]]
        self.assertTrue(any(".tasks/.claude/hooks/guard.py" in c for c in commands))

        self.install()
        settings = json.loads((self.repo / ".claude" / "settings.json").read_text())
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1,
                         "a second install must not register the guard twice")

    def test_unparseable_settings_are_left_alone(self) -> None:
        """Rewriting a file we could not read would throw away what was in it."""
        (self.repo / ".claude").mkdir()
        broken = self.repo / ".claude" / "settings.json"
        broken.write_text("{ this is not json", encoding="utf-8")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(broken.read_text(encoding="utf-8"), "{ this is not json")
        self.assertIn("add the hook by hand", result.stdout)

    def test_it_refuses_a_source_that_is_not_a_board(self) -> None:
        empty = Path(self._tmp.name) / "not a board"
        empty.mkdir()
        result = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "bin" / "install"),
             "--source", str(empty), "--into", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not look like a board", result.stderr)

    def test_the_shell_bootstrap_installs_from_a_local_checkout(self) -> None:
        """install.sh is two lines of bootstrap; this is the half that can be run here."""
        result = subprocess.run(
            ["sh", str(SOURCE_ROOT / "install.sh"),
             "--from", str(SOURCE_ROOT), "--into", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".tasks" / "bin" / "task").exists())

    def test_the_bootstrap_uses_the_checkout_it_is_sitting_in(self) -> None:
        """With no --from and no network: the board around the script.

        This is the whole flow for a private repository, where an
        unauthenticated download gets a 404 that says nothing about why.
        """
        env = dict(os.environ)
        env.pop("PROJECT_TASKS_REPO", None)
        result = subprocess.run(["sh", str(SOURCE_ROOT / "install.sh"),
                                 "--into", str(self.repo)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".tasks" / "bin" / "task").exists())
        self.assertNotIn("cloning", result.stdout)
        self.assertNotIn("fetching", result.stdout)

    def test_the_bootstrap_clones_a_repository_and_drops_its_git(self) -> None:
        """What goes into somebody's repository is files, not a repository in one."""
        if not (SOURCE_ROOT / ".git").exists():
            self.skipTest("not a git checkout, so there is nothing to clone")
        result = subprocess.run(
            ["sh", str(SOURCE_ROOT / "install.sh"),
             "--from", f"file://{SOURCE_ROOT}/.git", "--into", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cloning", result.stdout)
        self.assertTrue((self.repo / ".tasks" / "bin" / "task").exists())
        self.assertFalse((self.repo / ".tasks" / ".git").exists(),
                         "a board must not carry a git repository into another one")

    def test_the_bootstrap_says_so_when_there_is_nothing_to_install_from(self) -> None:
        """Copied somewhere on its own, with no checkout and no address."""
        lonely = Path(self._tmp.name) / "on its own"
        lonely.mkdir()
        shutil.copy(SOURCE_ROOT / "install.sh", lonely / "install.sh")
        env = dict(os.environ)
        env.pop("PROJECT_TASKS_REPO", None)
        result = subprocess.run(["sh", str(lonely / "install.sh"),
                                 "--into", str(self.repo)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing to install from", result.stderr)


class TestLoopctl(BoardTestCase):
    """The scheduler front end, which is the one part that differs per OS."""

    def loopctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / "bin" / "loopctl"), *args],
                              capture_output=True, text=True)

    def test_status_sees_a_run_in_flight(self) -> None:
        """It read `.runtime/worker.json` for a long time after nothing wrote it.

        The single worker file belongs to the board that could only work one
        task; the moment runs became one-per-resource they moved to
        `.runtime/workers/<generation>.json`, and `status` went on reporting an
        idle board however many agents were running.
        """
        workers = self.root / ".runtime" / "workers"
        workers.mkdir(parents=True, exist_ok=True)
        (workers / "7.json").write_text(json.dumps({
            "pid": os.getpid(), "task_id": "DM-041", "events": 12}), encoding="utf-8")

        out = self.loopctl("status").stdout
        self.assertIn("DM-041", out, "a live run has to appear in status")
        self.assertNotIn("worker:  idle", out)

    def test_a_dead_run_is_not_reported_as_working(self) -> None:
        workers = self.root / ".runtime" / "workers"
        workers.mkdir(parents=True, exist_ok=True)
        # A pid no longer in use. Left behind by a run that was killed, which is
        # the normal way these files end - nothing deletes them on the way out.
        (workers / "7.json").write_text(json.dumps({
            "pid": 999999, "task_id": "DM-041", "events": 12}), encoding="utf-8")

        self.assertIn("worker:  idle", self.loopctl("status").stdout)

    def test_the_rendered_job_has_no_placeholders_left(self) -> None:
        result = self.loopctl("unit")
        if result.returncode != 0:
            self.skipTest("this platform has no unattended scheduler")
        self.assertNotIn("{{", result.stdout, "every placeholder must be substituted")
        self.assertIn(str(self.root), result.stdout, "the job has to name this board")

    def test_stop_and_start_are_the_two_brakes(self) -> None:
        hold = self.root / ".runtime" / "usage_limit.json"
        hold.parent.mkdir(parents=True, exist_ok=True)
        hold.write_text("{}", encoding="utf-8")

        self.loopctl("stop")
        self.assertTrue((self.root / "STOP").exists())

        # `start` clears both, because having to remember which of two commands
        # lifts which brake is a worse interface than clearing both.
        self.loopctl("start")
        self.assertFalse((self.root / "STOP").exists())
        self.assertFalse(hold.exists())


class TestGroomGate(TickTestCase):
    """Grooming costs an agent run, so a drained board must not pay for one.

    The board was idle for a day and bought twenty-four agents to be told there
    was nothing to do, because the only question asked was how long it had been
    since the last grooming.
    """

    def setUp(self) -> None:
        super().setUp()
        config = json.loads((self.root / "config.json").read_text())
        config["groom_interval_sec"] = 0      # the interval is not what is under test
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.load_tick()
        self.new_project()

    def test_a_drained_board_that_has_not_changed_does_not_groom(self) -> None:
        self.tick.write_last_groom()
        groom, why = self.tick.should_groom()
        self.assertFalse(groom, why)
        self.assertIn("nothing has changed", why)

    def test_a_ready_task_is_worth_grooming_for(self) -> None:
        self.tick.write_last_groom()
        self.task("new", "--project", "demo", "--title", "Something", "--ready",
                  "--force")
        groom, why = self.tick.should_groom()
        self.assertTrue(groom, why)
        self.assertIn("lint", why)

    def test_a_promotable_backlog_task_is_worth_grooming_for(self) -> None:
        self.tick.write_last_groom()
        self.new_task("Waiting in the backlog")
        groom, why = self.tick.should_groom()
        self.assertTrue(groom, why)
        self.assertIn("could move to ready", why)

    def test_work_no_one_can_promote_is_not_a_reason_on_its_own(self) -> None:
        """A scheduled task is on the board but nothing about it is actionable."""
        self.new_task("Not until December", "--after", "2099-01-01T00:00:00Z")
        self.tick.write_last_groom()
        groom, why = self.tick.should_groom()
        self.assertFalse(groom, why)
        self.assertIn("nothing has changed", why)

    def test_a_board_that_changed_grooms_even_with_nothing_promotable(self) -> None:
        """New tasks can duplicate old ones, and only an agent sees that."""
        self.tick.write_last_groom()
        self.new_task("Also not until December", "--after", "2099-01-01T00:00:00Z")
        groom, why = self.tick.should_groom()
        self.assertTrue(groom, why)
        self.assertIn("changed", why)

    def test_a_target_the_last_run_already_saw_is_not_a_reason(self) -> None:
        """The hole a fingerprint alone leaves open.

        A `ready` task whose dependency is still open is a target every hour for
        as long as the dependency stays open, and no file changes while it waits.
        Grooming it again re-reads a board the last run already decided about.
        """
        blocker = self.new_task("The blocker")
        waiting = self.task("new", "--project", "demo", "--title", "Waiting",
                            "--ready", "--force", "--after", blocker).split()[0]
        self.assertIn("ready tasks to lint", self.tick.groom_targets())
        self.tick.write_last_groom()
        groom, why = self.tick.should_groom()
        self.assertFalse(groom, f"{waiting} groomed twice for the same reason: {why}")
        self.assertIn("nothing has changed", why)

    def test_a_target_that_appears_is_a_reason(self) -> None:
        self.tick.write_last_groom()
        self.task("new", "--project", "demo", "--title", "Something", "--ready",
                  "--force")
        groom, why = self.tick.should_groom()
        self.assertTrue(groom, why)

    def test_the_timestamp_format_before_this_still_reads(self) -> None:
        """A board upgraded mid-flight has a bare float in .runtime/last_groom."""
        self.tick.LAST_GROOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.tick.LAST_GROOM_FILE.write_text(str(time.time()), encoding="utf-8")
        at, seen = self.tick.read_last_groom()
        self.assertGreater(at, 0)
        self.assertIsNone(seen)
        groom, why = self.tick.should_groom()
        self.assertTrue(groom, why)
        self.assertIn("no record", why)

    def test_a_half_written_record_is_read_as_no_record(self) -> None:
        """The format between the bare float and this one carried no targets."""
        self.tick.LAST_GROOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.tick.LAST_GROOM_FILE.write_text(
            json.dumps({"at": time.time(), "fingerprint": "whatever"}),
            encoding="utf-8")
        at, seen = self.tick.read_last_groom()
        self.assertGreater(at, 0)
        self.assertIsNone(seen)


if __name__ == "__main__":
    unittest.main()
