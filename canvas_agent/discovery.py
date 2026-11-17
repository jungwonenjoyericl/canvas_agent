"""Discover same-course Canvas page links without fetching arbitrary URLs."""

import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit


class _PageLinks(HTMLParser):
    def __init__(self, course_url: str) -> None:
        super().__init__()
        self.course_url = course_url
        self.origin = urlsplit(course_url).netloc
        course_path = re.escape(urlsplit(course_url).path.rstrip("/"))
        self.pattern = re.compile(rf"^(?:/api/v1)?{course_path}/pages/([^/]+)/?$")
        self.slugs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        for key in ("href", "data-api-endpoint"):
            value = attributes.get(key)
            if not value:
                continue
            try:
                parsed = urlsplit(urljoin(self.course_url + "/", value))
            except ValueError:
                continue
            if parsed.scheme != "https" or parsed.netloc != self.origin:
                continue
            match = self.pattern.fullmatch(parsed.path)
            if match:
                slug = unquote(match.group(1))
                if (
                    slug not in (".", "..")
                    and "/" not in slug
                    and slug not in self.slugs
                ):
                    self.slugs.append(slug)


def discover_page_slugs(html: str, course_url: str) -> list[str]:
    """Return unique page slugs linked inside this course's HTTPS Canvas origin.

    Accept browser links and Canvas data-api-endpoint attributes. Ignore query
    strings, fragments, external sites, other courses, files, and module URLs.
    Callers build API requests from these slugs instead of following the links.
    """
    parser = _PageLinks(course_url)
    parser.feed(html)
    parser.close()
    return parser.slugs


class _FileLinks(HTMLParser):
    """Recognize Canvas file IDs in course HTML, including embedded previews."""

    def __init__(self, course_url: str) -> None:
        super().__init__()
        self.course_url = course_url
        self.origin = urlsplit(course_url).netloc
        course_path = re.escape(urlsplit(course_url).path.rstrip("/"))
        self.pattern = re.compile(
            rf"^(?:/api/v1)?(?:{course_path})?/files/([1-9][0-9]*)(?:/(?:download|preview))?/?$"
        )
        self.ids: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ("a", "iframe", "object", "embed"):
            return
        for key, value in attrs:
            if key not in ("href", "src", "data", "data-api-endpoint") or not value:
                continue
            try:
                parsed = urlsplit(urljoin(self.course_url + "/", value))
            except ValueError:
                continue
            if parsed.scheme != "https" or parsed.netloc != self.origin:
                continue
            match = self.pattern.fullmatch(parsed.path)
            if match:
                file_id = int(match.group(1))
                if file_id not in self.ids:
                    self.ids.append(file_id)


def discover_file_ids(html: str, course_url: str) -> list[int]:
    """Find Canvas file references without following external or other-course URLs.

    Bare /files/:id links are accepted, but membership and access are subsequently
    checked using the course-scoped Files API before downloading anything.
    """
    parser = _FileLinks(course_url)
    parser.feed(html)
    parser.close()
    return parser.ids
