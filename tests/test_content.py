"""Verify full course fetching and safe, source-linked study exports."""

import json
import unittest
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from canvas_agent.client import CanvasClient, CanvasError
from canvas_agent.content import (
    demo_course_content,
    prepare_course_content,
    render_course_content,
)


class CourseFetchingTests(unittest.TestCase):
    def setUp(self):
        self.client = CanvasClient("https://school.example", "test-token")
        self.course = {
            "id": 101,
            "name": "Math",
            "default_view": "wiki",
            "syllabus_body": "<p>Goals</p>",
        }
        self.objects = {
            "courses/101": self.course,
            "courses/101/front_page": {
                "page_id": 1,
                "url": "welcome",
                "body": "<p>Home</p>",
            },
            "courses/101/pages/study%20guide": {
                "page_id": 2,
                "url": "study guide",
                "body": "<p>Read chapter 1</p>",
            },
        }
        self.lists = {
            "courses": [{"id": 101}],
            "courses/101/pages": [
                {"page_id": 1, "url": "welcome"},
                {"page_id": 2, "url": "study guide"},
            ],
            "courses/101/modules": [
                {"id": 10, "name": "Week 1", "items": [{"id": 999}]}
            ],
            "courses/101/modules/10/items": [
                {"id": 20, "title": "Guide", "type": "Page"}
            ],
            "courses/101/activity_stream": [
                {"id": 30, "type": "Announcement", "message": "Hello"}
            ],
        }
        self.client.get_object = MagicMock(
            side_effect=lambda path, params=None: self.objects[path]
        )
        self.client.list_items = MagicMock(
            side_effect=lambda path, params=None: self.lists[path]
        )

    def test_fetches_full_page_bodies_and_module_items(self):
        course = self.client.course_content([101])[0]
        self.assertEqual(course["front_page"]["body"], "<p>Home</p>")
        self.assertEqual(course["pages"][0]["body"], "<p>Read chapter 1</p>")
        self.assertEqual(course["modules"][0]["items"][0]["id"], 20)
        self.client.get_object.assert_any_call(
            "courses/101", {"include[]": ["syllabus_body"]}
        )
        self.assertNotIn(
            "courses/101/pages/welcome",
            [call.args[0] for call in self.client.get_object.call_args_list],
        )

    def test_empty_selection_uses_active_student_courses(self):
        self.assertEqual(len(self.client.course_content([])), 1)
        self.client.list_items.assert_any_call(
            "courses", {"enrollment_type": "student", "enrollment_state": "active"}
        )

    def test_each_homepage_type_uses_appropriate_endpoint(self):
        for home in ("syllabus", "modules", "assignments", "feed"):
            with self.subTest(home=home):
                self.course["default_view"] = home
                self.lists["courses/101/pages"] = []
                self.client.get_object.reset_mock()
                course = self.client.course_content([101])[0]
                self.assertIsNone(course["front_page"])
                self.assertEqual(bool(course["activity_stream"]), home == "feed")
                self.assertNotIn(
                    "courses/101/front_page",
                    [call.args[0] for call in self.client.get_object.call_args_list],
                )

    def test_missing_front_page_and_restricted_page_are_reported(self):
        def get_object(path, params=None):
            if path.endswith("front_page"):
                raise CanvasError("Missing", status_code=404)
            if path.startswith("courses/101/pages/"):
                raise CanvasError("Forbidden", status_code=403)
            return self.objects[path]

        self.client.get_object.side_effect = get_object
        course = self.client.course_content([101])[0]
        self.assertIsNone(course["front_page"])
        self.assertTrue(course["pages"][0]["unavailable"])
        self.assertEqual(len(course["warnings"]), 3)
        self.assertIn("HTTP 404", course["warnings"][0])

    def test_authentication_and_server_failures_abort_export(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):

                def get_object(path, params=None, status=status):
                    if path.endswith("front_page"):
                        raise CanvasError("Failure", status_code=status)
                    return self.objects[path]

                self.client.get_object.side_effect = get_object
                with self.assertRaises(CanvasError):
                    self.client.course_content([101])

    def test_unpublished_pages_skipped_and_locked_pages_not_requested(self):
        self.lists["courses/101/pages"] = [
            {"url": "draft", "published": False},
            {"url": "locked", "locked_for_user": True},
        ]
        course = self.client.course_content([101])[0]
        self.assertEqual([page["url"] for page in course["pages"]], ["locked"])
        self.assertEqual(self.client.get_object.call_count, 2)


class CourseHttpTests(unittest.TestCase):
    def test_get_object_uses_authenticated_get_and_query_parameters(self):
        client = CanvasClient("https://school.example", "test-token")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = '{"id":101,"syllabus_body":"Goals"}'
        response.headers = {}
        client.opener.open = MagicMock(return_value=response)
        self.assertEqual(
            client.get_object("courses/101", {"include[]": ["syllabus_body"]})["id"],
            101,
        )
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(
            parse_qs(urlsplit(request.full_url).query), {"include[]": ["syllabus_body"]}
        )

    def test_object_endpoint_rejects_a_list(self):
        client = CanvasClient("https://school.example", "test-token")
        client._get = MagicMock(return_value=([], ""))
        with self.assertRaisesRegex(CanvasError, "object response"):
            client.get_object("courses/101")

    def test_page_and_module_item_lists_follow_all_pages(self):
        for path in (
            "courses/101/pages",
            "courses/101/modules",
            "courses/101/modules/10/items",
        ):
            with self.subTest(path=path):
                client = CanvasClient("https://school.example", "test-token")
                next_url = f"https://school.example/api/v1/{path}?cursor=next"
                client._get = MagicMock(
                    side_effect=[
                        ([{"id": 1}], f'<{next_url}>; rel="next"'),
                        ([{"id": 2}], ""),
                    ]
                )
                self.assertEqual(
                    [item["id"] for item in client.list_items(path)], [1, 2]
                )
                client._get.assert_called_with(next_url)


class CourseRenderingTests(unittest.TestCase):
    def test_json_retains_html_and_text_with_source_links(self):
        courses = prepare_course_content(
            demo_course_content(), "https://school.example"
        )
        course = json.loads(json.dumps(courses))[0]
        self.assertIn("Learning goals", course["syllabus"]["text"])
        self.assertIn("<h2>", course["syllabus"]["html"])
        self.assertEqual(
            course["front_page"]["body"]["links"],
            ["https://school.example/courses/101/pages/week-1"],
        )
        self.assertIn("\n\n", course["pages"][0]["body"]["text"])
        self.assertEqual(course["modules"][0]["items"][0]["page_url"], "week-1")

    def test_markdown_contains_homepage_syllabus_pages_and_modules(self):
        courses = prepare_course_content(
            demo_course_content(), "https://school.example"
        )
        text = render_course_content(courses)
        for phrase in (
            "Homepage type: wiki",
            "Welcome to Linear Algebra",
            "Learning goals",
            "Week 1 study guide",
            "#### Modules",
        ):
            self.assertIn(phrase, text)

    def test_locked_content_is_not_included(self):
        raw = demo_course_content()
        raw[0]["front_page"].update(
            {
                "locked_for_user": True,
                "body": "hidden content",
                "lock_explanation": "<p>Available next week</p>",
            }
        )
        course = prepare_course_content(raw, "https://school.example")[0]
        self.assertEqual(course["front_page"]["body"]["text"], "")
        self.assertIn("Available next week", render_course_content([course]))
        self.assertNotIn("hidden content", json.dumps(course))

    def test_html_cannot_inject_markup_or_executable_links_into_markdown(self):
        raw = demo_course_content()
        raw[0]["front_page"]["body"] = (
            '<script>bad()</script><p>[fake](bad)</p><a href="javascript:bad()">Bad link</a>'
        )
        courses = prepare_course_content(raw, "https://school.example")
        self.assertEqual(courses[0]["front_page"]["body"]["links"], [])
        text = render_course_content(courses)
        self.assertNotIn("bad()", text)
        self.assertIn(r"\[fake\]\(bad\)", text)

    def test_activity_feed_is_exported_and_rendered(self):
        raw = demo_course_content()
        raw[0].update(
            {
                "default_view": "feed",
                "front_page": None,
                "activity_stream": [
                    {
                        "type": "Announcement",
                        "title": "Exam",
                        "message": "<p>Bring a calculator.</p>",
                    }
                ],
            }
        )
        courses = prepare_course_content(raw, "https://school.example")
        self.assertIn("Bring a calculator", render_course_content(courses))
        self.assertEqual(courses[0]["homepage"]["type"], "feed")


if __name__ == "__main__":
    unittest.main()
