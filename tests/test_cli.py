"""CLI contract tests for offline runs and failure-safe file output."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from canvas_agent.cli import main, write_output
from canvas_agent.client import CanvasError
from canvas_agent.content import demo_course_content


class CliTests(unittest.TestCase):
    def test_course_demo_json_has_context_and_assignment_metadata(self):
        config = Path(__file__).resolve().parents[1] / "config.example.toml"
        with (
            patch("canvas_agent.cli.CanvasClient") as client,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = main(
                [
                    "--demo",
                    "--config",
                    str(config),
                    "--include-course-content",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["assignments"]), 3)
        self.assertEqual(data["courses"][0]["homepage"]["type"], "wiki")
        self.assertEqual(data["assignment_filters"]["timezone"], "Europe/Stockholm")
        client.assert_not_called()

    def test_course_demo_markdown_appends_full_context(self):
        config = Path(__file__).resolve().parents[1] / "config.example.toml"
        with redirect_stdout(io.StringIO()) as output:
            code = main(["--demo", "--config", str(config), "--include-course-content"])
        self.assertEqual(code, 0)
        self.assertIn("Matrix exercises", output.getvalue())
        self.assertIn("Week 1 study guide", output.getvalue())

    def test_optional_content_warnings_are_exported_and_reported(self):
        config = Path(__file__).resolve().parents[1] / "config.example.toml"
        raw_courses = demo_course_content()
        raw_courses[0]["warnings"] = [
            "Could not read courses/101/front_page (HTTP 404)."
        ]
        with (
            patch("canvas_agent.cli.CanvasClient") as factory,
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as error,
        ):
            client = factory.return_value
            client.base_url = "https://school.example"
            client.course_content.return_value = raw_courses
            client.assignments.return_value = []
            code = main(
                [
                    "--config",
                    str(config),
                    "--include-course-content",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("HTTP 404", error.getvalue())
        self.assertEqual(
            len(json.loads(output.getvalue())["courses"][0]["warnings"]), 1
        )
        client.assignments.assert_called_once_with([101])

    def test_failed_course_refresh_keeps_previous_export(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "course.json"
            output.write_text("previous export", encoding="utf-8")
            config = Path(__file__).resolve().parents[1] / "config.example.toml"
            with (
                patch("canvas_agent.cli.CanvasClient") as factory,
                redirect_stderr(io.StringIO()),
            ):
                factory.return_value.course_content.side_effect = CanvasError(
                    "Network failure"
                )
                code = main(
                    [
                        "--config",
                        str(config),
                        "--include-course-content",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous export")

    def test_demo_writes_json_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output" / "assignments.json"
            with (
                patch("canvas_agent.cli.CanvasClient") as client,
                redirect_stderr(io.StringIO()),
            ):
                code = main(
                    [
                        "--demo",
                        "--config",
                        str(Path(directory) / "absent.toml"),
                        "--format",
                        "json",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 3)
            client.assert_not_called()

    def test_missing_config_returns_failure(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            redirect_stderr(io.StringIO()) as error,
        ):
            code = main(["--config", str(Path(directory) / "absent.toml")])
        self.assertEqual(code, 1)
        self.assertIn("Copy config.example.toml", error.getvalue())

    def test_failed_refresh_keeps_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                '[canvas]\nbase_url="https://school.example"\ntoken="test-token"\n',
                encoding="utf-8",
            )
            output = root / "plan.md"
            output.write_text("previous plan", encoding="utf-8")
            with (
                patch(
                    "canvas_agent.cli.CanvasClient.assignments",
                    side_effect=CanvasError("Unavailable"),
                ),
                redirect_stderr(io.StringIO()),
            ):
                code = main(["--config", str(config), "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous plan")

    def test_example_config_renders_demo(self):
        config = Path(__file__).resolve().parents[1] / "config.example.toml"
        with redirect_stdout(io.StringIO()) as output:
            code = main(["--demo", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertIn("My study plan", output.getvalue())
        self.assertIn("Review the lecture notes", output.getvalue())

    def test_invalid_template_fails_before_fetching_assignments(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[style]\nitem_template="$unknown"\n', encoding="utf-8")
            with (
                patch("canvas_agent.cli.CanvasClient") as client,
                redirect_stderr(io.StringIO()) as error,
            ):
                code = main(["--config", str(config)])
            self.assertEqual(code, 1)
            self.assertIn("style.item_template", error.getvalue())
            client.assert_not_called()

    def test_failed_replacement_preserves_output_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan.md"
            output.write_text("previous plan", encoding="utf-8")
            with patch("canvas_agent.cli.os.replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    write_output(output, "new plan")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous plan")
            self.assertEqual(list(Path(directory).iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
