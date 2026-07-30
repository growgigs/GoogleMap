"""
Check a list of businesses for recent 1 or 2-star Google Maps reviews
that include actual written text (not just a bare star rating).

Reads businesses from a CSV file, pulls each one's newest reviews via
Apify's Google Maps Reviews Scraper, and writes out a CSV listing only the
businesses that have a 1 or 2-star review, with review text, posted within
the last DAYS_THRESHOLD days. Businesses with no matching review are
skipped entirely - they will not appear in the output at all.

Input file (see businesses_example.csv for the template):
  Columns: business_name, city, state, maps_url
  - If you have a direct Google Maps URL for the business, put it in the
    maps_url column and leave the other three blank.
  - Otherwise, fill in business_name, city, and state (all three - name
    alone risks matching the wrong branch of a chain) and leave maps_url
    blank.

How to use:
1. Set your Apify API token as an environment variable:
     export APIFY_API_TOKEN="your_token_here"
2. Put your businesses in businesses.csv (or change INPUT_CSV below).
3. Run:
     python check_businesses.py
4. Results land in flagged_businesses.csv (or change OUTPUT_CSV below).
"""

import csv
import os
import sys
import urllib.parse
from datetime import datetime, timezone

import requests

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")
ACTOR_ID = "compass~google-maps-reviews-scraper"

INPUT_CSV = "businesses.csv"
OUTPUT_CSV = "flagged_businesses.csv"

# Easy to change: only flag reviews posted within this many days.
DAYS_THRESHOLD = 21

# How many of the newest reviews to pull per business. 20 is plenty since
# we sort newest-first and only care about the last DAYS_THRESHOLD days.
MAX_REVIEWS_PER_BUSINESS = 20

LOW_STARS = {1, 2}

OUTPUT_FIELDS = ["Business Name", "Reviewer Name", "Star Rating", "Review Age", "Matched Place URL"]


def parse_iso(date_str):
    return datetime.fromisoformat(date_str.replace("Z", "+00:00"))


def describe_age(days_old):
    if days_old <= 0:
        return "today"
    if days_old == 1:
        return "yesterday"
    return f"{days_old} days ago"


def resolve_start_url(business_name, city, state, maps_url):
    """Return the Apify-ready URL for this row, or None if the row doesn't
    have enough information (a maps_url, or all of name+city+state)."""
    maps_url = (maps_url or "").strip()
    if maps_url:
        return maps_url

    business_name = (business_name or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    if not (business_name and city and state):
        return None

    query = f"{business_name}, {city}, {state}"
    return f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"


def fetch_reviews(start_url):
    endpoint = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
    run_input = {
        "startUrls": [{"url": start_url}],
        "maxReviews": MAX_REVIEWS_PER_BUSINESS,
        "reviewsSort": "newest",
        "language": "en",
    }
    response = requests.post(
        endpoint,
        params={"token": APIFY_API_TOKEN},
        json=run_input,
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def find_flagged_review(reviews):
    """Return (review, days_old) for the newest review that has a low star
    rating (LOW_STARS), includes actual written text, and is within
    DAYS_THRESHOLD days, or (None, None) if there isn't one. Reviews are
    already newest-first, so the first match is the one we want."""
    now = datetime.now(timezone.utc)
    for review in reviews:
        stars = review.get("stars")
        published_at_date = review.get("publishedAtDate")
        has_text = bool((review.get("text") or "").strip())
        if stars not in LOW_STARS or not published_at_date or not has_text:
            continue
        days_old = (now - parse_iso(published_at_date)).days
        if days_old <= DAYS_THRESHOLD:
            return review, days_old
    return None, None


def describe_error(exc):
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            return response.json().get("error", {}).get("message", "") or str(exc)
        except ValueError:
            return response.text[:200]
    return str(exc)


def main():
    if not APIFY_API_TOKEN:
        print("ERROR: The APIFY_API_TOKEN environment variable isn't set.")
        print('Set it first, e.g.: export APIFY_API_TOKEN="your_token_here"')
        sys.exit(1)

    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: Can't find {INPUT_CSV}. Create it first (see businesses_example.csv).")
        sys.exit(1)

    flagged_rows = []

    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            business_name = row.get("business_name", "") or ""
            city = row.get("city", "") or ""
            state = row.get("state", "") or ""
            maps_url = row.get("maps_url", "") or ""

            label = business_name.strip() or maps_url.strip()
            if not label:
                continue  # blank row

            start_url = resolve_start_url(business_name, city, state, maps_url)
            if not start_url:
                print(f"SKIPPING '{label}': needs either a maps_url, or business_name + city + state all filled in.")
                continue

            print(f"Checking {label}...")
            try:
                reviews = fetch_reviews(start_url)
            except requests.exceptions.RequestException as e:
                print(f"  Could not fetch reviews for {label}: {describe_error(e)}")
                continue

            if not reviews:
                print("  No reviews found at all, skipping.")
                continue

            flagged, days_old = find_flagged_review(reviews)
            if not flagged:
                print(f"  No 1-2 star review with text in the last {DAYS_THRESHOLD} days. Skipping.")
                continue

            resolved_name = business_name.strip() or reviews[0].get("title", label)
            age = describe_age(days_old)
            matched_place_url = reviews[0].get("url") or start_url
            print(f"  FLAGGED: {flagged.get('stars')} stars, {age}, by {flagged.get('name')}")
            print(f"  Matched place: {matched_place_url}")

            flagged_rows.append({
                "Business Name": resolved_name,
                "Reviewer Name": flagged.get("name", ""),
                "Star Rating": flagged.get("stars", ""),
                "Review Age": age,
                "Matched Place URL": matched_place_url,
            })

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(flagged_rows)

    print(f"\nDone. {len(flagged_rows)} business(es) flagged. Results in {OUTPUT_CSV}.")


if __name__ == "__main__":
    main()
