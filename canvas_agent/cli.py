"""Command-line entry point for manual runs and scheduled refreshes."""

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .client import CanvasClient, CanvasError, JsonObject
from .config import load_settings
from .content import demo_course_content, prepare_course_content, render_course_content
from .planner import prepare, render_markdown


def demo_assignments(now: datetime) -> list[JsonObject]:
    """Return sample work relative to now, without credentials or network access."""
    examples = [
        ("Matrix exercises", -1),
        ("Vector spaces", 2),
        ("Optional practice", None),
    ]
    assignments = []
    for assignment_id, (name, days) in enumerate(examples, start=1):
        due_at = now + timedelta(days=days) if days is not None else None
        assignments.append(
            {
                "id": assignment_id,
                "course_id": 101,
                "course_name": "Example: Linear Algebra",
                "name": name,
                "due_at": due_at.isoformat() if due_at else None,
                "points_possible": 10,
                "html_url": f"https://example.com/assignments/{assignment_id}",
                "description": (
                    "<p>Review the lecture notes, practice, then check your answers.</p>"
                ),
                "submission": {"workflow_state": "unsubmitted"},
            }
        )
    return assignments


def write_output(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 output file after its contents are complete.

    Create missing parent directories. If writing or replacement fails, keep
    the previous output and remove the temporary file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        # Use the same filesystem for atomic replacement; close first for Windows.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Turn Canvas assignments into a personal study plan."
    )
    parser.add_argument("--config", type=Path, default=Path("config.local.toml"))
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use sample assignments without credentials or network access",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument(
        "--include-course-content",
        action="store_true",
        help="Include homepage, syllabus, pages, and modules alongside assignments",
    )
    parser.add_argument(
        "--output", type=Path, help="Replace this generated file; defaults to stdout"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one refresh and return 0 on success or 1 on an operational error.

    argparse handles help and invalid arguments with its standard exit codes.
    Generated data goes to stdout unless --output is set; diagnostics use stderr.
    """
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config, demo=args.demo)
        now = datetime.now(UTC)
        courses = []
        if args.demo:
            assignments = demo_assignments(now)
            if args.include_course_content:
                courses = prepare_course_content(
                    demo_course_content(), "https://example.com"
                )
        else:
            client = CanvasClient(settings.base_url, settings.token)
            if args.include_course_content:
                raw_courses = client.course_content(settings.course_ids)
                courses = prepare_course_content(raw_courses, client.base_url)
                assignments = (
                    client.assignments([course["id"] for course in courses])
                    if courses
                    else []
                )
            else:
                assignments = client.assignments(settings.course_ids)

        items = prepare(
            assignments,
            now,
            settings.zone,
            settings.days_ahead,
            settings.include_completed,
        )
        if args.format == "json":
            export: object = items
            if args.include_course_content:
                export = {
                    "schema_version": 1,
                    "generated_at": now.isoformat(),
                    "assignment_filters": {
                        "timezone": settings.zone.key,
                        "days_ahead": settings.days_ahead,
                        "include_completed": settings.include_completed,
                    },
                    "assignments": items,
                    "courses": courses,
                }
            content = json.dumps(export, ensure_ascii=False, indent=2) + "\n"
        else:
            content = render_markdown(items, settings.style, now, settings.zone)
            if args.include_course_content:
                content += "\n" + render_course_content(courses)

        for course in courses:
            for warning in course["warnings"]:
                print(f"Warning (course {course['id']}): {warning}", file=sys.stderr)
        if args.output:
            write_output(args.output, content)
            summary = f"{len(items)} assignments"
            if args.include_course_content:
                summary += f" and context for {len(courses)} courses"
            print(f"Wrote {summary} to {args.output}", file=sys.stderr)
        else:
            print(content, end="")
    except (CanvasError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
