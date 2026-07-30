"""
Shared logic for checking a business's recent Google Maps reviews via
Apify's Google Maps Reviews Scraper. Used by both check_businesses.py
(the command-line version) and dashboard.py (the web dashboard), so the
actual checking rules only exist in one place.
"""

import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

ACTOR_ID = "compass~google-maps-reviews-scraper"

# Easy to change: only flag reviews posted within this many days.
DAYS_THRESHOLD = 21

# How many of the newest reviews to pull per business. 20 is plenty since
# we sort newest-first and only care about the last DAYS_THRESHOLD days.
MAX_REVIEWS_PER_BUSINESS = 20

# How many businesses to check at the same time. This only affects how
# fast a big list finishes - Apify charges the same either way (per actor
# run started + per review scraped, not by time or concurrency). If you
# see errors mentioning concurrent runs or rate limits, lower this.
MAX_CONCURRENT_CHECKS = 10

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
    """Return the Apify-ready URL for this business, or None if we don't
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


def fetch_reviews(api_token, start_url):
    endpoint = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
    run_input = {
        "startUrls": [{"url": start_url}],
        "maxReviews": MAX_REVIEWS_PER_BUSINESS,
        "reviewsSort": "newest",
        "language": "en",
    }
    response = requests.post(
        endpoint,
        params={"token": api_token},
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


def check_business(api_token, business_name="", city="", state="", maps_url=""):
    """Run the full check for one business. Always returns a dict with a
    "status" key, one of:
      "blank"   - nothing was given for this business, nothing to do
      "skipped" - not enough info to look it up (see "reason")
      "error"   - the Apify call failed (see "reason")
      "clean"   - checked successfully, nothing to flag (see "reason")
      "flagged" - checked successfully, found a matching review (see "row")
    "label" is a human-readable name for this business, present on every
    status except "blank"."""
    label = (business_name or "").strip() or (maps_url or "").strip()
    if not label:
        return {"status": "blank"}

    start_url = resolve_start_url(business_name, city, state, maps_url)
    if not start_url:
        return {
            "status": "skipped",
            "label": label,
            "reason": "needs either a maps_url, or business_name + city + state all filled in.",
        }

    try:
        reviews = fetch_reviews(api_token, start_url)
    except requests.exceptions.RequestException as e:
        return {"status": "error", "label": label, "reason": describe_error(e)}

    if not reviews:
        return {"status": "clean", "label": label, "reason": "No reviews found at all."}

    flagged, days_old = find_flagged_review(reviews)
    if not flagged:
        return {
            "status": "clean",
            "label": label,
            "reason": f"No 1-2 star review with text in the last {DAYS_THRESHOLD} days.",
        }

    resolved_name = (business_name or "").strip() or reviews[0].get("title", label)
    age = describe_age(days_old)
    matched_place_url = reviews[0].get("url") or start_url

    return {
        "status": "flagged",
        "label": label,
        "row": {
            "Business Name": resolved_name,
            "Reviewer Name": flagged.get("name", ""),
            "Star Rating": flagged.get("stars", ""),
            "Review Age": age,
            "Matched Place URL": matched_place_url,
        },
    }


def check_businesses_parallel(api_token, businesses, max_workers=MAX_CONCURRENT_CHECKS):
    """Check many businesses at once instead of one at a time.

    businesses: a list of (business_name, city, state, maps_url) tuples.
    Yields (index, result) as each business finishes - in completion order,
    not necessarily the order given - so callers can show live progress
    while work happens in the background. `index` is each business's
    position in the original `businesses` list, so callers can put results
    back in the original order afterwards if they want to."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(check_business, api_token, name, city, state, maps_url): i
            for i, (name, city, state, maps_url) in enumerate(businesses)
        }
        for future in as_completed(future_to_index):
            yield future_to_index[future], future.result()
