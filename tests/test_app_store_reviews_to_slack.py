import json
import tempfile
import unittest
from email.message import Message
from http.client import IncompleteRead
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


def truncated_json_response():
    response = MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.side_effect = IncompleteRead(b'{"data":', 3)
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

    def test_request_json_retries_when_the_response_body_is_truncated(self):
        with (
            patch.object(
                reviews.urllib.request,
                "urlopen",
                side_effect=[truncated_json_response(), json_response({"data": []})],
            ) as urlopen,
            patch.object(reviews.time, "sleep") as sleep,
            patch.object(reviews.sys, "stderr", new=StringIO()),
        ):
            self.assertEqual(reviews.request_json("https://example.test", "token"), {"data": []})

        self.assertEqual(urlopen.call_count, 2)
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
            first_page: {
                "data": [
                    review("newest", createdDate="2026-08-25T12:00:02Z"),
                    review("newer", createdDate="2026-08-25T12:00:01Z"),
                ],
                "links": {"next": second_page},
            },
            second_page: {
                "data": [
                    review("watermark", createdDate="2026-08-25T12:00:00Z"),
                    review("older", createdDate="2026-08-25T11:59:59Z"),
                ]
            },
        }

        with patch.object(reviews, "request_json", side_effect=lambda url, _token: responses.get(url, responses[first_page])):
            newest_watermark, unseen, watermark_found = reviews.get_reviews(
                "token",
                "123",
                reviews.ReviewWatermark("2026-08-25T12:00:00Z", frozenset({"watermark"})),
            )

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark("2026-08-25T12:00:02Z", frozenset({"newest"})),
        )
        self.assertEqual([item["id"] for item in unseen], ["newest", "newer"])
        self.assertTrue(watermark_found)

    def test_get_reviews_processes_an_entire_tied_timestamp_group(self):
        tied_timestamp = "2026-08-25T12:00:00Z"
        response = {
            "data": [
                review("watermark", createdDate=tied_timestamp),
                review("new-review", createdDate=tied_timestamp),
                review("older", createdDate="2026-08-25T11:59:59Z"),
            ]
        }

        with patch.object(reviews, "request_json", return_value=response):
            newest_watermark, unseen, watermark_found = reviews.get_reviews(
                "token",
                "123",
                reviews.ReviewWatermark(tied_timestamp, frozenset({"watermark"})),
            )

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark(tied_timestamp, frozenset({"watermark", "new-review"})),
        )
        self.assertEqual([item["id"] for item in unseen], ["new-review"])
        self.assertTrue(watermark_found)

    def test_get_reviews_compares_created_dates_by_instant_across_utc_offsets(self):
        saved_timestamp = "2026-11-01T01:55:00-07:00"
        newer_timestamp = "2026-11-01T01:05:00-08:00"
        response = {
            "data": [
                review("new-review", createdDate=newer_timestamp),
                review("watermark", createdDate=saved_timestamp),
                review("older", createdDate="2026-11-01T00:50:00-08:00"),
            ]
        }

        with patch.object(reviews, "request_json", return_value=response):
            newest_watermark, unseen, watermark_found = reviews.get_reviews(
                "token",
                "123",
                reviews.ReviewWatermark(saved_timestamp, frozenset({"watermark"})),
            )

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark(newer_timestamp, frozenset({"new-review"})),
        )
        self.assertEqual([item["id"] for item in unseen], ["new-review"])
        self.assertTrue(watermark_found)

    def test_initial_bootstrap_stops_after_the_newest_timestamp_group(self):
        first_page = "https://example.test/first"
        second_page = "https://example.test/second"
        responses = {
            first_page: {
                "data": [
                    review("newest", createdDate="2026-08-25T12:00:00Z"),
                    review("older", createdDate="2026-08-25T11:59:59Z"),
                ],
                "links": {"next": second_page},
            },
            second_page: {"data": [review("historic", createdDate="2025-01-01T00:00:00Z")]},
        }

        with patch.object(
            reviews,
            "request_json",
            side_effect=lambda url, _token: responses.get(url, responses[first_page]),
        ) as request_json:
            newest_watermark, unseen, watermark_found = reviews.get_reviews("token", "123", None)

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark("2026-08-25T12:00:00Z", frozenset({"newest"})),
        )
        self.assertEqual(unseen, [])
        self.assertTrue(watermark_found)
        request_json.assert_called_once()
        self.assertIn("/v1/apps/123/customerReviews?", request_json.call_args.args[0])

    def test_initial_bootstrap_collects_a_tied_timestamp_group_across_pages(self):
        first_page = "https://example.test/first"
        second_page = "https://example.test/second"
        tied_timestamp = "2026-08-25T12:00:00Z"
        responses = {
            first_page: {"data": [review("first", createdDate=tied_timestamp)], "links": {"next": second_page}},
            second_page: {
                "data": [
                    review("second", createdDate=tied_timestamp),
                    review("older", createdDate="2026-08-25T11:59:59Z"),
                ]
            },
        }

        with patch.object(
            reviews,
            "request_json",
            side_effect=lambda url, _token: responses.get(url, responses[first_page]),
        ) as request_json:
            newest_watermark, unseen, watermark_found = reviews.get_reviews("token", "123", None)

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark(tied_timestamp, frozenset({"first", "second"})),
        )
        self.assertEqual(unseen, [])
        self.assertTrue(watermark_found)
        self.assertEqual(request_json.call_count, 2)

    def test_get_reviews_refuses_to_assume_a_missing_watermark_is_safe(self):
        with patch.object(reviews, "request_json", return_value={"data": [review("newest")]}):
            _newest_watermark, _unseen, watermark_found = reviews.get_reviews(
                "token",
                "123",
                reviews.ReviewWatermark("2026-08-24T12:00:00Z", frozenset({"missing"})),
            )

        self.assertFalse(watermark_found)

    def test_an_empty_existing_watermark_posts_the_first_reviews_that_arrive(self):
        with patch.object(reviews, "request_json", return_value={"data": [review("first-review")]}):
            newest_watermark, unseen, watermark_found = reviews.get_reviews(
                "token",
                "123",
                reviews.ReviewWatermark(None, frozenset()),
            )

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark("2026-08-25T12:00:00Z", frozenset({"first-review"})),
        )
        self.assertEqual([item["id"] for item in unseen], ["first-review"])
        self.assertTrue(watermark_found)

    def test_a_v1_watermark_migrates_after_processing_its_complete_tied_group(self):
        tied_timestamp = "2026-08-25T12:00:00Z"
        response = {
            "data": [
                review("legacy-watermark", createdDate=tied_timestamp),
                review("new-review", createdDate=tied_timestamp),
                review("older", createdDate="2026-08-25T11:59:59Z"),
            ]
        }

        with patch.object(reviews, "request_json", return_value=response):
            newest_watermark, unseen, watermark_found = reviews.get_reviews(
                "token",
                "123",
                reviews.LegacyReviewWatermark("legacy-watermark"),
            )

        self.assertEqual(
            newest_watermark,
            reviews.ReviewWatermark(tied_timestamp, frozenset({"legacy-watermark", "new-review"})),
        )
        self.assertEqual([item["id"] for item in unseen], ["new-review"])
        self.assertTrue(watermark_found)

    def test_state_round_trip_includes_the_app_id_and_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            reviews.write_state(
                path,
                "123",
                reviews.ReviewWatermark("2026-08-25T12:00:00Z", frozenset({"review-456", "review-789"})),
            )

            state = reviews.read_state(path)

        self.assertEqual(state["app_id"], "123")
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["last_seen_review_created_date"], "2026-08-25T12:00:00Z")
        self.assertEqual(state["last_seen_review_ids"], ["review-456", "review-789"])

    def test_reads_a_v1_state_for_safe_timestamp_boundary_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"schema_version": 1, "app_id": "123", "last_seen_review_id": "review-456"}))

            state = reviews.read_state(path)

        self.assertEqual(reviews.state_watermark(state), reviews.LegacyReviewWatermark("review-456"))

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
