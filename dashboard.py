"""
Google Maps review-check dashboard.

A password-protected web dashboard (built with Streamlit) with two ways to
find businesses to check:

  - Search specific business list: paste or upload businesses you already
    know (name+city+state, or a direct Maps URL).
  - Search by category + location: give it a keyword (e.g. "Italian
    restaurant") and a place (e.g. "Miami, FL") and it finds matching
    businesses itself via Apify's Google Maps Places Scraper.

Either way, each business's recent reviews are checked via Apify's Google
Maps Reviews Scraper, and only businesses with a 1-2 star review (with
written text) posted within the recency window are shown/downloadable (see
gmaps_checker.py for the actual rules). A history of past runs is kept in
a local SQLite file (history.db).

Category searches show a cost estimate (typical and guaranteed worst-case,
based on Apify's real per-event pricing) before you can run them, and are
hard-blocked from running if the worst-case cost would exceed
gmaps_checker.HARD_COST_CAP_PER_1000 per 1,000 businesses.

One-time setup, in Streamlit Community Cloud's app settings -> Secrets:
  APIFY_API_TOKEN = "your_apify_token"
  DASHBOARD_PASSWORD = "choose_a_password"

To run it on your own computer instead: copy
.streamlit/secrets.toml.example to .streamlit/secrets.toml, fill in the
two values above (that file is gitignored - never commit your real
secrets), then run:
  streamlit run dashboard.py
"""

import csv
import io
import sqlite3
from datetime import datetime, timezone

import requests
import streamlit as st

from gmaps_checker import (
    CANADA_PROVINCE_CITIES,
    DAYS_THRESHOLD,
    HARD_COST_CAP_PER_1000,
    MULTI_LOCATION_OUTPUT_FIELDS,
    OUTPUT_FIELDS,
    US_RESORT_DESTINATIONS,
    US_STATE_CITIES,
    WORST_REVIEW_MAX_STARS,
    WORST_REVIEW_OUTPUT_FIELDS,
    check_businesses_parallel,
    check_businesses_worst_review_parallel,
    check_cost_cap,
    check_worst_review_cost_cap,
    cities_for_state,
    describe_error,
    estimate_multi_location_search_cost,
    find_multi_location_businesses,
    normalize_business_row,
    search_places,
)

DB_PATH = "history.db"

st.set_page_config(page_title="Google Maps Review Checker", page_icon="⭐")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            businesses_checked INTEGER NOT NULL,
            businesses_flagged INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flagged_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            business_name TEXT,
            reviewer_name TEXT,
            star_rating TEXT,
            review_age TEXT,
            matched_place_url TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
        """
    )
    try:
        conn.execute("ALTER TABLE flagged_reviews ADD COLUMN website TEXT")
    except sqlite3.OperationalError:
        pass  # already has the column - a history.db from before Website existed
    return conn


def save_run(conn, flagged_rows, businesses_checked):
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO runs (run_at, businesses_checked, businesses_flagged) VALUES (?, ?, ?)",
        (run_at, businesses_checked, len(flagged_rows)),
    )
    run_id = cur.lastrowid
    for row in flagged_rows:
        conn.execute(
            "INSERT INTO flagged_reviews "
            "(run_id, business_name, website, reviewer_name, star_rating, review_age, matched_place_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                row["Business Name"],
                row.get("Website", ""),
                row["Reviewer Name"],
                row["Star Rating"],
                row.get("Review Age", ""),
                row["Matched Place URL"],
            ),
        )
    conn.commit()
    return run_id


def rows_to_csv(rows, fields=OUTPUT_FIELDS):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def parse_pasted_lines(text):
    """Turn pasted lines into (business_name, city, state, maps_url) tuples.
    Each line is either a URL, or 'Business Name, City, State'."""
    businesses = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            businesses.append(("", "", "", line))
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            businesses.append((parts[0], parts[1], parts[2], ""))
        else:
            # Not enough info - check_business will report exactly why.
            businesses.append((line, "", "", ""))
    return businesses


def check_password():
    """Simple password gate. Returns True once the correct password has
    been entered for this browser session."""
    if st.session_state.get("password_ok"):
        return True

    st.title("Google Maps Review Checker")
    password = st.text_input("Password", type="password")
    if st.button("Enter"):
        if password and password == st.secrets.get("DASHBOARD_PASSWORD"):
            st.session_state["password_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    return False


def run_checks(api_token, businesses, days_threshold, check_mode):
    """Check every business in `businesses`, showing live progress, then
    save the results to history and store them in session_state for
    display below. check_mode is "recent" (recent bad review, with text)
    or "worst" (single worst review ever, any age, no text required)."""
    progress_area = st.empty()
    log_area = st.container()
    results_by_index = {}
    checked_count = 0
    total = len(businesses)

    if check_mode == "worst":
        checker = check_businesses_worst_review_parallel(api_token, businesses)
        output_fields = WORST_REVIEW_OUTPUT_FIELDS
    else:
        checker = check_businesses_parallel(api_token, businesses, days_threshold=days_threshold)
        output_fields = OUTPUT_FIELDS

    for index, result in checker:
        results_by_index[index] = result
        if result["status"] == "blank":
            continue
        checked_count += 1
        progress_area.write(f"Checked {checked_count}/{total}...")

        if result["status"] == "flagged":
            row = result["row"]
            if check_mode == "worst":
                log_area.write(
                    f"\U0001f6a9 **{row['Business Name']}** - {row['Star Rating']} stars, by {row['Reviewer Name']}"
                )
            else:
                log_area.write(
                    f"\U0001f6a9 **{row['Business Name']}** - {row['Star Rating']} stars, "
                    f"{row['Review Age']}, by {row['Reviewer Name']}"
                )
        elif result["status"] == "error":
            log_area.write(f"⚠️ {result['label']}: {result['reason']}")
        elif result["status"] == "skipped":
            log_area.write(f"⏭️ {result['label']}: {result['reason']}")
        # "clean" businesses produce no extra log line - the progress line above is enough.

    progress_area.write(f"Done checking {checked_count} business(es).")

    # Put flagged results back in the original input order.
    flagged_rows = [
        results_by_index[i]["row"]
        for i in sorted(results_by_index)
        if results_by_index[i]["status"] == "flagged"
    ]

    conn = get_db()
    save_run(conn, flagged_rows, checked_count)
    conn.close()

    st.session_state["last_run_rows"] = flagged_rows
    st.session_state["last_run_fields"] = output_fields


def main():
    if not check_password():
        return

    api_token = st.secrets.get("APIFY_API_TOKEN")
    if not api_token:
        st.error("APIFY_API_TOKEN isn't set in this app's Secrets. Add it in Streamlit Cloud's app settings.")
        return

    st.title("Google Maps Review Checker")

    tab_check, tab_multi, tab_history = st.tabs(["Check businesses", "Multi-location finder", "History"])

    with tab_check:
        check_mode_label = st.radio(
            "Check mode",
            [
                "Recent bad review (recency window, must have written text)",
                f"Worst review ever (single lowest-rated review per business, {WORST_REVIEW_MAX_STARS} stars or below, any age, no text required)",
            ],
        )
        check_mode = "worst" if check_mode_label.startswith("Worst") else "recent"

        if check_mode == "worst":
            st.caption(
                f"Flags businesses whose single worst-ever review is {WORST_REVIEW_MAX_STARS} stars or "
                "lower - reviewer name and star rating only, no date filter, no text required."
            )
        else:
            st.caption(
                "Flags businesses with a 1-2 star review (with written text) posted within your "
                f"chosen recency window (default {DAYS_THRESHOLD} days)."
            )

        mode = st.radio(
            "Search mode",
            ["Search specific business list", "Search by category + location"],
            horizontal=True,
        )

        businesses = []
        days_threshold = DAYS_THRESHOLD
        keyword = location = ""
        max_places = 0
        cost_info = None
        run_label = "Run check"

        if mode == "Search specific business list":
            st.subheader("1. Give it your businesses")
            st.markdown(
                "One per line: either a full Google Maps URL, or `Business Name, City, State` "
                "(name alone risks matching the wrong branch of a chain)."
            )
            pasted = st.text_area(
                "Paste businesses here",
                height=150,
                placeholder="Toyota of Cedar Park, Cedar Park, TX\nhttps://www.google.com/maps/place/...",
            )
            uploaded = st.file_uploader(
                "...or upload a CSV (columns: business_name, city, state, maps_url - "
                "common variations like 'Business Name' or 'Google Maps URL' are fine too)",
                type="csv",
            )

            if uploaded is not None:
                text = uploaded.getvalue().decode("utf-8")
                reader = csv.DictReader(io.StringIO(text))
                for row in reader:
                    businesses.append(normalize_business_row(row))
                if not any(any(field.strip() for field in b) for b in businesses):
                    st.warning(
                        "Couldn't find a business name or Maps URL in any row of this CSV. "
                        f"Columns found: {', '.join(reader.fieldnames or [])}. "
                        "Rename your columns to business_name/city/state/maps_url (or something close) and re-upload."
                    )
            elif pasted.strip():
                businesses = parse_pasted_lines(pasted)

            ready_to_run = bool(businesses)

        else:
            st.subheader("1. Search by category + location")
            col1, col2 = st.columns(2)
            with col1:
                keyword = st.text_input("Search keyword", placeholder="Italian restaurant")
            with col2:
                location = st.text_input("Location", placeholder="Miami, FL")
            max_places = st.number_input(
                "Max businesses to search", min_value=1, max_value=5000, value=20, step=1
            )
            if check_mode == "recent":
                days_threshold = st.number_input(
                    "Review recency window (days)", min_value=1, max_value=365, value=DAYS_THRESHOLD, step=1
                )

            cost_info = (
                check_worst_review_cost_cap(int(max_places))
                if check_mode == "worst"
                else check_cost_cap(int(max_places))
            )
            st.caption(
                f"Estimated businesses: ~{int(max_places)}  ·  "
                f"Estimated cost: \\${cost_info['typical_total']:.2f} typical "
                f"(\\${cost_info['typical_per_1000']:.2f} per 1,000)  ·  "
                f"up to \\${cost_info['worst_case_total']:.2f} worst case "
                f"(\\${cost_info['worst_case_per_1000']:.2f} per 1,000)"
            )
            if not cost_info["allowed"]:
                st.error(
                    f"Blocked: worst-case cost (\\${cost_info['worst_case_per_1000']:.2f} per 1,000) would "
                    f"exceed the \\${HARD_COST_CAP_PER_1000:.0f}/1,000 safety cap. Lower the business count to continue."
                )

            run_label = "Search & check"
            ready_to_run = bool(keyword.strip() and location.strip()) and cost_info["allowed"]

        if st.button(run_label, type="primary", disabled=not ready_to_run):
            if mode == "Search by category + location":
                with st.spinner(f"Searching for '{keyword}' in {location}..."):
                    try:
                        businesses = search_places(api_token, keyword.strip(), location.strip(), int(max_places))
                    except requests.exceptions.RequestException as e:
                        st.error(f"Search failed: {describe_error(e)}")
                        businesses = []
                st.write(f"Found {len(businesses)} business(es).")

            if businesses:
                run_checks(api_token, businesses, int(days_threshold), check_mode)
            elif mode == "Search by category + location":
                st.warning("No businesses found for that search - try a different keyword or location.")

        if st.session_state.get("last_run_rows") is not None:
            rows = st.session_state["last_run_rows"]
            fields = st.session_state.get("last_run_fields", OUTPUT_FIELDS)
            st.subheader("2. Results")
            if rows:
                st.dataframe(rows, width="stretch")
                st.download_button(
                    "Download CSV",
                    rows_to_csv(rows, fields),
                    file_name="flagged_businesses.csv",
                    mime="text/csv",
                )
            else:
                st.info("No businesses flagged in this run.")

    with tab_multi:
        st.subheader("Find businesses with multiple locations")
        st.caption(
            "Google Maps has no \"number of locations\" field - this works by scraping every "
            "matching place for a category across the location(s) below and grouping ones that "
            "look like the same business by name. It's a heuristic, not exact matching: "
            "inconsistently-branded locations can be missed, and unrelated businesses with very "
            "similar generic names could occasionally get grouped together. Treat results as a "
            "strong starting list to verify, not a guaranteed-accurate count."
        )

        ml_keyword = st.text_input("Search keyword", placeholder="Immigration attorney", key="ml_keyword")

        ml_country = st.radio("Country", ["United States", "Canada"], horizontal=True, key="ml_country")
        region_map = CANADA_PROVINCE_CITIES if ml_country == "Canada" else US_STATE_CITIES
        ml_nationwide = st.checkbox(f"Search the entire country (all {len(region_map)} states/provinces)", key="ml_nationwide")

        if ml_nationwide:
            ml_state_cities = [city for cities in region_map.values() for city in cities]
            st.caption(f"Will search all {len(ml_state_cities)} major metros across {ml_country}.")
        else:
            if ml_country == "Canada":
                ml_state = st.selectbox("Province/Territory", sorted(CANADA_PROVINCE_CITIES.keys()), key="ml_province")
            else:
                ml_state = st.selectbox("State", sorted(US_STATE_CITIES.keys()), key="ml_state")
            ml_state_cities = cities_for_state(ml_state) or []
            st.caption(f"Will search {len(ml_state_cities)} major metros in {ml_state}: {', '.join(ml_state_cities)}")

        ml_exclude_chains = st.checkbox(
            "Exclude major hotel/motel chains (Hilton, Marriott, IHG, Wyndham, Choice, and other franchise "
            "brand families) - our own location count only reflects what this search found, so a 2-location "
            "match could still be part of a much larger national chain. Best-effort name list, not guaranteed complete.",
            key="ml_exclude_chains",
        )
        ml_include_resorts = False
        if ml_country == "United States":
            ml_include_resorts = st.checkbox(
                f"Also search {len(US_RESORT_DESTINATIONS)} major US tourist/resort destinations (Key West, "
                "Aspen, Napa, etc.) - useful for hotel/resort-style categories, which cluster in leisure "
                "destinations rather than the biggest population metros above.",
                key="ml_include_resorts",
            )
            if ml_include_resorts:
                ml_state_cities = list(dict.fromkeys(ml_state_cities + US_RESORT_DESTINATIONS))

        with st.expander("Advanced: search a custom city list instead of a whole state"):
            ml_locations_text = st.text_area(
                "City, State - one per line. If filled in, this replaces the state selection above.",
                height=100,
                placeholder="New York, NY\nBuffalo, NY\nRochester, NY",
                key="ml_locations_text",
            )
        ml_custom_locations = [line.strip() for line in ml_locations_text.splitlines() if line.strip()]
        ml_locations = ml_custom_locations if ml_custom_locations else ml_state_cities

        col3, col4, col5, col6 = st.columns(4)
        with col3:
            ml_max_places = st.number_input(
                "Max places per city", min_value=1, max_value=5000, value=100, step=1, key="ml_max_places"
            )
        with col4:
            ml_min_locations = st.number_input(
                "Minimum locations", min_value=2, max_value=50, value=2, step=1, key="ml_min_locations"
            )
        with col5:
            ml_max_locations = st.number_input(
                "Maximum locations (0 = no cap)", min_value=0, max_value=10000, value=0, step=1, key="ml_max_locations"
            )
        with col6:
            ml_min_reviews = st.number_input(
                "Minimum reviews per location", min_value=0, max_value=10000, value=5, step=1, key="ml_min_reviews"
            )

        ml_total_places = int(ml_max_places) * len(ml_locations)
        ml_cost = estimate_multi_location_search_cost(ml_total_places)
        ml_cost_per_1000 = ml_cost / ml_total_places * 1000 if ml_total_places else 0
        st.caption(
            f"{len(ml_locations)} location(s) × {int(ml_max_places)} places each = "
            f"{ml_total_places} places scraped. Estimated cost: \\${ml_cost:.2f} "
            f"(\\${ml_cost_per_1000:.2f} per 1,000) - no per-review cost, so this is a fixed, "
            "not a worst-case, number."
        )

        if st.button("Search for multi-location businesses", type="primary", disabled=not (ml_keyword.strip() and ml_locations)):
            with st.spinner(f"Searching for '{ml_keyword}' across {len(ml_locations)} location(s)..."):
                try:
                    ml_rows = find_multi_location_businesses(
                        api_token,
                        ml_keyword.strip(),
                        ml_locations,
                        int(ml_max_places),
                        min_locations=int(ml_min_locations),
                        min_reviews_per_location=int(ml_min_reviews),
                        max_locations=int(ml_max_locations) if ml_max_locations else None,
                        exclude_hotel_chains=ml_exclude_chains,
                    )
                except requests.exceptions.RequestException as e:
                    st.error(f"Search failed: {describe_error(e)}")
                    ml_rows = []
            st.session_state["last_ml_rows"] = ml_rows

        if st.session_state.get("last_ml_rows") is not None:
            ml_rows = st.session_state["last_ml_rows"]
            st.subheader("Results")
            if ml_rows:
                st.write(f"Found {len(ml_rows)} business(es) with {int(ml_min_locations)}+ locations.")
                st.dataframe(ml_rows, width="stretch")
                st.download_button(
                    "Download CSV",
                    rows_to_csv(ml_rows, MULTI_LOCATION_OUTPUT_FIELDS),
                    file_name="multi_location_businesses.csv",
                    mime="text/csv",
                )
            else:
                st.info("No businesses found with that many locations - try a larger search, a lower minimum, or a different keyword/location.")

    with tab_history:
        st.subheader("Past runs")
        st.caption(
            "Note: this history lives in a local file on the server this dashboard runs on. "
            "On free hosting it can be reset if the app restarts or gets redeployed - "
            "download anything you want to keep."
        )
        conn = get_db()
        past_runs = conn.execute(
            "SELECT id, run_at, businesses_checked, businesses_flagged FROM runs ORDER BY run_at DESC"
        ).fetchall()

        if not past_runs:
            st.info("No past runs yet.")
        else:
            for run_id, run_at, checked, flagged_count in past_runs:
                with st.expander(f"{run_at} — {checked} checked, {flagged_count} flagged"):
                    db_rows = conn.execute(
                        "SELECT business_name, website, reviewer_name, star_rating, review_age, matched_place_url "
                        "FROM flagged_reviews WHERE run_id = ?",
                        (run_id,),
                    ).fetchall()
                    if db_rows:
                        table = [
                            {
                                "Business Name": r[0],
                                "Website": r[1] or "",
                                "Reviewer Name": r[2],
                                "Star Rating": r[3],
                                "Review Age": r[4],
                                "Matched Place URL": r[5],
                            }
                            for r in db_rows
                        ]
                        st.dataframe(table, width="stretch")
                        st.download_button(
                            "Download this run's CSV",
                            rows_to_csv(table),
                            file_name=f"flagged_businesses_run_{run_id}.csv",
                            mime="text/csv",
                            key=f"dl_{run_id}",
                        )
                    else:
                        st.write("No businesses were flagged in this run.")
        conn.close()


if __name__ == "__main__":
    main()
