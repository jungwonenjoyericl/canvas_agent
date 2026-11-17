"""Extract text from local PDFs and PPTX slides without running document content."""

from collections.abc import Iterable, Iterator
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from zipfile import ZipFile

MAX_TEXT_CHARS = 1_000_000
MAX_PPTX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


class ExtractionError(ValueError):
    """A document cannot be read by the supported text extractors."""


def require_extractors() -> None:
    """Fail early with installation instructions if optional libraries are absent."""
    if find_spec("pypdf") is None or find_spec("pptx") is None:
        raise ValueError(
            'File extraction requires: python -m pip install -e ".[files]"'
        )


def _shape_text(shapes: Iterable[Any]) -> Iterator[str]:
    for shape in shapes:
        if shape.has_text_frame:
            yield shape.text
        if shape.has_table:
            for row in shape.table.rows:
                yield "\t".join(cell.text for cell in row.cells if not cell.is_spanned)
        if hasattr(shape, "shapes"):
            yield from _shape_text(shape.shapes)


def _pdf_sections(path: Path) -> tuple[int, Iterator[tuple[str, str]]]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ExtractionError(
            "Encrypted PDFs are not supported; provide an unlocked copy."
        )
    return len(reader.pages), ((page.extract_text() or "", "") for page in reader.pages)


def _pptx_sections(path: Path) -> tuple[int, Iterator[tuple[str, str]]]:
    from pptx import Presentation

    # PPTX is a ZIP container. Bound its expanded size before parsing its XML.
    with ZipFile(path) as archive:
        if (
            sum(item.file_size for item in archive.infolist())
            > MAX_PPTX_UNCOMPRESSED_BYTES
        ):
            raise ExtractionError(
                "PPTX expanded contents exceed the 100 MiB parsing limit."
            )
    presentation = Presentation(path)

    def slides() -> Iterator[tuple[str, str]]:
        for slide in presentation.slides:
            text = "\n".join(_shape_text(slide.shapes))
            notes = ""
            if slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                notes = frame.text if frame is not None else ""
            yield text, notes

    return len(presentation.slides), slides()


def extract_document(path: Path, *, max_pages: int = 300) -> dict[str, Any]:
    """Extract numbered PDF pages or PPTX slides and speaker notes.

    Preserve line breaks, bound exported text to one million characters, and
    explicitly report blank sections and truncation. No OCR is performed, and
    diagrams, equations, and reading order may need review against the original.
    """
    require_extractors()
    if max_pages <= 0:
        raise ValueError("max_pages must be positive.")
    if path.suffix.lower() not in (".pdf", ".pptx"):
        raise ExtractionError("Only PDF and PPTX documents are supported.")
    # Keep local filesystem failures distinct from a malformed document.
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        kind = "page" if path.suffix.lower() == ".pdf" else "slide"
        total, source = _pdf_sections(path) if kind == "page" else _pptx_sections(path)
        warnings = []
        if total > max_pages:
            warnings.append(
                f"Only the first {max_pages} of {total} {kind}s were extracted (files.max_pages)."
            )
        sections = []
        remaining = MAX_TEXT_CHARS
        # Fetch only the selected sections, including when the parser is lazy.
        for number in range(1, min(total, max_pages) + 1):
            text, notes = next(source)
            text, notes = text.strip(), notes.strip()
            truncated = len(text) + len(notes) > remaining
            text = text[:remaining]
            remaining -= len(text)
            notes = notes[:remaining]
            remaining -= len(notes)
            sections.append({"number": number, "text": text, "notes": notes})
            if truncated or (remaining == 0 and number < min(total, max_pages)):
                warnings.append("Extracted text was limited to 1,000,000 characters.")
                break
        blank = sum(
            not section["text"] and not section["notes"] for section in sections
        )
        if blank or not sections:
            warnings.append(
                f"No text in {blank} of {len(sections)} processed {kind}s. Scans or images may require OCR."
            )
        combined = []
        for section in sections:
            if section["text"] or section["notes"]:
                combined.append(
                    f"{kind.title()} {section['number']}\n{section['text']}"
                )
                if section["notes"]:
                    combined.append("Speaker notes:\n" + section["notes"])
        return {
            "status": ("partial" if warnings else "ok") if combined else "no_text",
            "text": "\n\n".join(combined),
            "sections": sections,
            "section_kind": kind,
            "total_sections": total,
            "warnings": warnings,
        }
    except ExtractionError:
        raise
    except Exception as error:
        # Parsers raise several exception types for corrupt files. Do not echo
        # document data from their messages into logs or discard the whole export.
        raise ExtractionError(
            f"Could not parse this document ({type(error).__name__})."
        ) from None
