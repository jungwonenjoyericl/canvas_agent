"""Read-only Canvas REST client with pagination and bounded retries."""

import json
import re
import time
from collections import deque
from collections.abc import Mapping, Sequence
from email.message import Message
from typing import IO, Any, Literal, overload
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .discovery import discover_page_slugs

JsonObject = dict[str, Any]
QueryValue = str | int | Sequence[str | int]

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 30
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
NEXT_PAGE_PATTERN = re.compile(r'<([^>]+)>;\s*rel="next"')
HTTP_ERROR_HINTS = {
    401: "Check your Canvas token; it may have expired.",
    403: "Your token or enrollment cannot access this resource.",
    404: "Check your Canvas URL and course IDs.",
    429: "Canvas is rate limiting requests. Try again later.",
}


class CanvasError(RuntimeError):
    """An actionable Canvas request failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Keep HTTP status available for handling optional, restricted resources."""
        super().__init__(message)
        self.status_code = status_code


class NoRedirect(HTTPRedirectHandler):
    """Prevent authenticated API requests from following HTTP redirects."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Let urllib report redirects as HTTP errors instead of following them."""
        # Never forward a bearer token to a login page or another host.
        return None


class CanvasClient:
    """Fetch Canvas resources using read-only, paginated API requests.

    Methods raise CanvasError for HTTP, transport, and response-format failures.
    Each request has a timeout and at most two retries for transient HTTP errors.
    """

    def __init__(self, base_url: str, token: str) -> None:
        """Validate an HTTPS origin and bearer token without making a request."""
        if not isinstance(base_url, str) or not isinstance(token, str):
            raise ValueError("Canvas base_url and token must be strings.")
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            raise ValueError("Canvas base_url must be an HTTPS origin, without a path.")
        if not token.strip():
            raise ValueError(
                "Set CANVAS_API_TOKEN or canvas.token in config.local.toml."
            )
        if any(ord(char) < 33 or ord(char) > 126 for char in token.strip()):
            raise ValueError(
                "Canvas token must contain only printable ASCII without spaces."
            )
        self._token = token.strip()
        self._origin = (parsed.scheme, parsed.netloc)
        self.opener = build_opener(NoRedirect())

    def _get(self, url: str) -> tuple[object, str]:
        """Fetch one JSON page and its Link header, retrying transient failures."""
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != self._origin or not parsed.path.startswith(
            "/api/v1/"
        ):
            raise CanvasError("Canvas returned an unsafe pagination URL.")
        retries = 0
        while True:
            request = Request(
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
            )
            try:
                with self.opener.open(
                    request, timeout=REQUEST_TIMEOUT_SECONDS
                ) as response:
                    return json.load(response), response.headers.get("Link", "")
            except HTTPError as error:
                code = error.code
                retry_after = error.headers.get("Retry-After", "")
                error.close()
                if code in RETRYABLE_STATUS_CODES and retries < MAX_RETRIES:
                    delay = (
                        min(int(retry_after), MAX_RETRY_DELAY_SECONDS)
                        if retry_after.isascii() and retry_after.isdigit()
                        else 2**retries
                    )
                    time.sleep(delay)
                    retries += 1
                    continue
                hint = HTTP_ERROR_HINTS.get(code, "Request failed; try again later.")
                # Error bodies and URLs can contain private data; report only the status.
                raise CanvasError(
                    f"Canvas HTTP {code}. {hint}", status_code=code
                ) from None
            except (URLError, TimeoutError, OSError):
                raise CanvasError(
                    "Could not reach Canvas. Check your URL and connection."
                ) from None
            except (ValueError, UnicodeError):
                raise CanvasError(
                    "Canvas did not return valid JSON. Check your Canvas URL."
                ) from None

    def get_object(
        self, path: str, params: Mapping[str, QueryValue] | None = None
    ) -> JsonObject:
        """Retrieve one JSON object from a resource path relative to /api/v1/."""
        query = urlencode(params or {}, doseq=True)
        url = f"{self.base_url}/api/v1/{path}"
        if query:
            url += f"?{query}"
        result, _ = self._get(url)
        if not isinstance(result, dict):
            raise CanvasError("Canvas returned an unexpected object response.")
        return result

    def list_items(
        self, path: str, params: Mapping[str, QueryValue] | None = None
    ) -> list[JsonObject]:
        """Collect every page for a resource path relative to /api/v1/.

        params accepts scalar values and sequences for repeated query parameters.
        Reject off-origin next links, pagination loops, and non-object responses.
        """
        query = urlencode({"per_page": 100, **(params or {})}, doseq=True)
        url = f"{self.base_url}/api/v1/{path}?{query}"
        items: list[JsonObject] = []
        visited: set[str] = set()
        while url:
            if url in visited:
                raise CanvasError("Canvas returned a pagination loop.")
            visited.add(url)
            page, links = self._get(url)
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise CanvasError("Canvas returned an unexpected list response.")
            items.extend(page)
            # Canvas cursors are opaque; follow the supplied URL without rebuilding it.
            match = NEXT_PAGE_PATTERN.search(links)
            url = match.group(1) if match else None
        return items

    def assignments(self, course_ids: Sequence[int]) -> list[JsonObject]:
        """Fetch assignments with submission data and course display names.

        An empty course_ids sequence selects all active student courses. Explicit
        IDs are fetched even when they are absent from that active-course list.
        A failure in any course aborts the refresh rather than returning partial data.
        """
        courses = self.list_items(
            "courses",
            {
                "enrollment_type": "student",
                "enrollment_state": "active",
            },
        )
        by_id = {course["id"]: course for course in courses}
        selected = course_ids or list(by_id)
        result = []
        for course_id in selected:
            course = by_id.get(course_id, {})
            for assignment in self.list_items(
                f"courses/{course_id}/assignments",
                {
                    "include[]": ["submission"],
                    "override_assignment_dates": "true",
                },
            ):
                result.append(
                    {
                        **assignment,
                        "course_id": course_id,
                        "course_name": course.get("name", f"Course {course_id}"),
                    }
                )
        return result

    @overload
    def _optional_content(
        self, path: str, warnings: list[str], *, collection: Literal[True]
    ) -> list[JsonObject]: ...

    @overload
    def _optional_content(
        self, path: str, warnings: list[str], *, collection: Literal[False] = False
    ) -> JsonObject | None: ...

    def _optional_content(
        self, path: str, warnings: list[str], *, collection: bool = False
    ) -> JsonObject | list[JsonObject] | None:
        """Record absent or forbidden course context; propagate operational failures."""
        try:
            return self.list_items(path) if collection else self.get_object(path)
        except CanvasError as error:
            if error.status_code not in (403, 404):
                raise
            warnings.append(f"Could not read {path} (HTTP {error.status_code}).")
            return [] if collection else None

    def course_content(self, course_ids: Sequence[int]) -> list[JsonObject]:
        """Fetch course metadata, syllabus, full pages, modules, and feed homepages.

        Empty IDs select active student courses. Missing or forbidden optional
        resources are recorded in each course's warnings. Authentication, network,
        and server failures abort the export. Linked files are not downloaded.
        """
        selected = list(dict.fromkeys(course_ids))
        if not selected:
            selected = [
                course["id"]
                for course in self.list_items(
                    "courses",
                    {"enrollment_type": "student", "enrollment_state": "active"},
                )
            ]
        result = []
        for course_id in selected:
            path = f"courses/{course_id}"
            course = self.get_object(path, {"include[]": ["syllabus_body"]})
            warnings: list[str] = []
            front_page = None
            if course.get("default_view") == "wiki":
                front_page = self._optional_content(f"{path}/front_page", warnings)

            modules = []
            for module in self._optional_content(
                f"{path}/modules", warnings, collection=True
            ):
                if module.get("published") is False:
                    continue
                # Embedded module items may be omitted or incomplete for large modules.
                items = self._optional_content(
                    f"{path}/modules/{module['id']}/items", warnings, collection=True
                )
                modules.append({**module, "items": items})

            pages = self._course_pages(course, front_page, modules, warnings)
            activity = []
            if course.get("default_view") == "feed":
                activity = self._optional_content(
                    f"{path}/activity_stream", warnings, collection=True
                )
            result.append(
                {
                    **course,
                    "front_page": front_page,
                    "pages": pages,
                    "modules": modules,
                    "activity_stream": activity,
                    "warnings": warnings,
                }
            )
        return result

    def _course_pages(
        self,
        course: JsonObject,
        front_page: JsonObject | None,
        modules: Sequence[JsonObject],
        warnings: list[str],
    ) -> list[JsonObject]:
        """Read indexed and linked pages even when Canvas disables the page index."""
        path = f"courses/{course['id']}"
        course_url = f"{self.base_url}/{path}"
        try:
            index = self.list_items(f"{path}/pages")
        except CanvasError as error:
            if error.status_code not in (403, 404):
                raise
            index = []
            warnings.append(
                f"Page index unavailable for {path} (HTTP {error.status_code}). "
                "Exporting pages discovered through course content and modules; "
                "unlinked pages may be missing."
            )

        queue = deque(index)
        for module in modules:
            for item in module.get("items", []):
                if item.get("type") == "Page" and item.get("page_url"):
                    queue.append(
                        {
                            "url": item["page_url"],
                            "title": item.get("title"),
                            "published": item.get("published", True),
                            "locked_for_user": (item.get("content_details") or {}).get(
                                "locked_for_user", False
                            ),
                        }
                    )

        def enqueue_links(html: str) -> None:
            for slug in discover_page_slugs(html, course_url):
                queue.append({"url": slug, "title": slug})

        enqueue_links(course.get("syllabus_body") or "")
        seen_slugs: set[str] = set()
        seen_ids: set[int] = set()
        if front_page:
            if front_page.get("url"):
                seen_slugs.add(front_page["url"])
            if front_page.get("page_id") is not None:
                seen_ids.add(front_page["page_id"])
            if (
                not front_page.get("locked_for_user")
                and front_page.get("published") is not False
            ):
                enqueue_links(front_page.get("body") or "")

        pages = []
        while queue:
            summary = queue.popleft()
            slug = summary["url"]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            if summary.get("published") is False:
                continue
            if summary.get("locked_for_user"):
                pages.append(summary)
                continue
            detail = self._optional_content(
                f"{path}/pages/{quote(slug, safe='')}", warnings
            )
            if detail is None:
                pages.append({**summary, "unavailable": True})
                continue
            if detail.get("published") is False:
                continue
            page_id = detail.get("page_id")
            if page_id is not None:
                if page_id in seen_ids:
                    continue
                seen_ids.add(page_id)
            # Old slugs can redirect logically to the same page. Keep each page once.
            if detail.get("url"):
                seen_slugs.add(detail["url"])
            pages.append(detail)
            if not detail.get("locked_for_user"):
                enqueue_links(detail.get("body") or "")
        return pages
