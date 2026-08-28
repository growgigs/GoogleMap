"""
Shared logic for checking a business's recent Google Maps reviews via
Apify's Google Maps Reviews Scraper. Used by both check_businesses.py
(the command-line version) and dashboard.py (the web dashboard), so the
actual checking rules only exist in one place.
"""

import re
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

# Only report a business's single worst review if it's this bad or lower -
# if the lowest review a business has is 3+ stars, there's nothing worth
# flagging and the business is skipped entirely.
WORST_REVIEW_MAX_STARS = 2

WORST_REVIEW_OUTPUT_FIELDS = ["Business Name", "Website", "Reviewer Name", "Star Rating", "Matched Place URL"]

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


def fetch_lowest_review(api_token, start_url):
    """Get just the single lowest-starred review Google has ever recorded
    for this business - any age, no text required. Cheaper than
    fetch_reviews() since it asks Apify for exactly one review, sorted
    worst-first, instead of pulling a whole recency window."""
    endpoint = f"https://api.apify.com/v2/acts/{REVIEWS_ACTOR_ID}/run-sync-get-dataset-items"
    run_input = {
        "startUrls": [{"url": start_url}],
        "maxReviews": 1,
        "reviewsSort": "lowestRanking",
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


def check_business_worst_review(api_token, business_name="", city="", state="", maps_url="", website=""):
    """Like check_business(), but ignores recency and review text entirely -
    just gets the single worst review a business has ever received. Flags
    it only if that review is WORST_REVIEW_MAX_STARS or lower; if even the
    worst review is better than that, the business is "clean" (nothing
    worth reporting) and is skipped from the output, same as check_business()."""
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
        reviews = fetch_lowest_review(api_token, start_url)
    except requests.exceptions.RequestException as e:
        return {"status": "error", "label": label, "reason": describe_error(e)}

    if not reviews:
        return {"status": "clean", "label": label, "reason": "No reviews found at all."}

    worst = reviews[0]
    stars = worst.get("stars")
    if stars is None or stars > WORST_REVIEW_MAX_STARS:
        return {
            "status": "clean",
            "label": label,
            "reason": f"Lowest review is {stars} stars, above the {WORST_REVIEW_MAX_STARS}-star threshold.",
        }

    resolved_name = (business_name or "").strip() or worst.get("title", label)
    matched_place_url = worst.get("url") or start_url

    return {
        "status": "flagged",
        "label": label,
        "row": {
            "Business Name": resolved_name,
            "Website": website,
            "Reviewer Name": worst.get("name", ""),
            "Star Rating": stars,
            "Matched Place URL": matched_place_url,
        },
    }


def check_businesses_worst_review_parallel(api_token, businesses, max_workers=MAX_CONCURRENT_CHECKS):
    """Same calling convention as check_businesses_parallel(), but for
    check_business_worst_review() instead."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {}
        for i, business in enumerate(businesses):
            name, city, state, maps_url = business[:4]
            website = business[4] if len(business) > 4 else ""
            future = executor.submit(check_business_worst_review, api_token, name, city, state, maps_url, website)
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


# Major hotel/motel brand names to exclude when hunting for independent
# operators. This exists because our own location-count is only ever a
# count of what we found IN THIS SEARCH - a Hampton Inn that only shows up
# twice in our sample is still part of a chain with thousands of locations
# nationwide, not a genuine 2-location independent business. There's no
# way to know a business's true total location count from Google Maps
# data alone, so for franchise-heavy categories (hotels, motels), name-
# based exclusion is the only real way to filter them out. This is a
# best-effort list of the major US/Canada hotel brand families - it will
# not catch every franchise brand that exists, and could in rare cases
# exclude an independent business that coincidentally shares a word with
# a brand name (e.g. an independent "Value Inn").
HOTEL_CHAIN_KEYWORDS = {
    # Marriott family
    "marriott", "courtyard", "residence inn", "fairfield inn", "springhill suites",
    "towneplace suites", "renaissance hotel", "ritz-carlton", "ritz carlton", "jw marriott",
    "w hotel", "westin", "sheraton", "four points", "aloft", "element hotel",
    "autograph collection", "delta hotels", "gaylord", "st. regis", "st regis",
    "le meridien", "moxy", "ac hotel", "tribute portfolio", "protea hotel",
    "luxury collection", "four seasons",
    # Hyatt family
    "hyatt", "andaz", "alila", "thompson hotel", "destination by hyatt",
    "unbound collection",
    # Hilton family
    "hilton", "hampton inn", "hampton by hilton", "doubletree", "embassy suites",
    "homewood suites", "home2 suites", "hilton garden inn", "canopy by hilton",
    "curio collection", "tapestry collection", "tru by hilton", "waldorf astoria",
    "conrad hotel", "motto by hilton",
    # IHG family
    "holiday inn", "intercontinental", "crowne plaza", "candlewood suites",
    "staybridge suites", "kimpton", "even hotel", "hotel indigo", "avid hotel", "voco",
    # Wyndham family
    "wyndham", "days inn", "super 8", "ramada", "howard johnson", "travelodge",
    "baymont", "microtel", "la quinta", "wingate by wyndham", "hawthorn suites",
    "dolce hotel", "worldmark",
    # Choice Hotels family
    "comfort inn", "comfort suites", "quality inn", "sleep inn", "clarion",
    "mainstay suites", "suburban extended stay", "econo lodge", "rodeway inn",
    "cambria hotel", "woodspring suites", "everhome suites",
    # Other major chains
    "best western", "extended stay america", "motel 6", "studio 6", "red roof inn",
    "radisson", "country inn & suites", "country inn and suites", "park inn",
    "sonesta", "red lion hotel", "knights inn", "america's best value inn",
    "americas best value inn", "vagabond inn", "intown suites", "drury inn",
    "drury plaza", "candlewood", "great wolf lodge",
}


def _is_hotel_chain(name):
    name_lower = name.lower()
    return any(keyword in name_lower for keyword in HOTEL_CHAIN_KEYWORDS)


MULTI_LOCATION_OUTPUT_FIELDS = [
    "Business Name", "Website", "Total Rating", "Total Reviews",
    "Total Locations", "Google Profile Link",
]

# Common legal-entity suffixes and separator-led branch qualifiers
# (" - Brooklyn", " (Midtown)", " | Downtown") get stripped before grouping
# by name. This is a heuristic, not exact matching - inconsistently-branded
# locations can still be missed, and unrelated businesses with very similar
# generic names could occasionally get grouped together. Treat results as a
# strong starting list to verify, not a guaranteed-accurate count.
_NAME_SUFFIXES_TO_STRIP = {
    "llc", "llp", "pllc", "pc", "pa", "inc", "incorporated", "ltd", "co",
    "corp", "company", "group",
}


# Major metros per US state, used to expand a state-level search into
# several city searches - a bare state name/abbreviation passed straight
# to Google Maps does NOT actually search the whole state (verified live:
# "New York", "New York, USA", "New York State", and "NY, USA" all came
# back clustered in one single area each, and a different area depending
# on the exact phrasing - not real statewide coverage). This list is what
# makes state-level search actually work.
US_STATE_CITIES = {
    "Alabama": ["Birmingham, AL", "Montgomery, AL", "Huntsville, AL", "Mobile, AL", "Tuscaloosa, AL"],
    "Alaska": ["Anchorage, AK", "Fairbanks, AK", "Juneau, AK"],
    "Arizona": ["Phoenix, AZ", "Tucson, AZ", "Mesa, AZ", "Scottsdale, AZ", "Chandler, AZ"],
    "Arkansas": ["Little Rock, AR", "Fayetteville, AR", "Fort Smith, AR"],
    "California": ["Los Angeles, CA", "San Diego, CA", "San Jose, CA", "San Francisco, CA", "Fresno, CA", "Sacramento, CA", "Oakland, CA"],
    "Colorado": ["Denver, CO", "Colorado Springs, CO", "Aurora, CO", "Fort Collins, CO"],
    "Connecticut": ["Bridgeport, CT", "New Haven, CT", "Hartford, CT", "Stamford, CT"],
    "Delaware": ["Wilmington, DE", "Dover, DE"],
    "Florida": ["Jacksonville, FL", "Miami, FL", "Tampa, FL", "Orlando, FL", "St. Petersburg, FL", "Fort Lauderdale, FL"],
    "Georgia": ["Atlanta, GA", "Augusta, GA", "Columbus, GA", "Savannah, GA"],
    "Hawaii": ["Honolulu, HI", "Hilo, HI"],
    "Idaho": ["Boise, ID", "Meridian, ID", "Nampa, ID"],
    "Illinois": ["Chicago, IL", "Aurora, IL", "Rockford, IL", "Naperville, IL"],
    "Indiana": ["Indianapolis, IN", "Fort Wayne, IN", "Evansville, IN"],
    "Iowa": ["Des Moines, IA", "Cedar Rapids, IA", "Davenport, IA"],
    "Kansas": ["Wichita, KS", "Overland Park, KS", "Kansas City, KS"],
    "Kentucky": ["Louisville, KY", "Lexington, KY", "Bowling Green, KY"],
    "Louisiana": ["New Orleans, LA", "Baton Rouge, LA", "Shreveport, LA"],
    "Maine": ["Portland, ME", "Lewiston, ME", "Bangor, ME"],
    "Maryland": ["Baltimore, MD", "Frederick, MD", "Rockville, MD"],
    "Massachusetts": ["Boston, MA", "Worcester, MA", "Springfield, MA", "Cambridge, MA"],
    "Michigan": ["Detroit, MI", "Grand Rapids, MI", "Ann Arbor, MI", "Lansing, MI"],
    "Minnesota": ["Minneapolis, MN", "St. Paul, MN", "Rochester, MN"],
    "Mississippi": ["Jackson, MS", "Gulfport, MS"],
    "Missouri": ["Kansas City, MO", "St. Louis, MO", "Springfield, MO"],
    "Montana": ["Billings, MT", "Missoula, MT"],
    "Nebraska": ["Omaha, NE", "Lincoln, NE"],
    "Nevada": ["Las Vegas, NV", "Reno, NV", "Henderson, NV"],
    "New Hampshire": ["Manchester, NH", "Nashua, NH"],
    "New Jersey": ["Newark, NJ", "Jersey City, NJ", "Trenton, NJ", "Edison, NJ"],
    "New Mexico": ["Albuquerque, NM", "Las Cruces, NM", "Santa Fe, NM"],
    "New York": ["New York, NY", "Buffalo, NY", "Rochester, NY", "Albany, NY", "Syracuse, NY", "Yonkers, NY"],
    "North Carolina": ["Charlotte, NC", "Raleigh, NC", "Greensboro, NC", "Durham, NC"],
    "North Dakota": ["Fargo, ND", "Bismarck, ND"],
    "Ohio": ["Columbus, OH", "Cleveland, OH", "Cincinnati, OH", "Toledo, OH"],
    "Oklahoma": ["Oklahoma City, OK", "Tulsa, OK", "Norman, OK"],
    "Oregon": ["Portland, OR", "Eugene, OR", "Salem, OR"],
    "Pennsylvania": ["Philadelphia, PA", "Pittsburgh, PA", "Allentown, PA", "Erie, PA"],
    "Rhode Island": ["Providence, RI", "Warwick, RI"],
    "South Carolina": ["Charleston, SC", "Columbia, SC", "Greenville, SC"],
    "South Dakota": ["Sioux Falls, SD", "Rapid City, SD"],
    "Tennessee": ["Nashville, TN", "Memphis, TN", "Knoxville, TN", "Chattanooga, TN"],
    "Texas": ["Houston, TX", "San Antonio, TX", "Dallas, TX", "Austin, TX", "Fort Worth, TX", "El Paso, TX"],
    "Utah": ["Salt Lake City, UT", "West Valley City, UT", "Provo, UT"],
    "Vermont": ["Burlington, VT", "South Burlington, VT"],
    "Virginia": ["Virginia Beach, VA", "Norfolk, VA", "Richmond, VA", "Arlington, VA"],
    "Washington": ["Seattle, WA", "Spokane, WA", "Tacoma, WA", "Vancouver, WA"],
    "West Virginia": ["Charleston, WV", "Huntington, WV"],
    "Wisconsin": ["Milwaukee, WI", "Madison, WI", "Green Bay, WI"],
    "Wyoming": ["Cheyenne, WY", "Casper, WY"],
    "District of Columbia": ["Washington, DC"],
}

# Same lookup, keyed by two-letter abbreviation, so "NY" and "New York"
# both work.
_STATE_ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}


CANADA_PROVINCE_CITIES = {
    "Alberta": ["Calgary, AB", "Edmonton, AB", "Red Deer, AB"],
    "British Columbia": ["Vancouver, BC", "Surrey, BC", "Burnaby, BC", "Victoria, BC"],
    "Manitoba": ["Winnipeg, MB", "Brandon, MB"],
    "New Brunswick": ["Saint John, NB", "Moncton, NB", "Fredericton, NB"],
    "Newfoundland and Labrador": ["St. John's, NL"],
    "Northwest Territories": ["Yellowknife, NT"],
    "Nova Scotia": ["Halifax, NS", "Sydney, NS"],
    "Nunavut": ["Iqaluit, NU"],
    "Ontario": ["Toronto, ON", "Ottawa, ON", "Mississauga, ON", "Hamilton, ON", "London, ON"],
    "Prince Edward Island": ["Charlottetown, PE"],
    "Quebec": ["Montreal, QC", "Quebec City, QC", "Laval, QC", "Gatineau, QC"],
    "Saskatchewan": ["Saskatoon, SK", "Regina, SK"],
    "Yukon": ["Whitehorse, YT"],
}

US_RESORT_DESTINATIONS = [
    # Northeast
    "Hyannis, MA", "Newport, RI", "Lake George, NY", "Niagara Falls, NY",
    "Stroudsburg, PA", "Bar Harbor, ME", "Kennebunkport, ME", "Edgartown, MA",
    "Nantucket, MA", "Lake Placid, NY", "Saratoga Springs, NY", "North Conway, NH",
    "Stowe, VT",
    # Southeast
    "Key West, FL", "Naples, FL", "Destin, FL", "Panama City Beach, FL",
    "Sarasota, FL", "St. Augustine, FL", "Amelia Island, FL", "Clearwater Beach, FL",
    "Hilton Head Island, SC", "Myrtle Beach, SC", "Charleston, SC", "Savannah, GA",
    "Gatlinburg, TN", "Pigeon Forge, TN", "Asheville, NC", "Nags Head, NC",
    "Virginia Beach, VA", "Williamsburg, VA",
    # Midwest
    "Wisconsin Dells, WI", "Mackinac Island, MI", "Traverse City, MI",
    "Sturgeon Bay, WI", "Lake Geneva, WI", "Branson, MO", "Put-in-Bay, OH",
    # Mountain West / Southwest
    "Aspen, CO", "Vail, CO", "Breckenridge, CO", "Park City, UT", "Jackson, WY",
    "Sedona, AZ", "Scottsdale, AZ", "Santa Fe, NM", "Taos, NM", "Moab, UT",
    "South Lake Tahoe, CA",
    # West Coast
    "Napa, CA", "Sonoma, CA", "Monterey, CA", "Carmel-By-The-Sea, CA",
    "Santa Barbara, CA", "Newport Beach, CA", "Palm Springs, CA", "Cannon Beach, OR",
    "Bend, OR", "Leavenworth, WA",
    # Gulf / Texas
    "South Padre Island, TX", "Galveston, TX",
    # Hawaii
    "Lahaina, HI", "Lihue, HI",
]


_PROVINCE_ABBREVIATIONS = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NT": "Northwest Territories", "NS": "Nova Scotia",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}


def cities_for_state(state):
    """Look up the major-city list for a US state or Canadian province/
    territory, given either its full name or two-letter abbreviation
    (case-insensitive). Returns None if not recognized."""
    state = (state or "").strip()
    full_name = _STATE_ABBREVIATIONS.get(state.upper()) or _PROVINCE_ABBREVIATIONS.get(state.upper()) or state
    for region in (US_STATE_CITIES, CANADA_PROVINCE_CITIES):
        for name, cities in region.items():
            if name.lower() == full_name.lower():
                return cities
    return None


def _normalize_business_name_for_grouping(name):
    import re

    name = name.lower()
    # Cut off anything after a dash/pipe/parenthesis - almost always a
    # per-branch qualifier ("Business Name - Brooklyn", "(Midtown)").
    name = re.split(r"[-|(]", name)[0]
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    words = [w for w in name.split() if w not in _NAME_SUFFIXES_TO_STRIP]
    return " ".join(words).strip()


def _search_raw_places(api_token, keyword, location, max_places):
    """One category+location scrape, returning the raw place dicts exactly
    as Apify's Places Scraper gives them back."""
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
    return response.json()


def _search_raw_places_resilient(api_token, keyword, location, max_places, max_attempts=3):
    """Same as _search_raw_places(), but retries transient failures (e.g.
    Apify's occasional 502s under sustained load) with backoff instead of
    letting one bad request take down an entire multi-city batch. Returns
    (location, places, error) - error is None on success, or the last
    exception if every attempt failed (in which case places is [])."""
    import time as _time

    last_error = None
    for attempt in range(max_attempts):
        try:
            return location, _search_raw_places(api_token, keyword, location, max_places), None
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_attempts - 1:
                _time.sleep(2 ** attempt * 2)  # 2s, 4s, ...
    return location, [], last_error


def _price_high_end(price_str):
    """Parse Google's place price string (e.g. "$100+", "$20-30", "$10")
    into its upper-bound dollar estimate, or None if there's no price data
    at all. Google's search keyword text (e.g. "fine dining restaurant")
    does NOT actually filter by price tier - verified live, a nationwide
    "fine dining restaurant" search came back full of Red Lobster, 7-Eleven,
    and Cracker Barrel. This is the only reliable way to filter restaurant-
    type categories by how expensive they actually are."""
    if not price_str:
        return None
    s = price_str.replace("–", "-").replace(",", "")
    if "+" in s:
        match = re.search(r"(\d+)", s)
        return int(match.group(1)) if match else None
    nums = re.findall(r"(\d+)", s)
    if not nums:
        return None
    return int(nums[-1])


def _meets_min_price(place, min_price):
    """True if this place's price data indicates an average cost at or
    above min_price. Places with no price listed on Google at all are
    excluded, not assumed to pass - there's no way to verify them."""
    high_end = _price_high_end(place.get("price"))
    return high_end is not None and high_end >= min_price


def find_multi_location_businesses(api_token, keyword, locations, max_places_per_location, min_locations=2, min_reviews_per_location=0, max_locations=None, exclude_hotel_chains=False, min_price=None, max_concurrent_searches=8):
    """Find businesses that show up as 2+ (or min_locations+) separate
    branch listings, grouped by a normalized version of their name. Google
    Maps has no "number of locations" field - this works by scraping every
    matching place for a category and grouping ones that look like the
    same business. Returns one row per qualifying business
    (MULTI_LOCATION_OUTPUT_FIELDS) - website/rating/reviews/profile link
    are all taken from that business's "main" location (the branch with
    the most reviews, as a proxy for its flagship/most-established one).

    `locations` can be:
      - a single "City, State" string
      - a US state name or two-letter abbreviation (e.g. "New York" or
        "NY") - automatically expands to that state's major metros (see
        US_STATE_CITIES), since a bare state name passed straight to
        Google Maps does NOT actually search the whole state (verified
        live - it just centers on one single, inconsistent interpretation
        of the text)
      - a list of "City, State" strings, for full control over exactly
        which metros to search
      - "USA" / "United States" / "Canada" - every state/province in that
        country
    Results from every location searched are merged into one pool before
    grouping, so a chain's branches get counted together even if no
    single search saw more than one or two of them.

    City searches run concurrently (max_concurrent_searches at a time)
    rather than one at a time - a nationwide sweep sequentially would take
    hours.

    exclude_hotel_chains=True drops any place matching a known major hotel/
    motel brand (see HOTEL_CHAIN_KEYWORDS) before grouping - our own
    location count only reflects what THIS search found, so a brand with 2
    locations in our sample could still be part of a 500-location national
    chain; this is the only real way to filter those out for a franchise-
    heavy category like hotels. It's a best-effort name list, not
    guaranteed complete or free of false positives.

    min_price filters out places whose Google-listed average price (e.g.
    "$100+", "$20-30") comes in below this dollar amount, and drops places
    with no price data at all (unverifiable, not assumed to pass). This
    matters for restaurant-type searches specifically - verified live, a
    keyword like "fine dining restaurant" does NOT actually filter Google's
    results by price tier, it just full-text-matches "restaurant"; a
    nationwide run came back full of Red Lobster, 7-Eleven, and Cracker
    Barrel. min_price is the only reliable way to restrict to an actual
    price tier."""
    if isinstance(locations, str):
        if locations.strip().lower() in ("usa", "united states", "us"):
            locations = [city for cities in US_STATE_CITIES.values() for city in cities]
        elif locations.strip().lower() == "canada":
            locations = [city for cities in CANADA_PROVINCE_CITIES.values() for city in cities]
        else:
            state_cities = cities_for_state(locations)
            locations = state_cities if state_cities else [locations]

    places = []
    failed_locations = []
    with ThreadPoolExecutor(max_workers=max_concurrent_searches) as executor:
        futures = [
            executor.submit(_search_raw_places_resilient, api_token, keyword, location, max_places_per_location)
            for location in locations
        ]
        for future in as_completed(futures):
            location, location_places, error = future.result()
            if error is not None:
                failed_locations.append((location, describe_error(error)))
            places.extend(location_places)

    if failed_locations:
        print(f"WARNING: {len(failed_locations)}/{len(locations)} location(s) failed after retries and were skipped:")
        for location, reason in failed_locations:
            print(f"  - {location}: {reason}")

    # Nearby searched cities can both return the same physical place (e.g.
    # Tampa and St. Petersburg are close enough that a hotel near the
    # border could appear in both) - dedupe by Google's own place ID so
    # the same real location never gets counted twice.
    seen_place_ids = set()
    deduped_places = []
    for place in places:
        place_id = place.get("placeId")
        if place_id and place_id in seen_place_ids:
            continue
        if place_id:
            seen_place_ids.add(place_id)
        deduped_places.append(place)
    places = deduped_places

    groups = {}
    for place in places:
        if not place.get("url"):
            continue
        title = place.get("title", "")
        if exclude_hotel_chains and _is_hotel_chain(title):
            continue
        reviews_count = place.get("reviewsCount") or 0
        if reviews_count < min_reviews_per_location:
            continue
        if min_price is not None and not _meets_min_price(place, min_price):
            continue
        key = _normalize_business_name_for_grouping(title)
        if not key:
            continue
        groups.setdefault(key, []).append(place)

    rows = []
    for branches in groups.values():
        if len(branches) < min_locations:
            continue
        if max_locations is not None and len(branches) > max_locations:
            continue
        main = max(branches, key=lambda p: p.get("reviewsCount") or 0)
        rows.append({
            "Business Name": main.get("title", ""),
            "Website": main.get("website", "") or "",
            "Total Rating": main.get("totalScore", ""),
            "Total Reviews": main.get("reviewsCount", ""),
            "Total Locations": len(branches),
            "Google Profile Link": main.get("url", ""),
        })
    rows.sort(key=lambda r: (-r["Total Locations"], r["Business Name"]))
    return rows


def estimate_multi_location_search_cost(num_places):
    """Cost for scraping num_places raw places to search for multi-location
    businesses - no filter add-on used (see find_multi_location_businesses),
    so this is just the base place-scraped price."""
    return PLACES_ACTOR_START_PRICE + PLACE_SCRAPED_PRICE * num_places


# --- Cost estimation, verified against both actors' live Apify pricing ---
#
# Both actors run on Apify's pay-per-event pricing (re-verified 2026-08-09 -
# these actors have switched pricing models before, so re-check against a
# real run's usageTotalUsd/pricingInfo if these ever look off):
#   compass/crawler-google-places: $0.003 per place scraped, plus a small
#     one-time per-run "actor start" fee (~$0.00005/GB memory, negligible
#     at any real volume).
#   compass/google-maps-reviews-scraper: $0.00045 per review scraped, plus
#     the same negligible one-time per-run start fee.
PLACES_ACTOR_START_PRICE = 0.0002
PLACE_SCRAPED_PRICE = 0.003
REVIEWS_ACTOR_START_PRICE = 0.00005
REVIEW_SCRAPED_PRICE = 0.00045

# Apify's maxReviews input isn't a hard cap in practice - live testing
# (2026-08-09) saw it fetch a few reviews past the requested max on some
# businesses (asked for 1, got up to 5; asked for 20, got 40). This is an
# empirically-observed ceiling for the worst-review check (one review
# requested per business), not a documented guarantee from Apify - used
# here purely to keep the worst-case cost estimate honest.
WORST_REVIEW_MAX_EVENTS_PER_BUSINESS = 5

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


def estimate_worst_review_cost(num_businesses):
    """Return (typical_total_usd, worst_case_total_usd) for checking
    num_businesses with check_business_worst_review() - one requested
    review per business instead of a whole recency window, so this is far
    cheaper than estimate_search_cost()'s review-checking side. "Typical"
    assumes 1 review actually gets charged per business (what most of a
    small live sample showed); "worst case" assumes
    WORST_REVIEW_MAX_EVENTS_PER_BUSINESS every time."""
    typical_total = num_businesses * (REVIEWS_ACTOR_START_PRICE + REVIEW_SCRAPED_PRICE * 1)
    worst_case_total = num_businesses * (REVIEWS_ACTOR_START_PRICE + REVIEW_SCRAPED_PRICE * WORST_REVIEW_MAX_EVENTS_PER_BUSINESS)
    return typical_total, worst_case_total


def check_worst_review_cost_cap(num_businesses):
    """Same shape as check_cost_cap(), but for the worst-review check."""
    typical_total, worst_case_total = estimate_worst_review_cost(num_businesses)
    return {
        "num_businesses": num_businesses,
        "typical_total": typical_total,
        "worst_case_total": worst_case_total,
        "typical_per_1000": cost_per_1000(typical_total, num_businesses),
        "worst_case_per_1000": cost_per_1000(worst_case_total, num_businesses),
        "allowed": cost_per_1000(worst_case_total, num_businesses) <= HARD_COST_CAP_PER_1000,
    }
