"""
Step 1 of the Google Maps review checker.

The ONLY goal of this script is to prove the connection to Apify works
and returns real review data for ONE business. There is no filtering,
no CSV output, and no list processing yet - that comes later, once we've
confirmed this part works.

How to use:
1. Set your Apify API token as an environment variable (don't paste it
   into this file). In a terminal, before running the script:

     export APIFY_API_TOKEN="your_token_here"

2. Edit BUSINESS_INPUT below to either:
   - a business name, e.g. "Toyota of Cedar Park", or
   - a full Google Maps URL (must contain /maps/place, /maps/search,
     or /maps/reviews - a plain google.com search link or a shortened
     share.google link won't work).

3. Run it:

     python test_single_business.py
"""

import json
import os
import sys
import urllib.parse

import requests

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

# The Apify actor (their term for a scraper) we're calling.
ACTOR_ID = "compass~google-maps-reviews-scraper"

# Either a business name or a full Google Maps URL. See the docstring above.
BUSINESS_INPUT = "Toyota of Cedar Park"


def resolve_start_url(business_input):
    """Turn a plain business name into a Google Maps search URL Apify accepts.
    If it's already a URL, use it as-is."""
    if business_input.startswith("http://") or business_input.startswith("https://"):
        return business_input
    encoded_name = urllib.parse.quote(business_input)
    return f"https://www.google.com/maps/search/{encoded_name}"


def main():
    if not APIFY_API_TOKEN:
        print("ERROR: The APIFY_API_TOKEN environment variable isn't set.")
        print('Set it first, e.g.: export APIFY_API_TOKEN="your_token_here"')
        sys.exit(1)

    start_url = resolve_start_url(BUSINESS_INPUT)
    print(f"Business input: {BUSINESS_INPUT}")
    print(f"Resolved start URL sent to Apify: {start_url}\n")

    # This is the input Apify's Google Maps Reviews Scraper expects.
    # We ask for newest reviews first and cap it at 20 for this test run.
    run_input = {
        "startUrls": [{"url": start_url}],
        "maxReviews": 20,
        "reviewsSort": "newest",
        "language": "en",
    }

    endpoint = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

    print("Calling Apify... this runs the scraper live, so it can take 15-60 seconds.")
    response = requests.post(
        endpoint,
        params={"token": APIFY_API_TOKEN},
        json=run_input,
        timeout=300,
    )
    response.raise_for_status()
    reviews = response.json()

    print(f"\nGot {len(reviews)} review(s) back from Apify.\n")

    if not reviews:
        print("No reviews came back. Double check BUSINESS_INPUT resolves to a real place.")
        return

    business_name = reviews[0].get("title")
    print(f"Business found on Google Maps: {business_name}\n")

    print("Quick summary of what we got (this is what will become CSV rows later):\n")
    for r in reviews:
        print(
            f"- {r.get('stars')} stars | {r.get('publishedAtDate')} | {r.get('name')}: "
            f"{(r.get('text') or '')[:80]!r}"
        )

    print("\nHere is the FIRST review, exactly as Apify returned it, so we can")
    print("see every real field name before we build the filtering logic:\n")
    print(json.dumps(reviews[0], indent=2))


if __name__ == "__main__":
    main()
