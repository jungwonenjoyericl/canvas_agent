"""Document extraction and file export tests using generated, non-private fixtures."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from canvas_agent.client import CanvasClient, CanvasError, FileTooLargeError
from canvas_agent.config import FileSettings
from canvas_agent.content import (
    demo_course_content,
    prepare_course_content,
    render_course_content,
)
from canvas_agent.discovery import discover_file_ids
from canvas_agent.extraction import ExtractionError, extract_document
from canvas_agent.files import add_file_content, referenced_files


def make_pdf(path: Path, *, blank: bool = False, encrypted: bool = False) -> None:
    """Create two small PDF pages with real selectable text for parser tests."""
    writer = PdfWriter()
    for text in ("Matrix multiplication", "Vector spaces"):
        page = writer.add_blank_page(width=300, height=300)
        if not blank:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("test-password")
    writer.write(path)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_pdf_extracts_numbered_pages(self):
        path = self.root / "sample.pdf"
        make_pdf(path)
        result = extract_document(path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_sections"], 2)
        self.assertIn("Matrix multiplication", result["sections"][0]["text"])
        self.assertIn("Page 2\nVector spaces", result["text"])

    def test_page_limit_is_visible(self):
        path = self.root / "sample.pdf"
        make_pdf(path)
        result = extract_document(path, max_pages=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["sections"]), 1)
        self.assertIn("first 1 of 2", result["warnings"][0])

    def test_image_only_or_blank_pdf_reports_no_text(self):
        path = self.root / "blank.pdf"
        make_pdf(path, blank=True)
        result = extract_document(path)
        self.assertEqual(result["status"], "no_text")
        self.assertEqual(result["text"], "")
        self.assertIn("OCR", result["warnings"][0])

    def test_encrypted_pdf_reports_an_actionable_error(self):
        path = self.root / "encrypted.pdf"
        make_pdf(path, encrypted=True)
        with self.assertRaisesRegex(ExtractionError, "Encrypted PDFs"):
            extract_document(path)

    def test_pptx_extracts_text_tables_groups_and_speaker_notes(self):
        path = self.root / "lecture.pptx"
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(
            0, 0, Inches(3), Inches(1)
        ).text = "Flervariabelanalys – övningar"
        group = slide.shapes.add_group_shape()
        group.shapes.add_textbox(0, 0, Inches(2), Inches(1)).text = "Gradient"
        table = slide.shapes.add_table(1, 2, 0, Inches(2), Inches(4), Inches(1)).table
        table.cell(0, 0).text = "Method"
        table.cell(0, 1).text = "Lagrange"
        slide.notes_slide.notes_text_frame.text = "Remember the constraint."
        presentation.save(path)
        result = extract_document(path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["section_kind"], "slide")
        for value in (
            "Flervariabelanalys",
            "Gradient",
            "Lagrange",
            "Remember the constraint",
        ):
            self.assertIn(value, result["text"])
        self.assertEqual(result["sections"][0]["notes"], "Remember the constraint.")

    def test_invalid_pptx_does_not_echo_document_contents(self):
        path = self.root / "broken.pptx"
        path.write_bytes(b"private document contents")
        with self.assertRaisesRegex(ExtractionError, "Could not parse") as error:
            extract_document(path)
        self.assertNotIn("private document contents", str(error.exception))

    def test_text_limit_is_reported(self):
        path = self.root / "sample.pdf"
        make_pdf(path)
        with patch("canvas_agent.extraction.MAX_TEXT_CHARS", 5):
            result = extract_document(path)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sections"][0]["text"], "Matri")


class FileDiscoveryTests(unittest.TestCase):
    def test_links_and_embeds_are_deduplicated(self):
        html = """
        <a href="/courses/101/files/10/download?download_frd=1">PDF</a>
        <a href="/files/10" data-api-endpoint="https://school.example/api/v1/courses/101/files/10">Again</a>
        <iframe src="/courses/101/files/20/preview"></iframe>
        <object data="/api/v1/files/30"></object>
        <a href="https://other.example/files/40">External</a>
        <a href="/courses/202/files/50">Other course</a>
        <a href="/files/../60">Traversal</a>
        """
        self.assertEqual(
            discover_file_ids(html, "https://school.example/courses/101"), [10, 20, 30]
        )

    def test_module_and_assignment_references_keep_sources(self):
        course = prepare_course_content(
            demo_course_content(), "https://school.example"
        )[0]
        course["modules"][0]["items"] = [
            {
                "type": "File",
                "content_id": 10,
                "url": course["url"] + "/modules/items/1",
            }
        ]
        assignment = {
            "course_id": 101,
            "html_url": course["url"] + "/assignments/5",
            "description": '<a href="/files/10">PDF</a>',
        }
        refs = referenced_files(course, [assignment])
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(refs[10]), 2)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.client = CanvasClient("https://school.example", "test-token")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.destination = Path(temp.name) / "document.pdf"
        self.url = "https://school.example/files/1/download"

    def response(self, data, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = headers or {}
        response.read.side_effect = io.BytesIO(data).read
        return response

    def test_external_storage_redirect_does_not_receive_canvas_token(self):
        redirect = HTTPError(
            self.url,
            302,
            "redirect",
            {"Location": "https://storage.example/doc?signature=private"},
            io.BytesIO(),
        )
        self.client.opener.open = MagicMock(
            side_effect=[redirect, self.response(b"document")]
        )
        self.client.download_file(self.url, self.destination, max_bytes=100)
        requests = [call.args[0] for call in self.client.opener.open.call_args_list]
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-token")
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertEqual(self.destination.read_bytes(), b"document")

    def test_insecure_redirect_is_rejected(self):
        self.client.opener.open = MagicMock(
            side_effect=HTTPError(
                self.url,
                302,
                "redirect",
                {"Location": "http://storage.example/file"},
                io.BytesIO(),
            )
        )
        with self.assertRaisesRegex(CanvasError, "unsafe"):
            self.client.download_file(self.url, self.destination, max_bytes=100)
        self.assertEqual(self.client.opener.open.call_count, 1)
        self.assertFalse(self.destination.exists())

    def test_size_limit_preserves_previous_copy_and_removes_temporary_file(self):
        self.destination.write_bytes(b"previous")
        for headers in ({}, {"Content-Length": "8"}):
            with self.subTest(headers=headers):
                self.client.opener.open = MagicMock(
                    return_value=self.response(b"12345678", headers)
                )
                with self.assertRaises(FileTooLargeError):
                    self.client.download_file(self.url, self.destination, max_bytes=4)
                self.assertEqual(self.destination.read_bytes(), b"previous")
                self.assertEqual(
                    list(self.destination.parent.iterdir()), [self.destination]
                )

    def test_incomplete_download_does_not_replace_previous_copy(self):
        self.destination.write_bytes(b"previous")
        self.client.opener.open = MagicMock(
            return_value=self.response(b"short", {"Content-Length": "10"})
        )
        with self.assertRaisesRegex(CanvasError, "expected size"):
            self.client.download_file(self.url, self.destination, max_bytes=100)
        self.assertEqual(self.destination.read_bytes(), b"previous")

    @patch("canvas_agent.client.time.sleep")
    def test_transient_download_failure_is_retried(self, sleep):
        self.client.opener.open = MagicMock(
            side_effect=[
                HTTPError(self.url, 429, "private signed URL", {}, io.BytesIO()),
                self.response(b"ok"),
            ]
        )
        self.client.download_file(self.url, self.destination, max_bytes=100)
        self.assertEqual(self.destination.read_bytes(), b"ok")
        sleep.assert_called_once_with(1)


class FileExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fixture = self.root / "fixture.pdf"
        make_pdf(self.fixture)
        self.courses = prepare_course_content(
            demo_course_content(), "https://school.example"
        )
        self.courses[0]["syllabus"]["html"] = (
            '<a href="/courses/101/files/10">Lecture</a>'
        )
        self.client = MagicMock(spec=CanvasClient)
        self.client.cache_namespace = "test-account"
        self.metadata = {
            "id": 10,
            "filename": "../../lecture.pdf",
            "display_name": "Lecture.pdf",
            "content-type": "application/pdf",
            "size": self.fixture.stat().st_size,
            "updated_at": "2026-11-17T12:00:00Z",
            "url": "https://school.example/files/10/download?verifier=private-signature",
        }
        self.client.get_object.return_value = self.metadata
        self.client.download_file.side_effect = self.download
        self.settings = FileSettings(download_dir=self.root / "files")

    def download(self, url, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.fixture.read_bytes())

    def test_export_uses_numeric_paths_and_never_exposes_signed_download_url(self):
        add_file_content(self.client, self.courses, [], self.settings)
        record = self.courses[0]["files"][0]
        self.assertEqual(record["status"], "ok")
        self.assertEqual(Path(record["local_path"]), self.root / "files/101/10.pdf")
        self.assertNotIn("private-signature", json.dumps(record))
        self.client.get_object.assert_called_once_with("courses/101/files/10")
        markdown = render_course_content(self.courses)
        self.assertIn("###### Page 2", markdown)
        self.assertIn("Matrix multiplication", markdown)

    def test_unchanged_file_reuses_bytes_but_honors_new_extraction_limits(self):
        add_file_content(self.client, self.courses, [], self.settings)
        smaller = FileSettings(download_dir=self.settings.download_dir, max_pages=1)
        add_file_content(self.client, self.courses, [], smaller)
        self.client.download_file.assert_called_once()
        record = self.courses[0]["files"][0]
        self.assertTrue(record["cached"])
        self.assertEqual(len(record["sections"]), 1)
        self.assertEqual(record["status"], "partial")
        sidecar = Path(record["local_path"] + ".cache.json")
        self.assertNotIn("private-signature", sidecar.read_text(encoding="utf-8"))

    def test_changed_metadata_or_account_triggers_a_new_download(self):
        add_file_content(self.client, self.courses, [], self.settings)
        self.metadata["updated_at"] = "2026-11-18T12:00:00Z"
        add_file_content(self.client, self.courses, [], self.settings)
        self.client.cache_namespace = "other-account"
        add_file_content(self.client, self.courses, [], self.settings)
        self.assertEqual(self.client.download_file.call_count, 3)

    def test_missing_modified_and_corrupt_local_cache_are_redownloaded(self):
        add_file_content(self.client, self.courses, [], self.settings)
        path = Path(self.courses[0]["files"][0]["local_path"])
        sidecar = Path(str(path) + ".cache.json")
        for damage in (
            lambda: path.unlink(),
            lambda: path.write_bytes(b"edited"),
            lambda: sidecar.write_text("{broken"),
            lambda: sidecar.unlink(),
        ):
            damage()
            self.client.download_file.reset_mock()
            add_file_content(self.client, self.courses, [], self.settings)
            self.client.download_file.assert_called_once()
            self.assertFalse(self.courses[0]["files"][0]["cached"])

    def test_cached_file_is_not_used_if_access_is_now_locked(self):
        add_file_content(self.client, self.courses, [], self.settings)
        self.metadata["locked_for_user"] = True
        self.client.download_file.reset_mock()
        add_file_content(self.client, self.courses, [], self.settings)
        self.client.download_file.assert_not_called()
        record = self.courses[0]["files"][0]
        self.assertEqual(record["status"], "locked")
        self.assertEqual(record["text"], "")

    def test_force_refresh_and_missing_version_disable_reuse(self):
        add_file_content(self.client, self.courses, [], self.settings)
        add_file_content(self.client, self.courses, [], self.settings, refresh=True)
        self.metadata.pop("updated_at")
        add_file_content(self.client, self.courses, [], self.settings)
        add_file_content(self.client, self.courses, [], self.settings)
        self.assertEqual(self.client.download_file.call_count, 4)

    def test_failed_download_does_not_cache_new_version(self):
        add_file_content(self.client, self.courses, [], self.settings)
        self.metadata["updated_at"] = "2026-11-18T12:00:00Z"
        self.client.download_file.side_effect = CanvasError("Offline")
        with self.assertRaises(CanvasError):
            add_file_content(self.client, self.courses, [], self.settings)
        self.client.download_file.side_effect = self.download
        self.client.download_file.reset_mock()
        add_file_content(self.client, self.courses, [], self.settings)
        self.client.download_file.assert_called_once()

    def test_unsupported_locked_and_large_files_are_not_downloaded(self):
        cases = [
            (
                {
                    "filename": "old.ppt",
                    "content-type": "application/vnd.ms-powerpoint",
                },
                "unsupported",
            ),
            ({"locked_for_user": True}, "locked"),
            ({"size": 100 * 1024 * 1024}, "too_large"),
        ]
        for changes, status in cases:
            with self.subTest(status=status):
                self.client.get_object.return_value = {**self.metadata, **changes}
                add_file_content(self.client, self.courses, [], self.settings)
                self.assertEqual(self.courses[0]["files"][0]["status"], status)
        self.client.download_file.assert_not_called()

    def test_missing_file_is_visible_and_auth_failure_aborts(self):
        self.client.get_object.side_effect = CanvasError("Not found", status_code=404)
        add_file_content(self.client, self.courses, [], self.settings)
        self.assertEqual(self.courses[0]["files"][0]["status"], "unavailable")
        self.client.get_object.side_effect = CanvasError("Expired", status_code=401)
        with self.assertRaises(CanvasError):
            add_file_content(self.client, self.courses, [], self.settings)

    def test_file_count_limit_is_reported(self):
        self.courses[0]["syllabus"]["html"] += '<a href="/files/20">Another</a>'
        settings = FileSettings(
            download_dir=self.settings.download_dir, max_files_per_course=1
        )
        add_file_content(self.client, self.courses, [], settings)
        self.assertEqual(self.courses[0]["files"][1]["status"], "limit_reached")
        self.client.get_object.assert_called_once()

    def test_corrupt_file_is_reported_without_aborting_other_files(self):
        self.courses[0]["syllabus"]["html"] += '<a href="/files/20">Another</a>'

        def download(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                b"broken" if destination.stem == "10" else self.fixture.read_bytes()
            )

        self.client.download_file.side_effect = download
        with patch(
            "canvas_agent.extraction._pdf_sections",
            side_effect=[
                ExtractionError("Invalid PDF"),
                (1, iter([("Valid text", "")])),
            ],
        ):
            add_file_content(self.client, self.courses, [], self.settings)
        self.assertEqual(
            [item["status"] for item in self.courses[0]["files"]], ["error", "ok"]
        )


if __name__ == "__main__":
    unittest.main()
