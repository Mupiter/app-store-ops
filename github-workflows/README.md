# GitHub workflow templates

These files are **not** active in the `app-store-ops` repository. Copy the workflow you want into the consuming application's `.github/workflows/` directory, then configure its secrets and variables as described in the [root README](../README.md).

`app-store-reviews.yml` expects these two files in the consuming repository:

```text
scripts/app_store_connect.py
scripts/app_store_reviews_to_slack.py
```
