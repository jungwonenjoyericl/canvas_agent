"""Mocked HTTP tests; no Canvas account or network access is required."""

import io
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from canvas_agent.client import CanvasClient, CanvasError, NoRedirect


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = CanvasClient("https://school.example", "test-token")

    def response(self, data, link=""):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = data
        response.headers = {"Link": link}
        return response

    def test_follows_pagination_and_sends_bearer_header(self):
        next_url = "https://school.example/api/v1/courses?opaque=2"
        self.client.opener.open = MagicMock(
            side_effect=[
                self.response('[{"id":1}]', f'<{next_url}>; rel="next"'),
                self.response('[{"id":2}]'),
            ]
        )
        self.assertEqual(self.client.list_items("courses"), [{"id": 1}, {"id": 2}])
        requests = [call.args[0] for call in self.client.opener.open.call_args_list]
        self.assertEqual(requests[1].full_url, next_url)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-token")
        self.assertEqual(requests[0].get_method(), "GET")

    def test_rejects_cross_origin_next_page_before_sending_token(self):
        self.client.opener.open = MagicMock(
            return_value=self.response(
                "[]",
                '<https://other.example/api/v1/courses>; rel="next"',
            )
        )
        with self.assertRaisesRegex(CanvasError, "unsafe"):
            self.client.list_items("courses")
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_rejects_pagination_loop(self):
        url = "https://school.example/api/v1/courses?per_page=100"
        self.client.opener.open = MagicMock(
            return_value=self.response("[]", f'<{url}>; rel="next"')
        )
        with self.assertRaisesRegex(CanvasError, "loop"):
            self.client.list_items("courses")

    @patch("canvas_agent.client.time.sleep")
    def test_rate_limit_retries(self, sleep):
        error = HTTPError(
            "https://school.example",
            429,
            "rate limit",
            {"Retry-After": "1"},
            io.BytesIO(),
        )
        self.client.opener.open = MagicMock(side_effect=[error, self.response("[]")])
        self.assertEqual(self.client.list_items("courses"), [])
        sleep.assert_called_once_with(1)

    def test_auth_error_is_actionable_and_does_not_include_token(self):
        self.client.opener.open = MagicMock(
            side_effect=HTTPError(
                "https://school.example",
                401,
                "test-token",
                {},
                io.BytesIO(),
            )
        )
        with self.assertRaisesRegex(CanvasError, "Check your Canvas token") as caught:
            self.client.list_items("courses")
        self.assertNotIn("test-token", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 401)

    @patch("canvas_agent.client.time.sleep")
    def test_server_errors_stop_after_two_retries(self, sleep):
        errors = [
            HTTPError("https://school.example", 503, "unavailable", {}, io.BytesIO())
            for _ in range(3)
        ]
        self.client.opener.open = MagicMock(side_effect=errors)
        with self.assertRaisesRegex(CanvasError, "HTTP 503"):
            self.client.list_items("courses")
        self.assertEqual(self.client.opener.open.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_selected_course_fetches_submission_and_personal_due_dates(self):
        self.client.list_items = MagicMock(
            side_effect=[
                [{"id": 10, "name": "Math"}, {"id": 20, "name": "Physics"}],
                [{"id": 30, "name": "Vectors"}],
            ]
        )
        assignments = self.client.assignments((10,))
        self.assertEqual(assignments[0]["course_name"], "Math")
        self.assertEqual(assignments[0]["course_id"], 10)
        self.client.list_items.assert_called_with(
            "courses/10/assignments",
            {
                "include[]": ["submission"],
                "override_assignment_dates": "true",
            },
        )

    def test_rejects_invalid_json_and_unexpected_response(self):
        for payload in ("not JSON", '{"error":"denied"}', "[1]"):
            with self.subTest(payload=payload):
                self.client.opener.open = MagicMock(return_value=self.response(payload))
                with self.assertRaises(CanvasError):
                    self.client.list_items("courses")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example"
            )
        )

    def test_base_url_requires_https_origin(self):
        for url in (
            "http://school.example",
            "https://school.example/api/v1",
            "https://user:password@school.example",
        ):
            with self.assertRaises(ValueError):
                CanvasClient(url, "test-token")


if __name__ == "__main__":
    unittest.main()
