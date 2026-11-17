"""Collect referenced Canvas files and add extracted document text to courses."""

from collections.abc import Sequence
from pathlib import Path

from .cache import remember_download, reuse_download
from .client import CanvasClient, CanvasError, FileTooLargeError, JsonObject
from .config import FileSettings
from .discovery import discover_file_ids
from .extraction import ExtractionError, extract_document

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def referenced_files(
    course: JsonObject, assignments: Sequence[JsonObject]
) -> dict[int, list[str]]:
    """Map unique Canvas file IDs to the course pages/modules that reference them."""
    references: dict[int, list[str]] = {}

    def add(file_id: int, source: str) -> None:
        if type(file_id) is int and file_id > 0:
            sources = references.setdefault(file_id, [])
            if source not in sources:
                sources.append(source)

    def html_links(html: str, source: str) -> None:
        for file_id in discover_file_ids(html, course["url"]):
            add(file_id, source)

    html_links(course["syllabus"]["html"], course["url"] + "/assignments/syllabus")
    pages = [*course["pages"]]
    if course.get("front_page"):
        pages.append(course["front_page"])
    for page in pages:
        if page["status"] == "available":
            html_links(page["body"]["html"], page["url"])
    for module in course["modules"]:
        for item in module["items"]:
            if item["type"] == "File":
                add(item.get("content_id"), item.get("url") or course["url"])
    for item in course.get("activity_stream", []):
        html_links(item["body"]["html"], item.get("url") or course["url"])
    for assignment in assignments:
        if (
            assignment.get("course_id") == course["id"]
            and assignment.get("published") is not False
            and not assignment.get("locked_for_user")
        ):
            html_links(
                assignment.get("description") or "",
                assignment.get("html_url") or course["url"],
            )
    return references


def _file_record(file_id: int, course_url: str, sources: list[str]) -> JsonObject:
    return {
        "id": file_id,
        "name": f"File {file_id}",
        "url": f"{course_url}/files/{file_id}",
        "referenced_by": sources,
        "status": "unavailable",
        "message": "",
        "local_path": None,
        "text": "",
        "sections": [],
        "warnings": [],
    }


def add_file_content(
    client: CanvasClient,
    courses: Sequence[JsonObject],
    assignments: Sequence[JsonObject],
    settings: FileSettings,
    *,
    refresh: bool = False,
) -> None:
    """Attach file records to normalized courses, downloading PDFs/PPTX only.

    Fetch metadata through the course-scoped API, then download to numeric names
    inside the configured directory. Reuse matching local bytes unless refresh is
    requested. Keep signed download URLs out of exports and cache metadata.
    Optional file failures are visible; authentication/network/server errors abort.
    """
    for course in courses:
        records = []
        references = referenced_files(course, assignments)
        for index, (file_id, sources) in enumerate(references.items()):
            record = _file_record(file_id, course["url"], sources)
            records.append(record)
            if index >= settings.max_files_per_course:
                record.update(
                    status="limit_reached",
                    message="Not fetched: files.max_files_per_course reached.",
                )
                continue
            try:
                metadata = client.get_object(f"courses/{course['id']}/files/{file_id}")
                record.update(
                    name=metadata.get("display_name")
                    or metadata.get("filename")
                    or record["name"],
                    size=metadata.get("size"),
                    updated_at=metadata.get("updated_at"),
                    modified_at=metadata.get("modified_at"),
                    content_type=metadata.get("content-type"),
                )
                if metadata.get("locked_for_user") or metadata.get("hidden_for_user"):
                    record.update(
                        status="locked",
                        message="Canvas marks this file as locked or hidden for your account.",
                    )
                    continue
                suffix = Path(metadata.get("filename") or record["name"]).suffix.lower()
                mime = (metadata.get("content-type") or "").split(";", 1)[0].lower()
                if mime == "application/pdf":
                    suffix = ".pdf"
                elif mime == PPTX_MIME:
                    suffix = ".pptx"
                if suffix not in (".pdf", ".pptx"):
                    record.update(
                        status="unsupported",
                        message="Text extraction supports PDF and PPTX. Convert legacy PPT files to PPTX or PDF.",
                    )
                    continue
                max_bytes = settings.max_file_mb * 1024 * 1024
                if (metadata.get("size") or 0) > max_bytes:
                    raise FileTooLargeError("File exceeds files.max_file_mb.")
                destination = (
                    settings.download_dir / str(course["id"]) / f"{file_id}{suffix}"
                )
                cached = not refresh and reuse_download(
                    destination, metadata, client.cache_namespace, max_bytes=max_bytes
                )
                if not cached:
                    client.download_file(
                        metadata.get("url") or "", destination, max_bytes=max_bytes
                    )
                    remember_download(destination, metadata, client.cache_namespace)
                record["cached"] = cached
                record["local_path"] = destination.as_posix()
                record.update(
                    extract_document(destination, max_pages=settings.max_pages)
                )
            except FileTooLargeError as error:
                record.update(status="too_large", message=str(error))
            except ExtractionError as error:
                record.update(status="error", message=str(error))
            except CanvasError as error:
                if error.status_code not in (403, 404):
                    raise
                record.update(
                    status="unavailable",
                    message=f"File unavailable (HTTP {error.status_code}).",
                )

        if len(references) > settings.max_files_per_course:
            course["warnings"].append(
                f"Only the first {settings.max_files_per_course} of {len(references)} file references were checked; increase files.max_files_per_course for the rest."
            )
        for record in records:
            if record["status"] not in ("ok", "unsupported", "limit_reached"):
                message = record["message"] or " ".join(record["warnings"])
                course["warnings"].append(f"File {record['id']}: {message}")
        course["files"] = records


def add_demo_files(courses: Sequence[JsonObject]) -> None:
    """Attach explicitly labeled sample text without any downloads or parsing."""
    for course in courses:
        record = _file_record(1, course["url"], [course["url"]])
        record.update(
            name="Example lecture.pdf",
            status="demo",
            message="Sample text only; no file was downloaded or parsed.",
            text="Page 1\nPractice matrix multiplication.",
            section_kind="page",
            total_sections=1,
            sections=[
                {"number": 1, "text": "Practice matrix multiplication.", "notes": ""}
            ],
        )
        course["files"] = [record]
