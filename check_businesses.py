"""
Check a list of businesses for recent 1 or 2-star Google Maps reviews
that include actual written text (not just a bare star rating).

Reads businesses from a CSV file, pulls each one's newest reviews via
Apify's Google Maps Reviews Scraper, and writes out a CSV listing only the
businesses that have a 1 or 2-star review, with review text, posted within
the last DAYS_THRESHOLD days (see gmaps_checker.py). Businesses with no
matching review are skipped entirely - they will not appear in the output
at all.

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

(There's also dashboard.py, a web version of this same check with a
browser UI and run history.)
"""

import csv
import os
import sys

from gmaps_checker import OUTPUT_FIELDS, check_business

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

INPUT_CSV = "businesses.csv"
OUTPUT_CSV = "flagged_businesses.csv"


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

            result = check_business(APIFY_API_TOKEN, business_name, city, state, maps_url)

            if result["status"] == "blank":
                continue

            print(f"Checking {result['label']}...")

            if result["status"] == "skipped":
                print(f"  SKIPPING: {result['reason']}")
            elif result["status"] == "error":
                print(f"  Could not fetch reviews: {result['reason']}")
            elif result["status"] == "clean":
                print(f"  {result['reason']}")
            elif result["status"] == "flagged":
                row = result["row"]
                print(f"  FLAGGED: {row['Star Rating']} stars, {row['Review Age']}, by {row['Reviewer Name']}")
                print(f"  Matched place: {row['Matched Place URL']}")
                flagged_rows.append(row)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(flagged_rows)

    print(f"\nDone. {len(flagged_rows)} business(es) flagged. Results in {OUTPUT_CSV}.")


if __name__ == "__main__":
    main()
