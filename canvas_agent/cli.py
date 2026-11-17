"""Command-line entry point for manual runs and scheduled refreshes."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .changes import compare, load_snapshot, observe, render_changes, snapshot_path
from .client import CanvasClient, CanvasError, JsonObject
from .config import load_settings
from .content import demo_course_content, prepare_course_content, render_course_content
from .extraction import require_extractors
from .files import add_demo_files, add_file_content
from .planner import prepare, render_markdown
from .storage import write_json, write_output


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
    parser.add_argument(
        "--include-files",
        action="store_true",
        help="Download linked PDFs/PPTX and extract their text (includes course content)",
    )
    parser.add_argument(
        "--changes",
        action="store_true",
        help="Compare with the previous successful check",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("output/.state"),
        help="Directory for private change snapshots",
    )
    parser.add_argument(
        "--refresh-files",
        action="store_true",
        help="Download files again even when a cached copy matches",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one refresh and return 0 on success or 1 on an operational error.

    argparse handles help and invalid arguments with its standard exit codes.
    Generated data goes to stdout unless --output is set; diagnostics use stderr.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.refresh_files and not args.include_files:
        parser.error("--refresh-files requires --include-files")
    include_course_content = args.include_course_content or args.include_files
    try:
        settings = load_settings(args.config, demo=args.demo)
        if args.include_files and not args.demo:
            require_extractors()
        now = datetime.now(UTC)
        courses = []
        state_path = None
        previous = None
        if args.demo:
            assignments = demo_assignments(now)
            if include_course_content:
                courses = prepare_course_content(
                    demo_course_content(), "https://example.com"
                )
                if args.include_files:
                    add_demo_files(courses)
        else:
            client = CanvasClient(settings.base_url, settings.token)
            if args.changes:
                state_path = snapshot_path(
                    args.state_dir,
                    client.cache_namespace,
                    settings.course_ids,
                    course_content=include_course_content,
                    files=args.include_files,
                )
                if args.output and args.output.resolve() == state_path.resolve():
                    raise ValueError(
                        "The output file must differ from the snapshot file."
                    )
                previous = load_snapshot(state_path)
            if include_course_content:
                raw_courses = client.course_content(settings.course_ids)
                courses = prepare_course_content(raw_courses, client.base_url)
                assignments = (
                    client.assignments([course["id"] for course in courses])
                    if courses
                    else []
                )
                if args.include_files:
                    add_file_content(
                        client,
                        courses,
                        assignments,
                        settings.files,
                        refresh=args.refresh_files,
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
        changes = None
        snapshot = None
        if args.changes:
            changes, snapshot = compare(observe(assignments, courses, now), previous)
            changes["demo"] = args.demo
        if args.format == "json":
            export: object = items
            if include_course_content or args.changes:
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
                if changes is not None:
                    export["changes"] = changes
            content = json.dumps(export, ensure_ascii=False, indent=2) + "\n"
        else:
            content = render_markdown(items, settings.style, now, settings.zone)
            if changes is not None:
                content += "\n" + render_changes(changes, settings.zone)
            if include_course_content:
                content += "\n" + render_course_content(courses)

        for course in courses:
            for warning in course["warnings"]:
                print(f"Warning (course {course['id']}): {warning}", file=sys.stderr)
        if args.output:
            write_output(args.output, content)
            summary = f"{len(items)} assignments"
            if include_course_content:
                summary += f" and context for {len(courses)} courses"
            print(f"Wrote {summary} to {args.output}", file=sys.stderr)
        else:
            print(content, end="")
            sys.stdout.flush()
        # Advance the comparison only after the export was successfully delivered.
        if state_path is not None:
            write_json(state_path, snapshot)
        if changes is not None:
            summary = (
                "Baseline created"
                if changes["baseline"]
                else f"{len(changes['events'])} changes detected"
            )
            print("Demo: state not saved" if args.demo else summary, file=sys.stderr)
        if args.include_files and not args.demo:
            documents = [file for course in courses for file in course.get("files", [])]
            reused = sum(file.get("cached") is True for file in documents)
            downloaded = sum(file.get("cached") is False for file in documents)
            print(
                f"Files: {downloaded} downloaded, {reused} reused locally",
                file=sys.stderr,
            )
    except (CanvasError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
