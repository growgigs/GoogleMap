"""
Google Maps review-check dashboard.

A password-protected web dashboard (built with Streamlit) that lets you
paste or upload a list of businesses, checks each one's recent Google Maps
reviews via Apify, and shows/lets you download only the businesses with a
1-2 star review (with text) posted in the last DAYS_THRESHOLD days (see
gmaps_checker.py). It also keeps a history of past runs in a local SQLite
file (history.db) so you can look back without re-running everything.

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

import streamlit as st

from gmaps_checker import DAYS_THRESHOLD, OUTPUT_FIELDS, check_businesses_parallel

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
            "(run_id, business_name, reviewer_name, star_rating, review_age, matched_place_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                row["Business Name"],
                row["Reviewer Name"],
                row["Star Rating"],
                row["Review Age"],
                row["Matched Place URL"],
            ),
        )
    conn.commit()
    return run_id


def rows_to_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_FIELDS)
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


def main():
    if not check_password():
        return

    api_token = st.secrets.get("APIFY_API_TOKEN")
    if not api_token:
        st.error("APIFY_API_TOKEN isn't set in this app's Secrets. Add it in Streamlit Cloud's app settings.")
        return

    st.title("Google Maps Review Checker")
    st.caption(
        f"Flags businesses with a 1-2 star review (with written text) posted in the last {DAYS_THRESHOLD} days."
    )

    tab_check, tab_history = st.tabs(["Check businesses", "History"])

    with tab_check:
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
            "...or upload a CSV (columns: business_name, city, state, maps_url)", type="csv"
        )

        businesses = []
        if uploaded is not None:
            text = uploaded.getvalue().decode("utf-8")
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                businesses.append(
                    (
                        row.get("business_name", ""),
                        row.get("city", ""),
                        row.get("state", ""),
                        row.get("maps_url", ""),
                    )
                )
        elif pasted.strip():
            businesses = parse_pasted_lines(pasted)

        if st.button("Run check", type="primary", disabled=not businesses):
            progress_area = st.empty()
            log_area = st.container()
            results_by_index = {}
            checked_count = 0
            total = len(businesses)

            for index, result in check_businesses_parallel(api_token, businesses):
                results_by_index[index] = result
                if result["status"] == "blank":
                    continue
                checked_count += 1
                progress_area.write(f"Checked {checked_count}/{total}...")

                if result["status"] == "flagged":
                    row = result["row"]
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

        if st.session_state.get("last_run_rows") is not None:
            rows = st.session_state["last_run_rows"]
            st.subheader("2. Results")
            if rows:
                st.dataframe(rows, width="stretch")
                st.download_button(
                    "Download CSV",
                    rows_to_csv(rows),
                    file_name="flagged_businesses.csv",
                    mime="text/csv",
                )
            else:
                st.info("No businesses flagged in this run.")

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
                        "SELECT business_name, reviewer_name, star_rating, review_age, matched_place_url "
                        "FROM flagged_reviews WHERE run_id = ?",
                        (run_id,),
                    ).fetchall()
                    if db_rows:
                        table = [
                            {
                                "Business Name": r[0],
                                "Reviewer Name": r[1],
                                "Star Rating": r[2],
                                "Review Age": r[3],
                                "Matched Place URL": r[4],
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
