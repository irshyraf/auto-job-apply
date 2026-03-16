"""
scraper.py — Phase 2a: Job Discovery

Scrapes LinkedIn, Indeed, Glassdoor, Google Jobs via JobSpy.
Scrapes Reed via its public JSON API (requires REED_API_KEY in .env).

For each listing:
  1. Normalise fields to the jobs table schema
  2. Apply post-scrape filters (location, contract type, salary, title/keyword exclusions)
  3. Compute dedup_hash (SHA-256 of company + title + location)
  4. Insert into DB with status='scraped' — skip silently on duplicate

Usage (directly):
    python3 -m modules.scraper                   # run all queries
    python3 -m modules.scraper --priority high   # high-priority queries only
    python3 -m modules.scraper --boards indeed   # single board
"""

import argparse
import hashlib
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

# Reed API uses HTTP Basic Auth
import requests
from jobspy import scrape_jobs

# Allow running as __main__ from project root
sys.path.insert(0, str(Path(__file__).parent.parent))
from config_loader import job_board_targeting, target_profile
from database import get_connection

# Load .env for REED_API_KEY
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import os

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOBSPY_BOARDS = ["linkedin", "indeed", "glassdoor", "google"]

# Maps JobSpy site names → our source_board values
SITE_NAME_MAP = {
    "linkedin":  "linkedin",
    "indeed":    "indeed",
    "glassdoor": "glassdoor",
    "google":    "google",
    "zip_recruiter": "ziprecruiter",
}

# JobSpy job_type values that indicate permanent / full-time employment
PERMANENT_JOB_TYPES = {"fulltime", "full_time", "full-time", None}

# Title fragments that are hard dealbreakers (case-insensitive)
EXCLUDED_TITLE_FRAGMENTS = [
    "marketing assistant",
    "marketing intern",
    "sales assistant",
    "telesales",
    "cold caller",
    "receptionist",
    "administrator",
    "intern",
]

# Description keyword fragments that are hard dealbreakers (case-insensitive)
EXCLUDED_DESCRIPTION_KEYWORDS = [
    "commission only",
    "commission-only",
    "self-employed",
    "self employed",
    "unpaid",
    "volunteer",
    "work experience",
]

SALARY_MINIMUM = 28_000  # GBP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"\b(ltd|limited|llp|llc|inc|plc|group|holdings|uk|worldwide|global|international)\b",
    re.IGNORECASE,
)


def _normalise_company(company: str) -> str:
    """Strip legal suffixes and punctuation so 'MCCANN' == 'McCann Worldgroup Ltd'."""
    c = company.lower().strip()
    c = _COMPANY_SUFFIXES.sub("", c)
    c = re.sub(r"[^a-z0-9\s]", "", c)   # remove punctuation
    c = re.sub(r"\s+", " ", c).strip()
    return c


def _make_dedup_hash(company: str, title: str, location: str) -> str:
    """SHA-256 of normalised company+title+location."""
    raw = f"{_normalise_company(company)}|{title.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _detect_work_setup(row) -> str:
    """Derive 'remote' | 'hybrid' | 'on-site' | 'unknown' from JobSpy row."""
    if row.get("is_remote") is True:
        return "remote"
    wfh = str(row.get("work_from_home_type") or "").lower()
    if "hybrid" in wfh:
        return "hybrid"
    if "remote" in wfh:
        return "remote"
    # Also check description for hints
    desc = str(row.get("description") or "").lower()
    if re.search(r"\bhybrid\b", desc):
        return "hybrid"
    if re.search(r"\bfully remote\b|\b100%\s*remote\b", desc):
        return "remote"
    return "on-site"


def _normalise_salary(row) -> tuple[int | None, int | None]:
    """
    Return (salary_min, salary_max) in GBP annual.
    Returns (None, None) if salary is absent, non-GBP, or clearly hourly (<200).
    """
    currency = str(row.get("currency") or "").upper()
    # Accept GBP, blank (assume GBP for UK boards), or £
    if currency and currency not in ("GBP", "£", ""):
        return None, None

    interval = str(row.get("interval") or "").lower()
    mn = row.get("min_amount")
    mx = row.get("max_amount")

    # Convert to float safely
    try:
        mn = float(mn) if mn is not None else None
    except (ValueError, TypeError):
        mn = None
    try:
        mx = float(mx) if mx is not None else None
    except (ValueError, TypeError):
        mx = None

    if mn is None and mx is None:
        return None, None

    # Annualise if needed
    multipliers = {"hourly": 1872, "daily": 260, "weekly": 52, "monthly": 12, "yearly": 1, "annual": 1}
    mult = multipliers.get(interval, 1)

    if mn is not None:
        mn = int(mn * mult)
    if mx is not None:
        mx = int(mx * mult)

    # Sanity check: reject if max looks like hourly rate (< 500)
    if mx is not None and mx < 500:
        return None, None

    return mn, mx


def _normalise_date(val) -> str | None:
    """Convert date / datetime / string to ISO-8601 string, or None."""
    if val is None:
        return None
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    return str(val)


def _is_location_valid(location: str, work_setup: str) -> bool:
    """Accept London locations and fully-remote roles regardless of location."""
    if work_setup == "remote":
        return True
    loc = (location or "").lower()
    return "london" in loc


def _title_excluded(title: str) -> bool:
    t = title.lower()
    return any(frag in t for frag in EXCLUDED_TITLE_FRAGMENTS)


def _description_excluded(description: str) -> bool:
    d = (description or "").lower()
    return any(kw in d for kw in EXCLUDED_DESCRIPTION_KEYWORDS)


def _contract_type_excluded(job_type_raw) -> bool:
    """Return True if this job_type string indicates non-permanent work."""
    if job_type_raw is None:
        return False  # unknown — let it through, matcher will handle
    jt = str(job_type_raw).lower().replace("-", "_").replace(" ", "_")
    return jt in {"parttime", "part_time", "contract", "internship", "temporary"}


# ---------------------------------------------------------------------------
# Core insert
# ---------------------------------------------------------------------------

def _insert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """
    Insert a single normalised job dict into the jobs table.
    Returns True if inserted, False if skipped (duplicate or filtered).
    """
    try:
        conn.execute("""
            INSERT INTO jobs (
                source_board, source_url, dedup_hash,
                job_title, company_name, location, work_setup,
                salary_min, salary_max, contract_type,
                description_text, date_posted, status,
                company_sector
            ) VALUES (
                :source_board, :source_url, :dedup_hash,
                :job_title, :company_name, :location, :work_setup,
                :salary_min, :salary_max, :contract_type,
                :description_text, :date_posted, 'scraped',
                :company_sector
            )
        """, job)
        return True
    except sqlite3.IntegrityError:
        # dedup_hash UNIQUE constraint — already in DB
        return False


# ---------------------------------------------------------------------------
# JobSpy scraper (LinkedIn, Indeed, Glassdoor, Google)
# ---------------------------------------------------------------------------

def _scrape_via_jobspy(
    boards: list[str],
    query: str,
    results_wanted: int = 50,
    hours_old: int = 72,
) -> list[dict]:
    """
    Run a single query against the given boards via JobSpy.
    Returns a list of normalised job dicts ready for _insert_job().
    """
    try:
        df = scrape_jobs(
            site_name=boards,
            search_term=query,
            location="London, UK",
            country_indeed="uk",
            results_wanted=results_wanted,
            hours_old=hours_old,
            enforce_annual_salary=False,
            description_format="markdown",
            verbose=0,
        )
    except Exception as e:
        print(f"    JobSpy error ({query!r}): {e}")
        return []

    if df is None or df.empty:
        return []

    results = []
    for _, row in df.iterrows():
        row = row.where(row.notna(), other=None).to_dict()

        title = str(row.get("title") or "").strip()
        company = str(row.get("company") or "").strip()
        location = str(row.get("location") or "").strip()
        source_url = str(row.get("job_url") or row.get("job_url_direct") or "").strip()
        source_board = SITE_NAME_MAP.get(str(row.get("site") or "").lower(), "unknown")
        description = str(row.get("description") or "").strip()
        job_type_raw = row.get("job_type")

        if not title or not company or not source_url:
            continue

        work_setup = _detect_work_setup(row)
        salary_min, salary_max = _normalise_salary(row)

        # --- Post-scrape filters ---
        if not _is_location_valid(location, work_setup):
            continue
        if _title_excluded(title):
            continue
        if _description_excluded(description):
            continue
        if _contract_type_excluded(job_type_raw):
            continue
        if salary_min is not None and salary_min < SALARY_MINIMUM:
            continue

        results.append({
            "source_board":    source_board,
            "source_url":      source_url,
            "dedup_hash":      _make_dedup_hash(company, title, location),
            "job_title":       title,
            "company_name":    company,
            "location":        location,
            "work_setup":      work_setup,
            "salary_min":      salary_min,
            "salary_max":      salary_max,
            "contract_type":   "permanent",
            "description_text": description,
            "date_posted":     _normalise_date(row.get("date_posted")),
            "company_sector":  str(row.get("company_industry") or "").strip() or None,
        })

    return results


# ---------------------------------------------------------------------------
# Reed API scraper
# ---------------------------------------------------------------------------

def _scrape_reed(query: str, results_wanted: int = 50) -> list[dict]:
    """
    Scrape Reed using their public API.
    Requires REED_API_KEY environment variable (free at reed.co.uk/developers).
    Falls back gracefully if key is not set.
    """
    api_key = os.environ.get("REED_API_KEY", "")
    if not api_key:
        return []  # Key not configured — skip silently

    base_url = "https://www.reed.co.uk/api/1.0/search"
    params = {
        "keywords": query,
        "locationName": "London",
        "distancefromLocation": 5,
        "resultsToTake": min(results_wanted, 100),
        "permanent": True,
        "fullTime": True,
        "minimumSalary": SALARY_MINIMUM,
    }

    try:
        resp = requests.get(
            base_url,
            params=params,
            auth=(api_key, ""),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    Reed API error ({query!r}): {e}")
        return []

    results = []
    for item in data.get("results", []):
        title = str(item.get("jobTitle") or "").strip()
        company = str(item.get("employerName") or "").strip()
        location = str(item.get("locationName") or "London").strip()
        source_url = f"https://www.reed.co.uk/jobs/{item.get('jobId', '')}"
        description = str(item.get("jobDescription") or "").strip()
        salary_min_raw = item.get("minimumSalary")
        salary_max_raw = item.get("maximumSalary")

        if not title or not company:
            continue
        if _title_excluded(title):
            continue
        if _description_excluded(description):
            continue

        try:
            salary_min = int(salary_min_raw) if salary_min_raw else None
            salary_max = int(salary_max_raw) if salary_max_raw else None
        except (ValueError, TypeError):
            salary_min = salary_max = None

        if salary_min is not None and salary_min < SALARY_MINIMUM:
            continue

        # Reed doesn't surface work_from_home info directly — scan description
        work_setup = "on-site"
        desc_lower = description.lower()
        if re.search(r"\bfully remote\b|\b100%\s*remote\b", desc_lower):
            work_setup = "remote"
        elif re.search(r"\bhybrid\b", desc_lower):
            work_setup = "hybrid"

        results.append({
            "source_board":    "reed",
            "source_url":      source_url,
            "dedup_hash":      _make_dedup_hash(company, title, location),
            "job_title":       title,
            "company_name":    company,
            "location":        location,
            "work_setup":      work_setup,
            "salary_min":      salary_min,
            "salary_max":      salary_max,
            "contract_type":   "permanent",
            "description_text": description,
            "date_posted":     _normalise_date(item.get("date")),
            "company_sector":  None,
        })

    return results


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run_scrape(
    priority: str = "high",
    boards: list[str] | None = None,
    results_per_query: int = 50,
    hours_old: int = 72,
) -> dict:
    """
    Run a full scrape cycle.

    Args:
        priority: "high" | "medium" | "low" | "all"
        boards:   Override board list. Defaults to Tier 1 (linkedin, indeed, glassdoor, google).
        results_per_query: How many results to request per query per board.
        hours_old: Only return listings posted within this many hours.

    Returns:
        {"inserted": int, "skipped_dup": int, "filtered_out": int, "errors": int}
    """
    cfg = job_board_targeting()
    queries_cfg = cfg["search_queries"]

    # Build query list based on priority
    if priority == "high":
        queries = queries_cfg["high_priority"]
    elif priority == "medium":
        queries = queries_cfg["high_priority"] + queries_cfg["medium_priority"]
    elif priority == "low":
        queries = queries_cfg["medium_priority"] + queries_cfg["low_priority"]
    else:  # "all"
        queries = (
            queries_cfg["high_priority"]
            + queries_cfg["medium_priority"]
            + queries_cfg["low_priority"]
        )

    # Board selection — default to Tier 1 JobSpy-supported boards
    if boards is None:
        jobspy_boards = ["linkedin", "indeed", "glassdoor", "google"]
        use_reed = True
    else:
        jobspy_boards = [b for b in boards if b in JOBSPY_BOARDS]
        use_reed = "reed" in boards

    conn = get_connection()
    stats = {"inserted": 0, "skipped_dup": 0, "filtered_out": 0, "errors": 0}

    for i, query in enumerate(queries, 1):
        print(f"  [{i}/{len(queries)}] {query!r}")

        # JobSpy boards
        if jobspy_boards:
            jobs = _scrape_via_jobspy(jobspy_boards, query, results_per_query, hours_old)
            for job in jobs:
                if _insert_job(conn, job):
                    stats["inserted"] += 1
                else:
                    stats["skipped_dup"] += 1
            print(f"    JobSpy → {len(jobs)} listings, {stats['inserted']} inserted so far")

        # Reed API
        if use_reed:
            reed_jobs = _scrape_reed(query, results_per_query)
            for job in reed_jobs:
                if _insert_job(conn, job):
                    stats["inserted"] += 1
                else:
                    stats["skipped_dup"] += 1
            if reed_jobs:
                print(f"    Reed    → {len(reed_jobs)} listings")

        # Polite delay between queries
        if i < len(queries):
            time.sleep(2)

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the job scraper")
    parser.add_argument(
        "--priority",
        choices=["high", "medium", "low", "all"],
        default="high",
        help="Which query priority tier to run (default: high)",
    )
    parser.add_argument(
        "--boards",
        nargs="+",
        choices=["linkedin", "indeed", "glassdoor", "google", "reed"],
        help="Override board list (default: all Tier 1)",
    )
    parser.add_argument(
        "--results",
        type=int,
        default=50,
        help="Results to request per query per board (default: 50)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=72,
        help="Only return listings posted within N hours (default: 72)",
    )
    args = parser.parse_args()

    print(f"\nScraping — priority={args.priority}, boards={args.boards or 'Tier 1 default'}")
    print("-" * 60)

    stats = run_scrape(
        priority=args.priority,
        boards=args.boards,
        results_per_query=args.results,
        hours_old=args.hours,
    )

    print("\n=== Scrape Complete ===")
    print(f"  Inserted:     {stats['inserted']}")
    print(f"  Duplicates:   {stats['skipped_dup']}")
    print(f"  Filtered out: {stats['filtered_out']}")
    print()
