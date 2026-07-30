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

2. Edit BUSINESS_URL below to a real Google Maps place URL (open the
   business on Google Maps in your browser and copy the URL from the
   address bar).

3. Run it:

     python test_single_business.py
"""

import json
import os
import sys

import requests

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

# The Apify actor (their term for a scraper) we're calling.
ACTOR_ID = "compass~google-maps-reviews-scraper"

# Paste a real Google Maps place URL here before running.
BUSINESS_URL = "PASTE_A_GOOGLE_MAPS_URL_HERE"


def main():
    if not APIFY_API_TOKEN:
        print("ERROR: The APIFY_API_TOKEN environment variable isn't set.")
        print('Set it first, e.g.: export APIFY_API_TOKEN="your_token_here"')
        sys.exit(1)

    if BUSINESS_URL == "PASTE_A_GOOGLE_MAPS_URL_HERE":
        print("ERROR: Edit BUSINESS_URL in this file to a real Google Maps place URL.")
        sys.exit(1)

    # This is the input Apify's Google Maps Reviews Scraper expects.
    # We ask for newest reviews first and cap it at 20 for this test run.
    run_input = {
        "startUrls": [{"url": BUSINESS_URL}],
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
        print("No reviews came back. Double check BUSINESS_URL is a valid Google Maps place page.")
        return

    print("Here is the FIRST review, exactly as Apify returned it, so we can")
    print("see the real field names before we build the filtering logic:\n")
    print(json.dumps(reviews[0], indent=2))


if __name__ == "__main__":
    main()
