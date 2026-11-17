"""Normalize assignments and render an editable study digest."""

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from html.parser import HTMLParser
from string import Template
from typing import TypedDict
from urllib.parse import quote, urlsplit

from .client import JsonObject
from .config import StyleSettings

COMPLETED_STATES = frozenset({"submitted", "graded", "pending_review"})
GROUP_PRIORITY = {"Overdue": 0, "No due date": 2, "Completed": 3}


class StudyItem(TypedDict):
    """Normalized assignment fields shared by Markdown and JSON exports."""

    id: int
    course_id: int
    name: str
    course: str
    due_at: str | None
    due: str
    status: str
    completed: bool
    group: str
    points: float | None
    url: str
    description: str


class _PlainTextParser(HTMLParser):
    """Extract readable description text while omitting scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "li", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "li", "div"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    """Convert an HTML description to a single whitespace-normalized text line."""
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _due_date(item: JsonObject, submission: JsonObject) -> datetime | None:
    # An explicit null personal due date means no deadline; do not fall back.
    raw = (
        submission["cached_due_date"]
        if "cached_due_date" in submission
        else item.get("due_at")
    )
    if not raw:
        return None
    due_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # Canvas timestamps are UTC; tolerate missing offsets in imported fixtures.
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    return due_at


def _group_label(
    due_at: datetime | None, completed: bool, now: datetime, zone: tzinfo
) -> str:
    if completed:
        return "Completed"
    if due_at is None:
        return "No due date"
    if due_at < now:
        return "Overdue"
    return due_at.astimezone(zone).strftime("%Y-%m-%d")


def _assignment_order(item: StudyItem) -> tuple[datetime, str, str]:
    due_at = (
        datetime.fromisoformat(item["due_at"])
        if item["due_at"]
        else datetime.max.replace(tzinfo=UTC)
    )
    return due_at, item["course"], item["name"]


def prepare(
    assignments: Sequence[JsonObject],
    now: datetime,
    zone: tzinfo,
    days: int,
    include_completed: bool,
) -> list[StudyItem]:
    """Filter and normalize assignments, sorted by deadline, course, and name.

    now must be timezone-aware. The horizon includes the entire final local date;
    overdue and undated work remain visible. Input records are not modified.
    Submitted, graded, pending-review, and excused work counts as completed.
    """
    if now.utcoffset() is None:
        raise ValueError("The study plan's current time must be timezone-aware.")
    result: list[StudyItem] = []
    # Compare local calendar dates so DST and time of day do not shorten the horizon.
    last_date = now.astimezone(zone).date() + timedelta(days=days)
    for item in assignments:
        if item.get("published") is False:
            continue
        submission = item.get("submission") or {}
        completed = (
            bool(submission.get("excused"))
            or submission.get("workflow_state") in COMPLETED_STATES
        )
        if completed and not include_completed:
            continue
        due = _due_date(item, submission)
        if due and due.astimezone(zone).date() > last_date:
            continue
        result.append(
            {
                "id": item["id"],
                "course_id": item["course_id"],
                "name": item.get("name", "Untitled assignment"),
                "course": item["course_name"],
                "due_at": due.isoformat() if due else None,
                "due": due.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
                if due
                else "No due date",
                "status": "excused"
                if submission.get("excused")
                else submission.get("workflow_state", "unsubmitted"),
                "completed": completed,
                "group": _group_label(due, completed, now, zone),
                "points": item.get("points_possible"),
                "url": item.get("html_url", ""),
                "description": plain_text(item.get("description") or ""),
            }
        )
    return sorted(result, key=_assignment_order)


def escape_markdown(value: object) -> str:
    """Escape a single-line value so Canvas content cannot change the layout."""
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", " ".join(str(value).split()))


def _template_values(item: StudyItem) -> dict[str, str]:
    values = {
        key: escape_markdown(value if value is not None else "—")
        for key, value in item.items()
    }
    url = item["url"]
    # Link destinations need URL encoding, not Markdown's backslash escaping.
    values["url"] = (
        quote(url, safe=":/?=&%#@+")
        if urlsplit(url).scheme in ("https", "http")
        else ""
    )
    values["checkbox"] = "x" if item["completed"] else " "
    return values


def render_markdown(
    items: Sequence[StudyItem], style: StyleSettings, now: datetime, zone: tzinfo
) -> str:
    """Render a study plan with dated sections and the configured item template.

    Overdue work comes first; undated and completed work follow dated sections.
    Values from Canvas are escaped while the user's template remains Markdown.
    """
    template = Template(style.item_template)
    groups: dict[str, list[StudyItem]] = {}
    for item in items:
        groups.setdefault(item["group"], []).append(item)
    lines = [
        f"# {escape_markdown(style.title)}",
        "",
        f"Updated: {now.astimezone(zone):%Y-%m-%d %H:%M %Z}",
        "",
    ]
    if not items:
        lines.append("No assignments match your study filters.")
    order = sorted(groups, key=lambda group: (GROUP_PRIORITY.get(group, 1), group))
    for group in order:
        lines.extend([f"## {group}", ""])
        for item in groups[group]:
            lines.append(template.substitute(_template_values(item)).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
