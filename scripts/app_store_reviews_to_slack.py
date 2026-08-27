#!/usr/bin/env python3
"""Post new App Store customer reviews to Slack and maintain a watermark."""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .app_store_connect import make_token
else:
    from app_store_connect import make_token


API = "https://api.appstoreconnect.apple.com"
REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 60
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ReviewWatermark:
    """The complete set of reviews at the newest observed timestamp."""

    created_date: str | None
    review_ids: frozenset[str]


def retry_delay_seconds(error, attempt):
    """Return a bounded retry delay, honoring a numeric Retry-After header."""
    retry_after = error.headers.get("Retry-After") if getattr(error, "headers", None) else None
    if retry_after and retry_after.strip().isdigit():
        return min(int(retry_after), MAX_RETRY_DELAY_SECONDS)
    return min(2**attempt, MAX_RETRY_DELAY_SECONDS)


def request_with_retry(request, service, consume_response):
    """Perform and consume a request with a timeout and bounded transient retries."""
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return consume_response(response)
        except urllib.error.HTTPError as error:
            failure = error
            retryable = error.code in RETRYABLE_HTTP_STATUS_CODES
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionResetError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
        ) as error:
            failure = error
            retryable = True

        if not retryable or attempt == MAX_REQUEST_ATTEMPTS - 1:
            raise failure

        delay = retry_delay_seconds(failure, attempt)
        print(f"{service} request failed ({failure}); retrying in {delay} seconds.", file=sys.stderr)
        if isinstance(failure, urllib.error.HTTPError):
            failure.close()
        time.sleep(delay)


def request_json(url, token, method="GET", body=None):
    """Call App Store Connect and return its JSON response with useful errors."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        return request_with_retry(
            request,
            "App Store Connect",
            lambda response: json.load(response) if response.status != 204 else {},
        )
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:600]
        raise RuntimeError(f"{method} {url} -> {error.code}: {detail}") from error


def get_app_id(token, bundle_id):
    """Look up the App Store Connect app ID for a bundle identifier."""
    query = urllib.parse.urlencode({"filter[bundleId]": bundle_id, "limit": 2})
    apps = request_json(f"{API}/v1/apps?{query}", token)["data"]
    if len(apps) != 1:
        raise RuntimeError(f"expected one app for bundle ID {bundle_id}, found {len(apps)}")
    return apps[0]["id"]


def parse_created_date(created_date, source):
    """Parse an App Store Connect date-time with its required UTC offset."""
    if not isinstance(created_date, str) or not created_date:
        raise RuntimeError(f"{source} has an invalid createdDate")
    try:
        normalized_date = f"{created_date[:-1]}+00:00" if created_date.endswith("Z") else created_date
        parsed = datetime.fromisoformat(normalized_date)
    except ValueError as error:
        raise RuntimeError(f"{source} has an invalid createdDate") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{source} has an invalid createdDate")
    return parsed


def review_created_date(review):
    """Return a review's serialized and offset-aware creation timestamp."""
    created_date = review["attributes"]["createdDate"]
    return created_date, parse_created_date(created_date, f"review {review['id']}")


def get_reviews(token, app_id, watermark):
    """Return a new watermark, unseen reviews, and whether an existing boundary was found."""
    query = urllib.parse.urlencode(
        {
            "fields[customerReviews]": "rating,title,body,reviewerNickname,createdDate,territory",
            "limit": 200,
            "sort": "-createdDate",
        }
    )
    url = f"{API}/v1/apps/{app_id}/customerReviews?{query}"
    newest_created_date = None
    newest_created_at = None
    newest_review_ids = set()
    unseen = []
    watermark_found = watermark is None or watermark.created_date is None
    watermark_created_at = (
        parse_created_date(watermark.created_date, "review watermark")
        if watermark is not None and watermark.created_date is not None
        else None
    )

    def newest_watermark():
        return ReviewWatermark(newest_created_date, frozenset(newest_review_ids))

    while url:
        response = request_json(url, token)
        reviews = response["data"]
        for review in reviews:
            created_date, created_at = review_created_date(review)
            if newest_created_date is None:
                newest_created_date = created_date
                newest_created_at = created_at
            if created_at == newest_created_at:
                newest_review_ids.add(review["id"])

            if watermark is None:
                # Bootstrap only needs the newest timestamp group, not the full history.
                if created_at < newest_created_at:
                    return newest_watermark(), [], True
                continue

            if watermark.created_date is None:
                unseen.append(review)
                continue
            if created_at > watermark_created_at:
                unseen.append(review)
                continue
            if created_at == watermark_created_at:
                watermark_found = True
                if review["id"] not in watermark.review_ids:
                    unseen.append(review)
                continue
            return newest_watermark(), unseen, watermark_found
        url = response.get("links", {}).get("next")

    return newest_watermark(), unseen, watermark_found


def read_state(path):
    """Read and validate the persisted review watermark, if it exists."""
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if (
        state.get("schema_version") != 2
        or "app_id" not in state
        or "last_seen_review_created_date" not in state
        or "last_seen_review_ids" not in state
        or (
            state["last_seen_review_created_date"] is not None
            and (not isinstance(state["last_seen_review_created_date"], str) or not state["last_seen_review_created_date"])
        )
        or not isinstance(state["last_seen_review_ids"], list)
        or not all(isinstance(review_id, str) and review_id for review_id in state["last_seen_review_ids"])
        or len(state["last_seen_review_ids"]) != len(set(state["last_seen_review_ids"]))
        or (state["last_seen_review_created_date"] is None and state["last_seen_review_ids"])
    ):
        raise RuntimeError(f"invalid review state in {path}")
    return state


def state_watermark(state):
    """Return the current timestamp-boundary watermark from valid state."""
    return ReviewWatermark(
        state["last_seen_review_created_date"],
        frozenset(state["last_seen_review_ids"]),
    )


def write_state(path, app_id, watermark):
    """Atomically persist a complete timestamp-boundary watermark."""
    state = {
        "schema_version": 2,
        "app_id": app_id,
        "last_seen_review_created_date": watermark.created_date,
        "last_seen_review_ids": sorted(watermark.review_ids),
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state, indent=2) + "\n")
    temporary_path.replace(path)


def slack_escape(value):
    """Escape the three characters Slack treats as markup control characters."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(review):
    """Format one App Store customer review as a Slack Block Kit message."""
    attributes = review["attributes"]
    rating = int(attributes["rating"])
    if rating < 1 or rating > 5:
        raise ValueError(f"review rating must be between 1 and 5, got {rating}")

    stars = "★" * rating + "☆" * (5 - rating)
    nickname = slack_escape(attributes.get("reviewerNickname") or "App Store customer")
    territory = slack_escape(attributes.get("territory") or "Unknown territory")
    created = slack_escape(attributes.get("createdDate") or "Unknown date")
    review_id = slack_escape(review["id"])
    title = slack_escape(attributes.get("title") or "Untitled review")
    body = slack_escape(attributes.get("body") or "No review text provided.")
    review_text = f"*{title}*\n{body}"
    return {
        "text": f"New App Store review: {rating}/5 from {nickname}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"New App Store review — {stars}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": review_text[:3000]},
                "fields": [
                    {"type": "mrkdwn", "text": f"*Reviewer*\n{nickname}"},
                    {"type": "mrkdwn", "text": f"*Storefront*\n{territory}"},
                    {"type": "mrkdwn", "text": f"*Created*\n{created}"},
                    {"type": "mrkdwn", "text": f"*Review ID*\n{review_id}"},
                ],
            },
        ],
    }


def post_to_slack(webhook_url, review):
    """Deliver one review notification and fail the run when Slack rejects it."""
    payload = json.dumps(slack_payload(review)).encode()
    request = urllib.request.Request(
        webhook_url,
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        def require_success(response):
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Slack webhook returned {response.status}")

        request_with_retry(request, "Slack", require_success)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:600]
        raise RuntimeError(f"Slack webhook returned {error.code}: {detail}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True, help="App bundle identifier, for example com.example.myapp")
    parser.add_argument("--key-path", required=True, type=Path, help="Path to the App Store Connect .p8 key")
    parser.add_argument("--key-id", required=True, help="App Store Connect API key ID")
    parser.add_argument("--issuer-id", required=True, help="App Store Connect issuer ID")
    parser.add_argument("--state-file", required=True, type=Path, help="Path to the review watermark JSON file")
    args = parser.parse_args()

    token = make_token(args.key_path, args.key_id, args.issuer_id)
    app_id = get_app_id(token, args.bundle_id)
    state = read_state(args.state_file)
    if state is not None and state["app_id"] != app_id:
        raise RuntimeError("review state belongs to a different App Store Connect app")

    watermark = state_watermark(state) if state else None
    newest_watermark, unseen, watermark_found = get_reviews(token, app_id, watermark)

    if state is None:
        write_state(args.state_file, app_id, newest_watermark)
        print("No review state found; saved the current watermark without posting historic reviews.")
        return

    if not watermark_found:
        raise RuntimeError("review state watermark was not returned by App Store Connect; refusing to repost history")
    if not unseen:
        print("No new App Store reviews.")
        return

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL is required when posting reviews")
    for review in reversed(unseen):
        post_to_slack(webhook_url, review)

    write_state(args.state_file, app_id, newest_watermark)
    print(f"Posted {len(unseen)} new App Store review(s) to Slack.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
