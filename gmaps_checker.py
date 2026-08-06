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

REVIEWS_ACTOR_ID = "compass~google-maps-reviews-scraper"
PLACES_ACTOR_ID = "compass~crawler-google-places"

# Easy to change: only flag reviews posted within this many days. This is
# also sent to Apify as reviewsStartDate, so it stops pulling a business's
# reviews as soon as it reaches one older than this - not just a client-side
# filter, an actual cost saver.
DAYS_THRESHOLD = 21

# Hard per-business cap on reviews pulled, regardless of reviewsStartDate.
# This exists purely for cost safety: a very popular/viral business could
# otherwise rack up hundreds of reviews within DAYS_THRESHOLD days. 40 is
# chosen so that even in the worst case (every business hits this cap),
# the combined search+review cost stays under HARD_COST_CAP_PER_1000 - see
# estimate_search_cost() below.
MAX_REVIEWS_PER_BUSINESS = 40

# How many businesses to check at the same time. This only affects how
# fast a big list finishes - Apify charges the same either way (per actor
# run started + per review scraped, not by time or concurrency). If you
# see errors mentioning concurrent runs or rate limits, lower this.
MAX_CONCURRENT_CHECKS = 10

LOW_STARS = {1, 2}

OUTPUT_FIELDS = ["Business Name", "Website", "Reviewer Name", "Star Rating", "Review Age", "Matched Place URL"]

# Recognized alternate spellings for each input column, so a CSV exported
# from a spreadsheet (different capitalization, wording, or a typo like
# "Goolge Map URL") is still understood instead of silently matching
# nothing. Matching is case-insensitive and ignores spaces/punctuation.
COLUMN_ALIASES = {
    "business_name": {"businessname", "name", "business"},
    "city": {"city"},
    "state": {"state", "stateprovince", "province"},
    "maps_url": {
        "mapsurl", "url", "link", "googlemapurl", "googlemapsurl",
        "goolgemapurl", "mapurl", "gmapsurl", "googlemapslink", "mapslink",
        "googlemap", "googlemaps", "gmaps", "maps",
    },
}


def _normalize_header(header):
    return "".join(ch for ch in header.strip().lower() if ch.isalnum())


def normalize_business_row(row):
    """Take a CSV DictReader row with possibly differently-named or
    differently-cased columns (e.g. a spreadsheet export) and pull out
    (business_name, city, state, maps_url) using COLUMN_ALIASES. Returns
    empty strings for anything it can't find."""
    normalized = {_normalize_header(k): v for k, v in row.items() if k}
    values = {}
    for field, aliases in COLUMN_ALIASES.items():
        value = ""
        for alias in aliases:
            if normalized.get(alias):
                value = normalized[alias]
                break
        values[field] = value
    return values["business_name"], values["city"], values["state"], values["maps_url"]


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


def fetch_reviews(api_token, start_url, days_threshold=DAYS_THRESHOLD):
    endpoint = f"https://api.apify.com/v2/acts/{REVIEWS_ACTOR_ID}/run-sync-get-dataset-items"
    run_input = {
        "startUrls": [{"url": start_url}],
        "maxReviews": MAX_REVIEWS_PER_BUSINESS,
        "reviewsSort": "newest",
        "reviewsStartDate": f"{days_threshold} days",
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


def find_flagged_review(reviews, days_threshold=DAYS_THRESHOLD):
    """Return (review, days_old) for the newest review that has a low star
    rating (LOW_STARS), includes actual written text, and is within
    days_threshold days, or (None, None) if there isn't one. Reviews are
    already newest-first, so the first match is the one we want. This is a
    client-side backstop - reviewsStartDate in fetch_reviews already does
    the heavy lifting of not pulling older reviews in the first place."""
    now = datetime.now(timezone.utc)
    for review in reviews:
        stars = review.get("stars")
        published_at_date = review.get("publishedAtDate")
        has_text = bool((review.get("text") or "").strip())
        if stars not in LOW_STARS or not published_at_date or not has_text:
            continue
        days_old = (now - parse_iso(published_at_date)).days
        if days_old <= days_threshold:
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


def check_business(api_token, business_name="", city="", state="", maps_url="", days_threshold=DAYS_THRESHOLD, website=""):
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
        reviews = fetch_reviews(api_token, start_url, days_threshold)
    except requests.exceptions.RequestException as e:
        return {"status": "error", "label": label, "reason": describe_error(e)}

    if not reviews:
        return {"status": "clean", "label": label, "reason": "No reviews found at all."}

    flagged, days_old = find_flagged_review(reviews, days_threshold)
    if not flagged:
        return {
            "status": "clean",
            "label": label,
            "reason": f"No 1-2 star review with text in the last {days_threshold} days.",
        }

    resolved_name = (business_name or "").strip() or reviews[0].get("title", label)
    age = describe_age(days_old)
    matched_place_url = reviews[0].get("url") or start_url

    return {
        "status": "flagged",
        "label": label,
        "row": {
            "Business Name": resolved_name,
            "Website": website,
            "Reviewer Name": flagged.get("name", ""),
            "Star Rating": flagged.get("stars", ""),
            "Review Age": age,
            "Matched Place URL": matched_place_url,
        },
    }


def check_businesses_parallel(api_token, businesses, max_workers=MAX_CONCURRENT_CHECKS, days_threshold=DAYS_THRESHOLD):
    """Check many businesses at once instead of one at a time.

    businesses: a list of (business_name, city, state, maps_url) tuples, or
    (business_name, city, state, maps_url, website) tuples if you already
    have the website (e.g. from search_places - free, no extra lookup).
    Yields (index, result) as each business finishes - in completion order,
    not necessarily the order given - so callers can show live progress
    while work happens in the background. `index` is each business's
    position in the original `businesses` list, so callers can put results
    back in the original order afterwards if they want to."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {}
        for i, business in enumerate(businesses):
            name, city, state, maps_url = business[:4]
            website = business[4] if len(business) > 4 else ""
            future = executor.submit(check_business, api_token, name, city, state, maps_url, days_threshold, website)
            future_to_index[future] = i
        for future in as_completed(future_to_index):
            yield future_to_index[future], future.result()


def search_places(api_token, keyword, location, max_places):
    """Find businesses matching a category/keyword in a location, using
    Apify's Google Maps Places Scraper. Returns a list of (business_name,
    city, state, maps_url, website) tuples, ready to feed straight into
    check_businesses_parallel - maps_url is already the exact matched place,
    so no further name-matching/lookup is needed downstream. website comes
    free from this same call - no extra Apify cost."""
    endpoint = f"https://api.apify.com/v2/acts/{PLACES_ACTOR_ID}/run-sync-get-dataset-items"
    run_input = {
        "searchStringsArray": [keyword],
        "locationQuery": location,
        "maxCrawledPlacesPerSearch": max_places,
        "language": "en",
    }
    response = requests.post(
        endpoint,
        params={"token": api_token},
        json=run_input,
        timeout=600,
    )
    response.raise_for_status()
    places = response.json()
    return [
        (
            place.get("title", ""),
            place.get("city", "") or "",
            place.get("state", "") or "",
            place.get("url", ""),
            place.get("website", "") or "",
        )
        for place in places
        if place.get("url")
    ]


# --- Cost estimation, verified against both actors' live Apify pricing ---
#
# compass/crawler-google-places: $0.007 flat per run + $0.004 per place scraped
# compass/google-maps-reviews-scraper: $0.00005 flat per run (one run per
# business) + $0.0006 per review scraped
PLACES_ACTOR_START_PRICE = 0.007
PLACE_SCRAPED_PRICE = 0.004
REVIEWS_ACTOR_START_PRICE = 0.00005
REVIEW_SCRAPED_PRICE = 0.0006

# Real-world sampling (several Miami restaurants, Aug 2026) found anywhere
# from ~3 to ~170 new reviews posted per business in a 21-day window,
# depending on how popular the place is. This number is only used for the
# *typical* estimate shown to the user - MAX_REVIEWS_PER_BUSINESS above is
# what actually protects the budget in the worst case.
TYPICAL_REVIEWS_PER_BUSINESS_ESTIMATE = 15

# The user's hard requirement: never let a run's guaranteed worst-case cost
# exceed this many dollars per 1,000 businesses searched.
HARD_COST_CAP_PER_1000 = 30.0


def estimate_search_cost(num_businesses):
    """Return (typical_total_usd, worst_case_total_usd) for a category/
    location search of num_businesses places, each followed by a reviews
    check. "Worst case" assumes every business hits MAX_REVIEWS_PER_BUSINESS
    - that's the only number we can actually guarantee, so it's what
    check_cost_cap() checks against HARD_COST_CAP_PER_1000."""
    search_total = PLACES_ACTOR_START_PRICE + PLACE_SCRAPED_PRICE * num_businesses
    typical_reviews_total = num_businesses * (
        REVIEWS_ACTOR_START_PRICE
        + REVIEW_SCRAPED_PRICE * min(TYPICAL_REVIEWS_PER_BUSINESS_ESTIMATE, MAX_REVIEWS_PER_BUSINESS)
    )
    worst_case_reviews_total = num_businesses * (REVIEWS_ACTOR_START_PRICE + REVIEW_SCRAPED_PRICE * MAX_REVIEWS_PER_BUSINESS)
    return search_total + typical_reviews_total, search_total + worst_case_reviews_total


def cost_per_1000(total_usd, num_businesses):
    if num_businesses <= 0:
        return 0.0
    return total_usd / num_businesses * 1000


def check_cost_cap(num_businesses):
    """Estimate cost for searching num_businesses places and decide whether
    it's allowed to run. Returns a dict with the typical and worst-case
    totals/per-1000 rates, and "allowed" (bool) - checked against the
    worst case, not the typical estimate, so an unlucky batch of popular
    businesses can't blow past HARD_COST_CAP_PER_1000."""
    typical_total, worst_case_total = estimate_search_cost(num_businesses)
    return {
        "num_businesses": num_businesses,
        "typical_total": typical_total,
        "worst_case_total": worst_case_total,
        "typical_per_1000": cost_per_1000(typical_total, num_businesses),
        "worst_case_per_1000": cost_per_1000(worst_case_total, num_businesses),
        "allowed": cost_per_1000(worst_case_total, num_businesses) <= HARD_COST_CAP_PER_1000,
    }
