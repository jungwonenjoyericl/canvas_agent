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
