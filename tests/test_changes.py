"""Behavioral checks for comparisons, private state, and successful-run commits."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from canvas_agent.changes import (
    compare,
    load_snapshot,
    observe,
    render_changes,
    snapshot_path,
)
from canvas_agent.cli import main
from canvas_agent.client import CanvasClient, CanvasError
from canvas_agent.content import demo_course_content, prepare_course_content

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def assignment(**overrides):
    return {
        "id": 1,
        "course_id": 101,
        "course_name": "Example",
        "name": "Report",
        "description": "Write a report",
        "due_at": "2026-12-01T12:00:00Z",
        "html_url": "https://school.example/courses/101/assignments/1",
        **overrides,
    }


class ChangeTests(unittest.TestCase):
    def test_first_observation_is_a_baseline_then_unchanged_has_no_events(self):
        current = observe([assignment()], [], NOW)
        report, state = compare(current, None)
        self.assertTrue(report["baseline"])
        self.assertEqual(report["events"], [])
        report, _ = compare(current, state)
        self.assertFalse(report["baseline"])
        self.assertEqual(report["events"], [])

    def test_new_assignments_and_content_changes_are_distinct(self):
        old = observe([assignment()], [], NOW)
        current = observe(
            [assignment(description="Updated instructions"), assignment(id=2)], [], NOW
        )
        report, _ = compare(current, old)
        self.assertEqual([e["kind"] for e in report["events"]], ["updated", "new"])

    def test_personal_deadline_overrides_course_deadline_including_null(self):
        old = observe(
            [assignment(submission={"cached_due_date": "2026-12-03T12:00:00Z"})],
            [],
            NOW,
        )
        new = observe([assignment(submission={"cached_due_date": None})], [], NOW)
        report, _ = compare(new, old)
        (event,) = report["events"]
        self.assertEqual(event["kind"], "deadline_changed")
        self.assertEqual(event["before"], "2026-12-03T12:00:00+00:00")
        self.assertIsNone(event["after"])
        markdown = render_changes(report, ZoneInfo("Europe/Stockholm"))
        self.assertIn("2026-12-03 13:00 CET", markdown)
        self.assertIn("No due date", markdown)

    def test_equivalent_deadline_offsets_and_submission_changes_are_ignored(self):
        old = observe([assignment()], [], NOW)
        new = observe(
            [
                assignment(
                    due_at="2026-12-01T13:00:00+01:00",
                    submission={"workflow_state": "graded"},
                )
            ],
            [],
            NOW,
        )
        self.assertEqual(compare(new, old)[0]["events"], [])

    def test_pages_and_file_versions_are_tracked_not_cache_status(self):
        courses = prepare_course_content(
            demo_course_content(), "https://school.example"
        )
        courses[0]["files"] = [
            {
                "id": 10,
                "name": "Lecture.pdf",
                "url": "https://school.example/files/10",
                "status": "ok",
                "updated_at": "2026-11-01",
                "cached": False,
            }
        ]
        old = observe([], courses, NOW)
        courses[0]["files"][0]["cached"] = True
        self.assertEqual(compare(observe([], courses, NOW), old)[0]["events"], [])
        courses[0]["files"][0]["updated_at"] = "2026-11-17"
        courses[0]["pages"][0]["body"]["html"] = "New study material"
        report, _ = compare(observe([], courses, NOW), old)
        self.assertEqual(
            {event["resource"] for event in report["events"]}, {"page", "file"}
        )

    def test_temporarily_missing_resources_keep_their_last_observation(self):
        old = observe([assignment()], [], NOW)
        report, retained = compare(observe([], [], NOW), old)
        self.assertEqual(report["events"], [])
        self.assertEqual(compare(old, retained)[0]["events"], [])

    def test_state_contains_fingerprints_not_assignment_bodies(self):
        state = observe([assignment(description="Private course text")], [], NOW)
        self.assertNotIn("Private course text", json.dumps(state))

    def test_accounts_course_selections_and_modes_have_separate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = snapshot_path(
                root, "account-a", [101, 102], course_content=True, files=True
            )
            self.assertEqual(
                path,
                snapshot_path(
                    root, "account-a", [102, 101], course_content=True, files=True
                ),
            )
            for namespace, ids, content, files in [
                ("account-b", [101, 102], True, True),
                ("account-a", [101], True, True),
                ("account-a", [101, 102], True, False),
                ("account-a", [101, 102], False, False),
            ]:
                self.assertNotEqual(
                    path,
                    snapshot_path(
                        root, namespace, ids, course_content=content, files=files
                    ),
                )
        self.assertNotEqual(
            CanvasClient("https://school.example", "a").cache_namespace,
            CanvasClient("https://school.example", "b").cache_namespace,
        )

    def test_corrupt_or_incompatible_state_needs_new_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertIsNone(load_snapshot(path))
            for text in (
                "{broken",
                "[]",
                '{"schema_version":99}',
                '{"schema_version":1,"checked_at":"now","items":{"x":null}}',
            ):
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(load_snapshot(path))


class ChangeCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state_dir = self.root / "state"
        self.output = self.root / "export.json"
        self.config = self.root / "config.toml"
        self.config.write_text(
            '[canvas]\nbase_url="https://school.example"\ntoken="test-token"\ncourse_ids=[101]\n',
            encoding="utf-8",
        )
        self.args = [
            "--config",
            str(self.config),
            "--changes",
            "--state-dir",
            str(self.state_dir),
            "--format",
            "json",
            "--output",
            str(self.output),
        ]

    def run_export(self, assignments):
        with (
            patch(
                "canvas_agent.cli.CanvasClient.assignments",
                return_value=copy.deepcopy(assignments),
            ),
            redirect_stderr(io.StringIO()),
        ):
            return main(self.args)

    def test_two_successful_runs_report_changes_even_outside_planning_horizon(self):
        self.assertEqual(
            self.run_export([assignment(due_at="2090-01-01T00:00:00Z")]), 0
        )
        data = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(data["assignments"], [])
        self.assertTrue(data["changes"]["baseline"])
        self.assertEqual(
            self.run_export([assignment(due_at="2090-01-02T00:00:00Z")]), 0
        )
        data = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(data["changes"]["events"][0]["kind"], "deadline_changed")
        self.assertEqual(
            self.run_export([assignment(due_at="2090-01-02T00:00:00Z")]), 0
        )
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8"))["changes"]["events"], []
        )

    def test_network_or_output_failure_does_not_advance_baseline(self):
        self.run_export([assignment()])
        (path,) = self.state_dir.glob("*.json")
        before = path.read_bytes()
        for target, error in (
            ("canvas_agent.cli.CanvasClient.assignments", CanvasError("Offline")),
            ("canvas_agent.cli.write_output", OSError("Read only")),
        ):
            with (
                patch(
                    "canvas_agent.cli.CanvasClient.assignments",
                    return_value=[assignment(name="Changed")],
                ),
                patch(target, side_effect=error),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(self.args), 1)
            self.assertEqual(path.read_bytes(), before)

    def test_demo_creates_no_comparison_state(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["--demo", "--changes", "--state-dir", str(self.state_dir)]), 0
            )
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
