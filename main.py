"""
main.py — Entry point for the Automated Job Application Pipeline.

Pipeline flow (two-stage Review Gate architecture):
  Phase 1  — Repo setup, database, config loader          ← DONE
  Phase 2a — Job scraper (Module 1: scraper.py)           ← DONE
  Phase 2b — Job matcher / scorer (Module 2: matcher.py)  ← DONE
  Phase 3  — CV tailor (Module 3: cv_tailor.py)           ← DONE
  Phase 4  — Answer generator (Module 4: answer_gen.py)   ← on-demand at submit time
  Phase 5  — Review Gate UI (Module 5: review_gate.py)    ← DONE (two-stage)
  Phase 6  — Form submitter (Module 6: submitter.py)      ← DONE
  Phase 7  — Tracker / reporting (Module 7: tracker.py)   ← DONE

Daily workflow:
  1. python3 main.py --auto          → scrape + match → pending_stage_1
  2. streamlit run modules/review_gate.py  → Stage 1 tab: approve/skip (no API cost)
  3. python3 main.py --tailor        → tailor CVs for approved_stage_1 jobs → pending_stage_2
  4. streamlit run modules/review_gate.py  → Stage 2 tab: review full content, approve
  5. python3 -m modules.submitter --submit → submit approved jobs
  6. python3 -m modules.tracker --update <id> <status>

Usage:
    python main.py --auto           # scrape + match (stops before tailoring)
    python main.py --tailor         # tailor CVs for Stage-1-approved jobs
    python main.py --phase scrape   # run a single phase by name
    python main.py --status         # print current pipeline status
    python main.py --skip-scrape    # use with --auto to skip scraping
"""

import argparse
import sys
from pathlib import Path

from config_loader import (
    personal_data,
    answer_bank,
    tone_voice,
    target_profile,
    cv_tailoring_prompt,
    question_classification_rules,
    review_gate_ux,
    job_board_targeting,
)
from database import initialise_database, get_connection


# ---------------------------------------------------------------------------
# Phase functions
# ---------------------------------------------------------------------------

def phase_scrape() -> None:
    """Phase 2a: Discover and store new job postings."""
    from modules.scraper import run_scrape
    print("Running scraper (priority=high, Tier 1 boards)...")
    stats = run_scrape(priority="high")
    print(f"  Inserted: {stats['inserted']}  |  Duplicates: {stats['skipped_dup']}  |  Filtered: {stats['filtered_out']}")


def phase_match() -> None:
    """Phase 2b: Score and filter jobs; select CV variant."""
    from modules.matcher import run_match
    print("Running matcher...")
    stats = run_match()
    print(f"  Pending Stage 1: {stats['matched']}  |  Filtered out: {stats['filtered_out']}")


def phase_tailor_cv() -> None:
    """Phase 3: Tailor CV for each approved_stage_1 job."""
    from modules.cv_tailor import run_tailor
    stats = run_tailor()
    print(f"  OK: {stats['ok']}  Warnings: {stats['warnings']}  Fallbacks: {stats['fallbacks']}  Failed: {stats['failed']}")


def phase_generate_answers() -> None:
    """Phase 4: Classify form questions and generate answers (on-demand at submit time)."""
    print("[Phase 4] Answer Generator — run from the Streamlit UI or call run_answer_gen(job_id, fields) directly.")


def phase_review_gate() -> None:
    """Phase 5: Launch Streamlit Review Gate for human approval."""
    import subprocess
    subprocess.run([sys.executable, "-m", "streamlit", "run",
                    str(Path(__file__).parent / "modules" / "review_gate.py")])


def phase_submit() -> None:
    """Phase 6: Submit approved applications via Playwright."""
    from modules.submitter import run_submit
    print("Running submitter (dry-run by default)...")
    stats = run_submit(dry_run=True)
    print(f"  Dry run OK: {stats.get('dry_run_ok', 0)}  Blocked: {stats.get('blocked', 0)}  Failed: {stats.get('failed', 0)}")


def phase_track() -> None:
    """Phase 7: Update statuses and generate daily digest."""
    from modules.tracker import generate_digest
    print(generate_digest())


# ---------------------------------------------------------------------------
# Pipeline constants (read from job_board_targeting.json if present)
# ---------------------------------------------------------------------------

JOB_CAP = 10    # max jobs shown in Stage 1 per run (highest score first)


# ---------------------------------------------------------------------------
# Queue promotion — bring top queued jobs back to pending_stage_1
# ---------------------------------------------------------------------------

def _promote_queued_jobs(conn) -> int:
    """
    At the start of each run, check if there are slots available in Stage 1.
    If the current pending_stage_1 count is below JOB_CAP, promote the highest-scoring
    queued jobs to fill the remaining slots.
    Returns the number of jobs promoted.
    """
    current_stage1 = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'pending_stage_1'"
    ).fetchone()[0]

    slots_available = JOB_CAP - current_stage1
    if slots_available <= 0:
        return 0

    queued_ids = [
        row["id"] for row in conn.execute(
            """SELECT id FROM jobs
               WHERE status = 'queued'
               ORDER BY match_score DESC
               LIMIT ?""",
            (slots_available,)
        ).fetchall()
    ]

    if not queued_ids:
        return 0

    placeholders = ",".join("?" * len(queued_ids))
    conn.execute(
        f"UPDATE jobs SET status = 'pending_stage_1' WHERE id IN ({placeholders})",
        queued_ids
    )
    conn.commit()
    return len(queued_ids)


# ---------------------------------------------------------------------------
# Cap enforcement — after matching, keep top JOB_CAP in stage_1, queue the rest
# ---------------------------------------------------------------------------

def _apply_job_cap(conn) -> int:
    """
    If more than JOB_CAP jobs are in pending_stage_1, demote the lowest-scoring
    ones to 'queued' so the Stage 1 Review Gate shows at most JOB_CAP jobs.
    Returns the number of jobs queued.
    """
    all_stage1 = conn.execute(
        """SELECT id FROM jobs
           WHERE status = 'pending_stage_1'
           ORDER BY match_score DESC"""
    ).fetchall()

    if len(all_stage1) <= JOB_CAP:
        return 0

    overflow_ids = [row["id"] for row in all_stage1[JOB_CAP:]]
    placeholders = ",".join("?" * len(overflow_ids))
    conn.execute(
        f"UPDATE jobs SET status = 'queued' WHERE id IN ({placeholders})",
        overflow_ids
    )
    conn.commit()
    return len(overflow_ids)


# ---------------------------------------------------------------------------
# Automated pipeline — scrape + match only (stops before tailoring)
# ---------------------------------------------------------------------------

def run_auto_pipeline(skip_scrape: bool = False) -> None:
    """
    Automated portion of the pipeline: scrape → match → stage_1 queue.

    Stops BEFORE tailoring — no API calls made here.
    Human opens the Review Gate (Stage 1 tab) to approve which jobs to tailor.
    Then runs: python3 main.py --tailor

    Answers and cover letters are generated on-the-fly at submission time.
    """
    from modules.scraper import run_scrape
    from modules.matcher import run_match
    from modules.tracker import generate_digest, auto_update_no_response

    print("\n" + "=" * 56)
    print("  AUTO PIPELINE — scrape + match")
    print("=" * 56)

    conn = get_connection()

    # 0. Promote any queued jobs from previous runs
    promoted = _promote_queued_jobs(conn)
    if promoted:
        print(f"\n[0] Promoted {promoted} queued job(s) back to Stage 1.")
    conn.close()

    # 1. Scrape
    if not skip_scrape:
        print("\n[1/2] Scraping jobs...")
        stats = run_scrape(priority="high")
        print(f"  Inserted: {stats['inserted']}  Duplicates: {stats['skipped_dup']}  Filtered: {stats['filtered_out']}")
    else:
        print("\n[1/2] Scrape skipped.")

    # 2. Match → sets status to pending_stage_1 for qualifying jobs
    print("\n[2/2] Matching and scoring...")
    stats = run_match()
    print(f"  New Stage 1 matches: {stats['matched']}  Filtered out: {stats['filtered_out']}")

    # Apply cap: top JOB_CAP stay as pending_stage_1, rest become queued
    conn = get_connection()
    queued = _apply_job_cap(conn)
    stage1_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'pending_stage_1'"
    ).fetchone()[0]
    conn.close()

    if queued:
        print(f"  Cap applied: {queued} job(s) queued for next run (showing top {JOB_CAP})")

    # Auto-update no_response + digest
    auto_update_no_response()
    print("\n" + generate_digest())

    print("\n" + "=" * 56)
    print("  NEXT STEPS")
    print("=" * 56)
    print(f"  {stage1_count} job(s) awaiting Stage 1 review.")
    print("  1. Open Review Gate:  streamlit run modules/review_gate.py")
    print("     → Stage 1 tab: approve jobs to tailor (no API cost here)")
    print("  2. Tailor CVs:        python3 main.py --tailor")
    print("  3. Review Gate again: Stage 2 tab — check tailored CVs + approve")
    print("  4. Submit:            python3 -m modules.submitter --submit")
    print("=" * 56 + "\n")


# ---------------------------------------------------------------------------
# Tailor pipeline — tailor CVs for Stage-1-approved jobs only
# ---------------------------------------------------------------------------

def run_tailor_pipeline() -> None:
    """
    Tailor CVs for all jobs approved at Stage 1 (status='approved_stage_1').
    After tailoring, jobs move to status='pending_stage_2'.
    """
    from modules.cv_tailor import run_tailor

    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'approved_stage_1'"
    ).fetchone()[0]
    conn.close()

    if count == 0:
        print("\nNo jobs approved at Stage 1. Open the Review Gate → Stage 1 tab first.")
        print("  streamlit run modules/review_gate.py\n")
        return

    print(f"\n{'=' * 56}")
    print(f"  TAILOR PIPELINE — {count} job(s) to tailor")
    print(f"{'=' * 56}\n")

    stats = run_tailor()

    print(f"\n{'=' * 56}")
    print("  TAILORING COMPLETE")
    print(f"{'=' * 56}")
    print(f"  OK: {stats['ok']}  Warnings: {stats['warnings']}  Fallbacks: {stats['fallbacks']}  Failed: {stats['failed']}")
    print("\n  NEXT STEPS")
    print("  Open Review Gate → Stage 2 tab to review tailored CVs:")
    print("  streamlit run modules/review_gate.py")
    print(f"{'=' * 56}\n")


# ---------------------------------------------------------------------------
# UI-callable wrapper — used by Streamlit app.py Dashboard
# ---------------------------------------------------------------------------

def run_scrape_and_match() -> dict:
    """
    Scrape jobs and match/score them. Called by the Streamlit Dashboard when
    the user clicks 'Scrape now'. Returns a summary dict the UI can display.

    Returns {"new_matches": int, "queued": int, "inserted": int, "timestamp": str}
    """
    from datetime import datetime
    from modules.scraper import run_scrape
    from modules.matcher import run_match
    from modules.tracker import auto_update_no_response

    conn = get_connection()
    promoted = _promote_queued_jobs(conn)
    conn.close()

    scrape_stats = run_scrape(priority="high")
    match_stats  = run_match()
    auto_update_no_response()

    conn = get_connection()
    queued = _apply_job_cap(conn)
    stage1_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'pending_stage_1'"
    ).fetchone()[0]
    conn.close()

    return {
        "new_matches": stage1_count,
        "queued":      queued,
        "inserted":    scrape_stats.get("inserted", 0),
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


PHASES = {
    "scrape":   phase_scrape,
    "match":    phase_match,
    "tailor":   phase_tailor_cv,
    "answers":  phase_generate_answers,
    "review":   phase_review_gate,
    "submit":   phase_submit,
    "track":    phase_track,
}


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def print_status() -> None:
    """Print a summary of jobs currently in the database by status."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM jobs
        GROUP BY status
        ORDER BY count DESC
    """).fetchall()
    conn.close()

    # Display order that makes pipeline sense
    STATUS_ORDER = [
        "pending_stage_1", "approved_stage_1", "queued",
        "pending_stage_2", "approved",
        "in_progress", "submitted", "interview",
        "no_response", "rejected", "withdrawn",
        "skipped_stage_1", "skipped_stage_2", "filtered_out", "scraped",
    ]
    status_map = {row["status"]: row["count"] for row in rows}

    print("\n=== Pipeline Status ===")
    if not rows:
        print("  No jobs in database yet.")
    else:
        for s in STATUS_ORDER:
            if s in status_map:
                print(f"  {s:<22} {status_map[s]:>4} jobs")
        # Any unexpected statuses
        for s, count in status_map.items():
            if s not in STATUS_ORDER:
                print(f"  {s:<22} {count:>4} jobs")
    print()


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def startup_checks() -> bool:
    """
    Verify configs load and database is reachable.
    Returns True if all checks pass, False otherwise.
    """
    print("Running startup checks...")

    loaders = [
        ("personal_data_vault",           personal_data),
        ("answer_bank",                   answer_bank),
        ("tone_voice_guide",              tone_voice),
        ("target_profile",                target_profile),
        ("cv_tailoring_prompt",           cv_tailoring_prompt),
        ("question_classification_rules", question_classification_rules),
        ("review_gate_ux",                review_gate_ux),
        ("job_board_targeting",           job_board_targeting),
    ]
    for name, loader in loaders:
        try:
            loader()
        except FileNotFoundError as e:
            print(f"  FAIL config/{name}.json — {e}")
            return False

    print("  OK  All 8 config files loaded.")

    try:
        initialise_database()
        conn = get_connection()
        conn.execute("SELECT 1 FROM jobs LIMIT 1")
        conn.close()
        print("  OK  Database reachable.")
    except Exception as e:
        print(f"  FAIL Database — {e}")
        return False

    templates_dir = Path(__file__).parent / "templates"
    for f in ["cv_template_modern_clean.html", "render_cv.js"]:
        if not (templates_dir / f).exists():
            print(f"  WARN templates/{f} not found.")
        else:
            print(f"  OK  templates/{f}")

    print("Startup checks complete.\n")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_full_pipeline() -> None:
    """Run all phases in order."""
    for name, fn in PHASES.items():
        print(f"\n--- {name.upper()} ---")
        fn()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automated Job Application Pipeline"
    )
    parser.add_argument(
        "--phase",
        choices=list(PHASES.keys()),
        help="Run a single phase instead of the full pipeline.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current pipeline status and exit.",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip startup checks (for development use).",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Run scrape + match → jobs queued for Stage 1 Review Gate. No API calls.",
    )
    parser.add_argument(
        "--tailor",
        action="store_true",
        help="Tailor CVs for Stage-1-approved jobs → moves to Stage 2 Review Gate.",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Use with --auto to skip scraping (re-process existing jobs only).",
    )
    args = parser.parse_args()

    if not args.skip_checks:
        ok = startup_checks()
        if not ok:
            print("Startup checks failed. Aborting.")
            sys.exit(1)

    if args.status:
        print_status()
        return

    if args.auto:
        run_auto_pipeline(skip_scrape=args.skip_scrape)
        return

    if args.tailor:
        run_tailor_pipeline()
        return

    if args.phase:
        print(f"\nRunning phase: {args.phase}")
        PHASES[args.phase]()
    else:
        run_full_pipeline()


if __name__ == "__main__":
    main()
