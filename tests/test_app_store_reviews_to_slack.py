import json
import tempfile
import unittest
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from scripts import app_store_reviews_to_slack as reviews


def review(review_id, rating=5, **attributes):
    return {
        "id": review_id,
        "attributes": {
            "rating": rating,
            "title": "Great app",
            "body": "Love it",
            "reviewerNickname": "Happy customer",
            "createdDate": "2026-08-25T12:00:00Z",
            "territory": "USA",
            **attributes,
        },
    }


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTTPError("https://example.test", status, "temporary error", headers, BytesIO(b"temporary error"))


def json_response(payload):
    response = StringIO(json.dumps(payload))
    response.status = 200
    return response


class AppStoreReviewsToSlackTests(unittest.TestCase):
    def test_get_app_id_queries_by_bundle_id(self):
        with patch.object(reviews, "request_json", return_value={"data": [{"id": "123"}]}) as request_json:
            app_id = reviews.get_app_id("token", "com.example.myapp")

        self.assertEqual(app_id, "123")
        self.assertIn("filter%5BbundleId%5D=com.example.myapp", request_json.call_args.args[0])

    def test_request_json_retries_transient_failures_with_a_timeout(self):
        with (
            patch.object(
                reviews.urllib.request,
                "urlopen",
                side_effect=[http_error(503), json_response({"data": []})],
            ) as urlopen,
            patch.object(reviews.time, "sleep") as sleep,
            patch.object(reviews.sys, "stderr", new=StringIO()),
        ):
            self.assertEqual(reviews.request_json("https://example.test", "token"), {"data": []})

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], reviews.REQUEST_TIMEOUT_SECONDS)
        sleep.assert_called_once_with(1)

    def test_slack_retries_with_retry_after_and_does_not_retry_client_errors(self):
        successful_response = MagicMock()
        successful_response.__enter__.return_value.status = 200
        with (
            patch.object(
                reviews.urllib.request,
                "urlopen",
                side_effect=[http_error(429, retry_after=7), successful_response],
            ) as urlopen,
            patch.object(reviews.time, "sleep") as sleep,
            patch.object(reviews.sys, "stderr", new=StringIO()),
        ):
            reviews.post_to_slack("https://hooks.slack.com/services/example", review("123"))

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(7)

        with (
            patch.object(reviews.urllib.request, "urlopen", side_effect=http_error(400)) as urlopen,
            patch.object(reviews.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Slack webhook returned 400"),
        ):
            reviews.post_to_slack("https://hooks.slack.com/services/example", review("123"))

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_get_reviews_returns_new_reviews_across_pages_until_the_watermark(self):
        first_page = "https://example.test/first"
        second_page = "https://example.test/second"
        responses = {
            first_page: {"data": [review("newest"), review("newer")], "links": {"next": second_page}},
            second_page: {"data": [review("watermark"), review("older")]},
        }

        with patch.object(reviews, "request_json", side_effect=lambda url, _token: responses.get(url, responses[first_page])):
            newest_id, unseen, watermark_found = reviews.get_reviews("token", "123", "watermark")

        self.assertEqual(newest_id, "newest")
        self.assertEqual([item["id"] for item in unseen], ["newest", "newer"])
        self.assertTrue(watermark_found)

    def test_get_reviews_refuses_to_assume_a_missing_watermark_is_safe(self):
        with patch.object(reviews, "request_json", return_value={"data": [review("newest")]}):
            _newest_id, _unseen, watermark_found = reviews.get_reviews("token", "123", "missing")

        self.assertFalse(watermark_found)

    def test_state_round_trip_includes_the_app_id_and_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            reviews.write_state(path, "123", "review-456")

            state = reviews.read_state(path)

        self.assertEqual(state["app_id"], "123")
        self.assertEqual(state["last_seen_review_id"], "review-456")

    def test_slack_payload_escapes_control_characters_limits_the_body_and_includes_the_review_id(self):
        payload = reviews.slack_payload(
            review(
                "123",
                rating=3,
                title="A < B & C",
                body="x" * 4000,
                reviewerNickname="Kim & Lee",
            )
        )

        self.assertEqual(payload["text"], "New App Store review: 3/5 from Kim &amp; Lee")
        self.assertEqual(payload["blocks"][0]["text"]["text"], "New App Store review — ★★★☆☆")
        self.assertIn("A &lt; B &amp; C", payload["blocks"][1]["text"]["text"])
        self.assertLessEqual(len(payload["blocks"][1]["text"]["text"]), 3000)
        self.assertIn({"type": "mrkdwn", "text": "*Review ID*\n123"}, payload["blocks"][1]["fields"])

    def test_read_state_rejects_an_unknown_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"schema_version": 2, "app_id": "123", "last_seen_review_id": "456"}))

            with self.assertRaisesRegex(RuntimeError, "invalid review state"):
                reviews.read_state(path)


if __name__ == "__main__":
    unittest.main()
