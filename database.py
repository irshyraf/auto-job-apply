"""
database.py — SQLite schema creation and connection management.
All tables are defined here. Run this file directly to initialise a fresh database.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "jobs.db"


def get_connection() -> sqlite3.Connection:
    """Return a connection with row_factory set so rows behave like dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialise_database() -> None:
    """Create all tables if they do not already exist."""
    conn = get_connection()
    cursor = conn.cursor()

    # ------------------------------------------------------------------
    # TABLE: jobs
    # One row per unique job posting discovered by the scraper.
    # dedup_hash prevents cross-board duplicates (hash of company+title+location).
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Source metadata
            source_board        TEXT    NOT NULL,           -- e.g. "linkedin", "indeed", "reed"
            source_url          TEXT    NOT NULL,
            dedup_hash          TEXT    NOT NULL UNIQUE,    -- SHA-256 of company+title+location

            -- Job details (populated by scraper)
            job_title           TEXT    NOT NULL,
            company_name        TEXT    NOT NULL,
            location            TEXT,
            work_setup          TEXT,                       -- "remote" | "hybrid" | "on-site"
            salary_min          INTEGER,                    -- in GBP, nullable
            salary_max          INTEGER,
            contract_type       TEXT,                       -- "permanent" | "contract" | etc.
            description_text    TEXT,

            -- Dates
            date_posted         TEXT,                       -- ISO-8601 string from source
            date_scraped        TEXT    NOT NULL DEFAULT (datetime('now')),

            -- Processing status
            -- Allowed values:
            --   scraped           → just discovered by scraper
            --   pending_stage_1   → matched, awaiting Stage 1 lightweight review
            --   approved_stage_1  → approved at Stage 1, queued for CV tailoring
            --   skipped_stage_1   → rejected at Stage 1 (no API cost incurred)
            --   queued            → matched but beyond JOB_CAP; auto-promoted next run
            --   pending_stage_2   → tailored, awaiting Stage 2 full content review
            --   skipped_stage_2   → rejected at Stage 2
            --   approved          → approved at Stage 2, ready to submit
            --   in_progress       → submission in flight
            --   submitted         → successfully submitted
            --   no_response       → submitted >14 days, no reply
            --   interview         → interview booked
            --   rejected          → rejected by employer
            --   withdrawn         → withdrawn by candidate
            --   filtered_out      → removed by automated filters (legacy)
            status              TEXT    NOT NULL DEFAULT 'scraped',

            -- Scoring & matching (populated by matcher module)
            match_score         REAL,                       -- 0.0 – 1.0
            match_notes         TEXT,                       -- brief rationale from matcher
            cv_variant_used     TEXT,                       -- e.g. "BD", "Marketing"

            -- Company research (populated by matcher / review gate)
            company_dossier     TEXT,                       -- raw scraped company info
            company_sector      TEXT,

            -- Review Gate metadata
            review_approved_at  TEXT,                       -- ISO-8601, set when user approves
            submitted_at        TEXT,                       -- ISO-8601, set on submission
            application_ref     TEXT,                       -- confirmation number / reference

            -- Flags
            flagged_visa        INTEGER NOT NULL DEFAULT 0, -- 1 if visa expiry question found
            dealbreaker_found   INTEGER NOT NULL DEFAULT 0  -- 1 if hard dealbreaker detected
        )
    """)

    # ------------------------------------------------------------------
    # TABLE: application_answers
    # One row per form field per job application.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS application_answers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

            -- Field identity
            field_name      TEXT    NOT NULL,   -- label or name attribute from the form
            field_type      TEXT,               -- "text_short" | "text_long" | "dropdown" |
                                                --  "radio" | "checkbox" | "file_upload"

            -- Classification (from question_classification_rules.json)
            tier            INTEGER NOT NULL,   -- 1 | 2 | 3 | 4
            competency_tags TEXT,               -- JSON array of tags, used for Tier 4

            -- Answer content
            answer_text     TEXT,               -- final answer to be submitted
            answer_source   TEXT,               -- "auto_vault" | "auto_bank" | "ai_generated" | "manual"

            -- Tier 4 traceability
            story_id        TEXT,               -- e.g. "AB-003", links back to answer_bank.json

            -- Review Gate state
            needs_review    INTEGER NOT NULL DEFAULT 0,  -- 1 = amber highlight in Review Gate
            flagged         INTEGER NOT NULL DEFAULT 0,  -- 1 = red, blocks submission (visa expiry)
            user_edited     INTEGER NOT NULL DEFAULT 0,  -- 1 = user overrode AI answer
            approved        INTEGER NOT NULL DEFAULT 0,  -- 1 = user approved in Review Gate

            -- Audit
            generated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # TABLE: api_usage_log
    # One row per Claude API call. Used for the "API spend this month" metric
    # on the Dashboard and the budget enforcement in Settings.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            module          TEXT    NOT NULL,   -- "cv_tailor" | "answer_gen"
            call_type       TEXT    NOT NULL,   -- "tailor" | "tier3" | "tier4" | "cover_letter"
            input_tokens    INTEGER NOT NULL DEFAULT 0,
            output_tokens   INTEGER NOT NULL DEFAULT 0,
            cost_usd        REAL    NOT NULL DEFAULT 0.0,
            timestamp       TEXT    NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage_log(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_job_id    ON api_usage_log(job_id)")

    # ------------------------------------------------------------------
    # INDEXES — speed up the most common queries
    # ------------------------------------------------------------------
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_score       ON jobs(match_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_scraped     ON jobs(date_scraped)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_answers_job_id   ON application_answers(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_answers_tier     ON application_answers(tier)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_answers_flagged  ON application_answers(flagged)")

    conn.commit()
    conn.close()
    print(f"Database initialised at: {DB_PATH}")


def calculate_api_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    model: str = "claude-sonnet-4-20250514"
) -> float:
    """
    Calculate API cost in USD for a Claude API call.

    Claude Sonnet 4 pricing (as of March 2026):
    - Regular input: $3 per 1M tokens
    - Cache creation: $3.75 per 1M tokens (25% surcharge)
    - Cache read: $0.30 per 1M tokens (90% discount)
    - Output: $15 per 1M tokens
    """
    if model not in ("claude-sonnet-4-20250514", "claude-sonnet-4.1-20250514"):
        model = "claude-sonnet-4-20250514"  # default

    # All our models use Sonnet pricing
    INPUT_COST_PER_M = 3.0
    CACHE_CREATION_COST_PER_M = 3.75
    CACHE_READ_COST_PER_M = 0.30
    OUTPUT_COST_PER_M = 15.0

    regular_input_cost = (input_tokens - cache_creation_tokens - cache_read_tokens) * (INPUT_COST_PER_M / 1_000_000)
    cache_creation_cost = cache_creation_tokens * (CACHE_CREATION_COST_PER_M / 1_000_000)
    cache_read_cost = cache_read_tokens * (CACHE_READ_COST_PER_M / 1_000_000)
    output_cost = output_tokens * (OUTPUT_COST_PER_M / 1_000_000)

    return round(regular_input_cost + cache_creation_cost + cache_read_cost + output_cost, 4)


def log_api_usage(job_id: int | None, module: str, call_type: str,
                  input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    """Insert one row into api_usage_log. Called after every Claude API call."""
    from datetime import datetime, timezone
    conn = get_connection()
    conn.execute(
        """INSERT INTO api_usage_log (job_id, module, call_type, input_tokens, output_tokens, cost_usd, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, module, call_type, input_tokens, output_tokens, cost_usd,
         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


def get_monthly_spend() -> float:
    """Return total API spend (USD) for the current calendar month."""
    conn = get_connection()
    row = conn.execute(
        """SELECT COALESCE(SUM(cost_usd), 0.0) as total
           FROM api_usage_log
           WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')"""
    ).fetchone()
    conn.close()
    return float(row["total"])


if __name__ == "__main__":
    initialise_database()
