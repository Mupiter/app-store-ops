#!/usr/bin/env python3
"""Post new App Store customer reviews to Slack and maintain a watermark."""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .app_store_connect import make_token
else:
    from app_store_connect import make_token


API = "https://api.appstoreconnect.apple.com"


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
        with urllib.request.urlopen(request) as response:
            return json.load(response) if response.status != 204 else {}
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


def get_reviews(token, app_id, watermark):
    """Return every review newer than ``watermark``, newest first from Apple."""
    query = urllib.parse.urlencode(
        {
            "fields[customerReviews]": "rating,title,body,reviewerNickname,createdDate,territory",
            "limit": 200,
            "sort": "-createdDate",
        }
    )
    url = f"{API}/v1/apps/{app_id}/customerReviews?{query}"
    newest_id = None
    unseen = []

    while url:
        response = request_json(url, token)
        reviews = response["data"]
        if newest_id is None and reviews:
            newest_id = reviews[0]["id"]
        for review in reviews:
            if watermark is not None and review["id"] == watermark:
                return newest_id, unseen, True
            unseen.append(review)
        url = response.get("links", {}).get("next")

    return newest_id, unseen, watermark is None


def read_state(path):
    """Read and validate the persisted review watermark, if it exists."""
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if state.get("schema_version") != 1 or "last_seen_review_id" not in state or "app_id" not in state:
        raise RuntimeError(f"invalid review state in {path}")
    return state


def write_state(path, app_id, review_id):
    """Atomically persist the latest review ID after a successful run."""
    state = {
        "schema_version": 1,
        "app_id": app_id,
        "last_seen_review_id": review_id,
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
        with urllib.request.urlopen(request) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Slack webhook returned {response.status}")
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

    watermark = state["last_seen_review_id"] if state else None
    newest_id, unseen, watermark_found = get_reviews(token, app_id, watermark)

    if state is None:
        write_state(args.state_file, app_id, newest_id)
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

    write_state(args.state_file, app_id, newest_id)
    print(f"Posted {len(unseen)} new App Store review(s) to Slack.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
