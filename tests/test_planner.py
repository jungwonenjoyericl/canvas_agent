"""Behavioral tests for deadlines, filtering, and Markdown output."""

import unittest
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from canvas_agent.config import StyleSettings
from canvas_agent.planner import prepare, render_markdown


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 12, tzinfo=UTC)
        self.zone = ZoneInfo("Europe/Stockholm")

    def assignment(self, **changes):
        return {
            "id": 1,
            "course_id": 2,
            "course_name": "Math",
            "name": "Vectors",
            "due_at": "2026-11-17T23:30:00Z",
            "html_url": "https://school.example/a(1)",
            "description": "<p>Practice <b>vectors</b>.</p><script>bad()</script>",
            **changes,
        }

    def prepare(self, items, include_completed=False):
        return prepare(items, self.now, self.zone, 14, include_completed)

    def test_due_dates_use_local_timezone(self):
        item = self.prepare([self.assignment()])[0]
        self.assertEqual(item["group"], "2026-11-18")
        self.assertEqual(item["due"], "2026-11-18 00:30 CET")
        self.assertEqual(item["description"], "Practice vectors.")

    def test_filters_completed_unpublished_and_distant_assignments(self):
        items = [
            self.assignment(submission={"workflow_state": state})
            for state in ("submitted", "graded", "pending_review")
        ]
        items += [
            self.assignment(published=False),
            self.assignment(due_at="2027-01-01T12:00:00Z"),
            self.assignment(submission={"excused": True}),
        ]
        self.assertEqual(self.prepare(items), [])

    def test_includes_overdue_and_undated_and_sorts_chronologically(self):
        result = self.prepare(
            [
                self.assignment(due_at=None),
                self.assignment(),
                self.assignment(due_at="2026-11-15T12:00:00Z"),
            ]
        )
        self.assertEqual(
            [item["group"] for item in result], ["Overdue", "2026-11-18", "No due date"]
        )

    def test_uses_personal_submission_due_date_including_null(self):
        item = self.prepare([self.assignment(submission={"cached_due_date": None})])[0]
        self.assertIsNone(item["due_at"])
        item = self.prepare(
            [self.assignment(submission={"cached_due_date": "2026-11-20T12:00:00Z"})]
        )[0]
        self.assertEqual(item["group"], "2026-11-20")

    def test_horizon_includes_entire_last_local_date(self):
        items = [
            self.assignment(due_at="2026-11-30T22:59:00Z"),
            self.assignment(due_at="2026-11-30T23:00:00Z"),
        ]
        self.assertEqual(len(self.prepare(items)), 1)

    def test_completed_items_render_checked_and_template_is_customizable(self):
        items = self.prepare(
            [self.assignment(submission={"workflow_state": "submitted"})], True
        )
        style = StyleSettings(
            title="Plan", item_template="[${checkbox}] ${name}: ${due}"
        )
        text = render_markdown(items, style, self.now, self.zone)
        self.assertIn("# Plan", text)
        self.assertIn("[x] Vectors", text)

    def test_markdown_escapes_content_and_encodes_links(self):
        items = self.prepare([self.assignment(name="[click](bad)")])
        text = render_markdown(items, StyleSettings(), self.now, self.zone)
        self.assertIn(r"\[click\]\(bad\)", text)
        self.assertIn("a%281%29", text)

    def test_invalid_template_reports_configuration_error(self):
        with self.assertRaisesRegex(ValueError, "item_template"):
            render_markdown(
                self.prepare([self.assignment()]),
                StyleSettings(item_template="$unknown"),
                self.now,
                self.zone,
            )

    def test_naive_current_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            prepare([self.assignment()], datetime(2026, 11, 16), self.zone, 14, False)

    def test_dst_transition_uses_the_correct_local_offset(self):
        now = datetime(2026, 10, 24, 12, tzinfo=UTC)
        items = prepare(
            [
                self.assignment(due_at="2026-10-25T00:30:00Z"),
                self.assignment(due_at="2026-10-25T01:30:00Z"),
            ],
            now,
            self.zone,
            14,
            False,
        )
        self.assertEqual(items[0]["due"], "2026-10-25 02:30 CEST")
        self.assertEqual(items[1]["due"], "2026-10-25 02:30 CET")

    def test_empty_plan_has_an_explicit_message(self):
        text = render_markdown([], StyleSettings(), self.now, self.zone)
        self.assertIn("No assignments match your study filters.", text)


if __name__ == "__main__":
    unittest.main()
