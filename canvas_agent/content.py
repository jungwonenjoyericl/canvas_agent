"""Normalize course context and render it as source-linked study material."""

from collections.abc import Sequence
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

from .client import JsonObject
from .planner import escape_markdown, plain_text

HOME_DESCRIPTIONS = {
    "wiki": "The course homepage is the front page shown below.",
    "syllabus": "The course homepage displays the syllabus shown below.",
    "modules": "The course homepage displays the modules listed below.",
    "assignments": "The course homepage displays assignments. The assignment export uses your study filters.",
    "feed": "The course homepage displays recent activity, included below as returned by Canvas.",
}


def demo_course_content() -> list[JsonObject]:
    """Return sample course context for offline previews and documentation."""
    return [
        {
            "id": 101,
            "name": "Example: Linear Algebra",
            "course_code": "MATH101",
            "default_view": "wiki",
            "public_description": "Vectors, matrices, and applications.",
            "syllabus_body": "<h2>Learning goals</h2><p>Explain vector spaces and solve linear systems.</p>",
            "front_page": {
                "page_id": 1,
                "url": "welcome",
                "title": "Welcome to Linear Algebra",
                "body": '<p>Start with the weekly study guide.</p><p><a href="/courses/101/pages/week-1">Week 1</a></p>',
            },
            "pages": [
                {
                    "page_id": 2,
                    "url": "week-1",
                    "title": "Week 1 study guide",
                    "body": "<p>Read chapter 1 before the lecture.</p><p>Practice matrix multiplication.</p>",
                }
            ],
            "modules": [
                {
                    "id": 10,
                    "name": "Week 1",
                    "position": 1,
                    "state": "unlocked",
                    "items": [
                        {
                            "id": 20,
                            "title": "Week 1 study guide",
                            "type": "Page",
                            "page_url": "week-1",
                            "html_url": "https://example.com/courses/101/pages/week-1",
                        }
                    ],
                }
            ],
            "activity_stream": [],
            "warnings": [],
        }
    ]


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme in ("https", "http")
            and parsed.hostname
            and not parsed.username
        ):
            return value
    except ValueError:
        pass
    return ""


class _ContentParser(HTMLParser):
    """Preserve paragraph boundaries and collect links without following them."""

    def __init__(self, source_url: str) -> None:
        super().__init__()
        self.source_url = source_url
        self.parts: list[str] = []
        self.links: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self.parts.append("\n")
        if tag in ("td", "th"):
            self.parts.append(" ")
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            url = _safe_url(urljoin(self.source_url, attributes["href"]))
            if url and url not in self.links:
                self.links.append(url)
        if tag == "img" and attributes.get("alt"):
            self.parts.append(f" {attributes['alt']} ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in (
            "p",
            "div",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "tr",
        ):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _body(html: str, source_url: str) -> JsonObject:
    parser = _ContentParser(source_url)
    parser.feed(html)
    parser.close()
    paragraphs = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return {
        "html": html,
        "text": "\n\n".join(line for line in paragraphs if line),
        "links": parser.links,
    }


def _page(page: JsonObject, course_url: str) -> JsonObject:
    url = _safe_url(
        page.get("html_url")
        or f"{course_url}/pages/{quote(str(page.get('url', '')), safe='')}"
    )
    locked = page.get("locked_for_user", False)
    unavailable = page.get("unavailable", False)
    return {
        "id": page.get("page_id"),
        "title": page.get("title", "Untitled page"),
        "url": url,
        "updated_at": page.get("updated_at"),
        "status": "locked" if locked else "unavailable" if unavailable else "available",
        "lock_explanation": plain_text(page.get("lock_explanation") or ""),
        "body": _body("" if locked or unavailable else page.get("body") or "", url),
    }


def prepare_course_content(
    courses: Sequence[JsonObject], base_url: str
) -> list[JsonObject]:
    """Export course text, original HTML, source URLs, and module relationships.

    Restrict the export to study-relevant fields. Locked page bodies are omitted;
    their status and explanation remain visible. Links are retained, not fetched.
    """
    result = []
    for course in courses:
        url = f"{base_url.rstrip('/')}/courses/{course['id']}"
        home_type = course.get("default_view", "unknown")
        front_page = course.get("front_page")
        modules = []
        for module in course.get("modules", []):
            items = []
            for item in module.get("items", []):
                if item.get("published") is False:
                    continue
                items.append(
                    {
                        "id": item["id"],
                        "title": item.get("title", "Untitled item"),
                        "type": item.get("type"),
                        "position": item.get("position"),
                        "url": _safe_url(item.get("html_url") or ""),
                        "external_url": _safe_url(item.get("external_url") or ""),
                        "content_id": item.get("content_id"),
                        "page_url": item.get("page_url"),
                        "completion_requirement": item.get("completion_requirement"),
                    }
                )
            modules.append(
                {
                    "id": module["id"],
                    "name": module.get("name", "Untitled module"),
                    "position": module.get("position"),
                    "state": module.get("state"),
                    "unlock_at": module.get("unlock_at"),
                    "prerequisite_module_ids": module.get(
                        "prerequisite_module_ids", []
                    ),
                    "items": items,
                }
            )
        activity = [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "title": item.get("title") or item.get("type", "Activity"),
                "url": _safe_url(item.get("html_url") or ""),
                "updated_at": item.get("updated_at"),
                "body": _body(item.get("message") or "", url),
            }
            for item in course.get("activity_stream", [])
        ]
        result.append(
            {
                "id": course["id"],
                "name": course.get("name", f"Course {course['id']}"),
                "course_code": course.get("course_code"),
                "url": url,
                "description": plain_text(course.get("public_description") or ""),
                "homepage": {
                    "type": home_type,
                    "url": url,
                    "description": HOME_DESCRIPTIONS.get(
                        home_type, "This homepage type is not supported."
                    ),
                },
                "syllabus": _body(
                    course.get("syllabus_body") or "", f"{url}/assignments/syllabus"
                ),
                "front_page": _page(front_page, url)
                if front_page and front_page.get("published") is not False
                else None,
                "pages": [
                    _page(page, url)
                    for page in course.get("pages", [])
                    if page.get("published") is not False
                ],
                "modules": modules,
                "activity_stream": activity,
                "warnings": list(course.get("warnings", [])),
            }
        )
    return result


def _link(label: str, url: str) -> str:
    return (
        f"[{escape_markdown(label)}]({quote(url, safe=':/?=&%#@+')})"
        if url
        else escape_markdown(label)
    )


def _body_lines(body: JsonObject) -> list[str]:
    lines = [
        escape_markdown(line) if line else "" for line in body["text"].splitlines()
    ]
    if not lines:
        lines.append("No text was returned by Canvas.")
    if body["links"]:
        lines.extend(["", "Links:", ""])
        lines.extend(f"- {_link(url, url)}" for url in body["links"])
    return [*lines, ""]


def _page_lines(page: JsonObject) -> list[str]:
    lines = [
        f"#### {escape_markdown(page['title'])}",
        "",
        _link("Source page", page["url"]),
        "",
    ]
    if page["status"] != "available":
        lines.extend(
            [
                f"Content {page['status']}. {escape_markdown(page['lock_explanation'])}",
                "",
            ]
        )
    else:
        lines.extend(_body_lines(page["body"]))
    return lines


def render_course_content(courses: Sequence[JsonObject]) -> str:
    """Render normalized context as Markdown suitable for appending to a plan."""
    lines = ["## Course context", ""]
    for course in courses:
        lines.extend(
            [
                f"### {escape_markdown(course['name'])}",
                "",
                _link("Course homepage", course["url"]),
                "",
            ]
        )
        if course["description"]:
            lines.extend([escape_markdown(course["description"]), ""])
        lines.extend(
            [
                f"Homepage type: {escape_markdown(course['homepage']['type'])}.",
                course["homepage"]["description"],
                "",
            ]
        )
        if course["warnings"]:
            lines.extend(["Unavailable content:", ""])
            lines.extend(
                f"- {escape_markdown(warning)}" for warning in course["warnings"]
            )
            lines.append("")
        if course["front_page"]:
            lines.extend(_page_lines(course["front_page"]))
        lines.extend(["#### Syllabus", "", *_body_lines(course["syllabus"])])
        for page in course["pages"]:
            lines.extend(_page_lines(page))
        lines.extend(["#### Modules", ""])
        if not course["modules"]:
            lines.extend(["No modules were returned by Canvas.", ""])
        for module in course["modules"]:
            lines.extend([f"##### {escape_markdown(module['name'])}", ""])
            if module["state"]:
                lines.extend([f"State: {escape_markdown(module['state'])}", ""])
            lines.extend(
                f"- {_link(item['title'], item['url'] or item['external_url'])} ({escape_markdown(item['type'])})"
                for item in module["items"]
            )
            lines.append("")
        if course["homepage"]["type"] == "feed":
            lines.extend(["#### Recent activity", ""])
            if not course["activity_stream"]:
                lines.extend(["No activity was returned by Canvas.", ""])
            for activity in course["activity_stream"]:
                lines.extend(
                    [
                        f"##### {_link(activity['title'], activity['url'])}",
                        "",
                        *_body_lines(activity["body"]),
                    ]
                )
    return "\n".join(lines).rstrip() + "\n"
