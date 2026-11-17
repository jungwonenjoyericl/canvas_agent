"""Regression tests for disabled page indexes and same-course link discovery."""

import unittest
from unittest.mock import MagicMock

from canvas_agent.client import CanvasClient, CanvasError
from canvas_agent.discovery import discover_page_slugs


class LinkDiscoveryTests(unittest.TestCase):
    def test_browser_and_api_links_are_decoded_and_deduplicated(self):
        html = """
            <a href="/courses/101/pages/study%20guide?view=1#top">Guide</a>
            <a href="https://school.example/courses/101/pages/study%20guide/">Again</a>
            <a href="/courses/101/modules/items/20"
               data-api-endpoint="https://school.example/api/v1/courses/101/pages/notes">Notes</a>
            <a href="pages/exam">Exam</a>
        """
        self.assertEqual(
            discover_page_slugs(html, "https://school.example/courses/101"),
            ["study guide", "notes", "exam"],
        )

    def test_links_cannot_escape_course_or_origin(self):
        urls = [
            "https://other.example/courses/101/pages/external",
            "http://school.example/courses/101/pages/insecure",
            "/courses/202/pages/other-course",
            "/courses/101/files/42/download",
            "/courses/101/pages",
            "/courses/101/pages/%2E%2E",
            "/courses/101/pages/a%2Fb",
            "javascript:alert(1)",
            "https://[invalid/",
        ]
        html = "".join(f'<a href="{url}">Link</a>' for url in urls)
        self.assertEqual(
            discover_page_slugs(html, "https://school.example/courses/101"), []
        )


class DisabledPageIndexTests(unittest.TestCase):
    def setUp(self):
        self.client = CanvasClient("https://school.example", "test-token")
        self.course = {
            "id": 101,
            "default_view": "syllabus",
            "syllabus_body": '<a href="/courses/101/pages/overview">Overview</a>',
        }
        self.pages = {
            "overview": {
                "page_id": 1,
                "url": "overview",
                "body": '<a href="/courses/101/pages/assessment">Assessment</a>',
            },
            "assessment": {
                "page_id": 2,
                "url": "assessment",
                "body": '<a href="/courses/101/pages/overview">Back</a>',
            },
            "module-page": {
                "page_id": 3,
                "url": "module-page",
                "body": "Module content",
            },
        }
        self.modules = [
            {
                "id": 10,
                "items": [
                    {"type": "Page", "page_url": "module-page", "title": "Module page"}
                ],
            }
        ]
        self.client.list_items = MagicMock(
            side_effect=CanvasError("Disabled", status_code=404)
        )
        self.client.get_object = MagicMock(
            side_effect=lambda path: self.pages[path.rsplit("/", 1)[1]]
        )

    def test_disabled_index_uses_modules_syllabus_and_recursive_links(self):
        warnings = []
        pages = self.client._course_pages(self.course, None, self.modules, warnings)
        self.assertEqual(
            {page["url"] for page in pages}, {"module-page", "overview", "assessment"}
        )
        self.assertEqual(self.client.get_object.call_count, 3)
        self.assertEqual(len(warnings), 1)
        self.assertIn("unlinked pages may be missing", warnings[0])

    def test_restricted_index_also_uses_accessible_direct_pages(self):
        self.client.list_items.side_effect = CanvasError("Forbidden", status_code=403)
        pages = self.client._course_pages(self.course, None, [], [])
        self.assertEqual(len(pages), 2)

    def test_front_page_links_work_without_syllabus_or_index(self):
        self.course["syllabus_body"] = ""
        front = {
            "page_id": 0,
            "url": "home",
            "body": '<a href="/courses/101/pages/overview">Overview</a>',
        }
        self.assertEqual(len(self.client._course_pages(self.course, front, [], [])), 2)

    def test_failed_link_remains_visible_as_unavailable(self):
        self.client.get_object.side_effect = CanvasError("Not found", status_code=404)
        warnings = []
        pages = self.client._course_pages(self.course, None, [], warnings)
        self.assertTrue(pages[0]["unavailable"])
        self.assertEqual(len(warnings), 2)

    def test_authentication_failure_is_not_treated_as_disabled_index(self):
        self.client.list_items.side_effect = CanvasError("Expired", status_code=401)
        with self.assertRaises(CanvasError):
            self.client._course_pages(self.course, None, [], [])
        self.client.get_object.assert_not_called()

    def test_locked_and_unpublished_index_entries_are_not_refetched_via_links(self):
        self.client.list_items.side_effect = None
        self.client.list_items.return_value = [
            {"url": "overview", "locked_for_user": True},
            {"url": "module-page", "published": False},
        ]
        pages = self.client._course_pages(self.course, None, self.modules, [])
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["locked_for_user"])
        self.client.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
