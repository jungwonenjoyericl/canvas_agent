"""Compare successful observations without confusing filters with deleted work."""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from urllib.parse import quote, urlsplit

from .client import JsonObject
from .planner import assignment_due_date, escape_markdown
from .storage import read_json


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot_path(
    directory: Path,
    namespace: str,
    course_ids: Sequence[int],
    *,
    course_content: bool,
    files: bool,
) -> Path:
    """Keep accounts, course selections, and export scopes in separate baselines."""
    key = _fingerprint([namespace, sorted(set(course_ids)), course_content, files])
    return directory / f"{key}.json"


def load_snapshot(path: Path) -> JsonObject | None:
    """Treat missing, corrupt, or incompatible state as a new baseline."""
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    if not isinstance(data.get("checked_at"), str) or not isinstance(
        data.get("items"), dict
    ):
        return None
    for record in data["items"].values():
        if not isinstance(record, dict) or not isinstance(
            record.get("fingerprint"), str
        ):
            return None
    return data


def observe(
    assignments: Sequence[JsonObject],
    courses: Sequence[JsonObject],
    now: datetime,
) -> JsonObject:
    """Snapshot unfiltered assignments and accessible pages/files, without bodies."""
    items = {}

    def add(
        resource: str,
        course_id: int,
        item_id: object,
        name: str,
        url: str,
        content: object,
        **extra: object,
    ) -> None:
        items[f"{resource}:{course_id}:{item_id}"] = {
            "resource": resource,
            "course_id": course_id,
            "id": item_id,
            "name": name,
            "url": url,
            "fingerprint": _fingerprint(content),
            **extra,
        }

    for assignment in assignments:
        if assignment.get("published") is False or assignment.get("locked_for_user"):
            continue
        due = assignment_due_date(assignment, assignment.get("submission") or {})
        add(
            "assignment",
            assignment["course_id"],
            assignment["id"],
            assignment.get("name", "Untitled assignment"),
            assignment.get("html_url", ""),
            [assignment.get(key) for key in ("name", "description", "points_possible")],
            due_at=due.astimezone(UTC).isoformat() if due else None,
        )
    for course in courses:
        course_id = course["id"]
        add(
            "syllabus",
            course_id,
            course_id,
            f"{course['name']}: syllabus",
            course["url"] + "/assignments/syllabus",
            course["syllabus"]["html"],
        )
        pages = [*course["pages"]]
        if course.get("front_page"):
            pages.append(course["front_page"])
        for page in pages:
            if page["status"] == "available":
                add(
                    "page",
                    course_id,
                    page.get("id") or page["url"],
                    page["title"],
                    page["url"],
                    [page["title"], page["body"]["html"]],
                )
        for file in course.get("files", []):
            if file["status"] not in ("unavailable", "locked", "limit_reached", "demo"):
                add(
                    "file",
                    course_id,
                    file["id"],
                    file["name"],
                    file["url"],
                    [
                        file.get(key)
                        for key in (
                            "name",
                            "size",
                            "updated_at",
                            "modified_at",
                            "content_type",
                        )
                    ],
                )
    return {"schema_version": 1, "checked_at": now.isoformat(), "items": items}


def compare(
    current: JsonObject,
    previous: JsonObject | None,
) -> tuple[JsonObject, JsonObject]:
    """Report additions/updates and retain unseen resources across partial exports.

    Absence does not prove deletion: permissions, disabled indexes, and file limits
    can hide resources. Only observed records replace the previous observation.
    """
    events = []
    old_items = previous["items"] if previous else {}
    if previous is not None:
        for key, item in current["items"].items():
            old = old_items.get(key)
            info = {key: value for key, value in item.items() if key != "fingerprint"}
            if old is None:
                events.append({**info, "kind": "new"})
                continue
            if item["resource"] == "assignment" and item["due_at"] != old.get("due_at"):
                events.append(
                    {
                        **info,
                        "kind": "deadline_changed",
                        "before": old.get("due_at"),
                        "after": item["due_at"],
                    }
                )
            if item["fingerprint"] != old["fingerprint"]:
                events.append({**info, "kind": "updated"})
    report = {
        "baseline": previous is None,
        "previous_check": previous["checked_at"] if previous else None,
        "checked_at": current["checked_at"],
        "events": events,
    }
    snapshot = {**current, "items": {**old_items, **current["items"]}}
    return report, snapshot


def render_changes(report: JsonObject, zone: tzinfo) -> str:
    """Render changes with source links and deadlines in the configured timezone."""
    lines = ["## Changes since previous check", ""]
    if report.get("demo"):
        lines.append("Demo only. No comparison baseline is saved.")
    elif report["baseline"]:
        lines.append(
            "Baseline created. Changes will appear after the next successful check."
        )
    elif not report["events"]:
        lines.append("No changes detected in the available course material.")
    for event in report["events"]:
        label = escape_markdown(f"{event['name']} (course {event['course_id']})")
        url = event["url"]
        if urlsplit(url).scheme in ("https", "http"):
            label = f"[{label}]({quote(url, safe=':/?=&%#@+')})"
        if event["kind"] == "deadline_changed":

            def date(value: str | None) -> str:
                if value is None:
                    return "No due date"
                return (
                    datetime.fromisoformat(value)
                    .astimezone(zone)
                    .strftime("%Y-%m-%d %H:%M %Z")
                )

            lines.append(
                f"- Deadline changed: {label} — {date(event['before'])} → {date(event['after'])}."
            )
        else:
            lines.append(f"- {event['kind'].title()} {event['resource']}: {label}.")
    return "\n".join(lines) + "\n"
