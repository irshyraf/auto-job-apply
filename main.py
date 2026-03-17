"""
main.py — Entry point for the Automated Job Application Pipeline.

Pipeline phases (build order from handoff brief):
  Phase 1  — Repo setup, database, config loader          ← DONE
  Phase 2a — Job scraper (Module 1: scraper.py)           ← DONE
  Phase 2b — Job matcher / scorer (Module 2: matcher.py)  ← DONE
  Phase 3  — CV tailor (Module 3: cv_tailor.py)           ← DONE
  Phase 4  — Answer generator (Module 4: answer_gen.py)
  Phase 5  — Review Gate UI (Module 5: review_gate.py)    ← DONE
  Phase 6  — Form submitter (Module 6: submitter.py)      ← DONE
  Phase 7  — Tracker / reporting (Module 7: tracker.py)  ← DONE

Usage:
    python main.py                  # run the full pipeline
    python main.py --phase scrape   # run a single phase by name
    python main.py --status         # print current pipeline status
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
# Phase stubs — each will be replaced by the real module import in later phases
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
    print(f"  Pending review: {stats['matched']}  |  Filtered out: {stats['filtered_out']}")


def phase_tailor_cv() -> None:
    """Phase 3: Tailor CV for each approved job."""
    from modules.cv_tailor import run_tailor
    stats = run_tailor()
    print(f"  OK: {stats['ok']}  Warnings: {stats['warnings']}  Fallbacks: {stats['fallbacks']}  Failed: {stats['failed']}")


def phase_generate_answers() -> None:
    """Phase 4: Classify form questions and generate answers."""
    from modules.answer_gen import run_answer_gen
    print("[Phase 4] Answer Generator — call run_answer_gen(job_id, fields) per application.")


def phase_review_gate() -> None:
    """Phase 5: Launch Streamlit Review Gate for human approval."""
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "streamlit", "run",
                    str(Path(__file__).parent / "modules" / "review_gate.py")])


def phase_submit() -> None:
    """Phase 6: Submit approved applications via Playwright."""
    from modules.submitter import run_submit
    print("Running submitter (dry-run by default)...")
    stats = run_submit(dry_run=True)
    print(f"  Dry run OK: {stats.get('dry_run_ok',0)}  Blocked: {stats.get('blocked',0)}  Failed: {stats.get('failed',0)}")


def phase_track() -> None:
    """Phase 7: Update statuses and generate daily digest."""
    from modules.tracker import generate_digest
    print(generate_digest())


# ---------------------------------------------------------------------------
# Automated pipeline (scrape → match → tailor → digest)
# ---------------------------------------------------------------------------

JOB_CAP          = 10    # max jobs tailored per auto run (highest score first)
TAILOR_SCORE_MIN = 0.60  # only tailor CVs for jobs at or above this match score


def run_auto_pipeline(skip_scrape: bool = False) -> None:
    """
    Runs the fully automated portion of the pipeline:
      1. Scrape new jobs
      2. Match / score
      3. Tailor CVs (top JOB_CAP jobs scoring >= TAILOR_SCORE_MIN only)
      4. Print daily digest

    Answers and cover letters are NOT generated here.
    They are generated on-the-fly by the submitter when it encounters
    the real fields on each application form.

    Human steps (Review Gate + Submit) are NOT run here — open them manually.
    """
    from modules.scraper   import run_scrape
    from modules.matcher   import run_match
    from modules.cv_tailor import run_tailor
    from modules.tracker   import generate_digest, auto_update_no_response
    from database import get_connection

    print("\n" + "=" * 56)
    print("  AUTO PIPELINE — starting")
    print("=" * 56)

    # 1. Scrape
    if not skip_scrape:
        print("\n[1/3] Scraping jobs...")
        stats = run_scrape(priority="high")
        print(f"  Inserted: {stats['inserted']}  Duplicates: {stats['skipped_dup']}  Filtered: {stats['filtered_out']}")
    else:
        print("\n[1/3] Scrape skipped.")

    # 2. Match
    print("\n[2/3] Matching and scoring...")
    stats = run_match()
    print(f"  Pending review: {stats['matched']}  Filtered out: {stats['filtered_out']}")

    # 3. Tailor CVs — top JOB_CAP jobs scoring >= TAILOR_SCORE_MIN, no CV yet
    print(f"\n[3/3] Tailoring CVs (score >= {TAILOR_SCORE_MIN}, top {JOB_CAP})...")
    conn_t = get_connection()
    high_score_ids = [
        row["id"] for row in conn_t.execute(
            """SELECT id FROM jobs
               WHERE status = 'pending_review'
                 AND match_score >= ?
                 AND (match_notes IS NULL OR match_notes NOT LIKE '%PDF:%')
               ORDER BY match_score DESC
               LIMIT ?""",
            (TAILOR_SCORE_MIN, JOB_CAP)
        ).fetchall()
    ]
    conn_t.close()

    if high_score_ids:
        tailor_stats = run_tailor(job_ids=high_score_ids)
        print(f"  OK: {tailor_stats['ok']}  Warnings: {tailor_stats['warnings']}  Failed: {tailor_stats['failed']}")
    else:
        print("  No new jobs need CV tailoring.")

    # 4. Auto-update no_response + digest
    auto_update_no_response()
    print("\n" + generate_digest())

    print("\n" + "=" * 56)
    print("  NEXT STEPS")
    print("=" * 56)
    print("  Review applications:  streamlit run modules/review_gate.py")
    print("  Submit approved jobs: python3 -m modules.submitter --submit")
    print("=" * 56 + "\n")


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

    print("\n=== Pipeline Status ===")
    if not rows:
        print("  No jobs in database yet.")
    else:
        for row in rows:
            print(f"  {row['status']:<20} {row['count']:>4} jobs")
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

    # 1. Config files
    loaders = [
        ("personal_data_vault", personal_data),
        ("answer_bank",         answer_bank),
        ("tone_voice_guide",    tone_voice),
        ("target_profile",      target_profile),
        ("cv_tailoring_prompt", cv_tailoring_prompt),
        ("question_classification_rules", question_classification_rules),
        ("review_gate_ux",      review_gate_ux),
        ("job_board_targeting", job_board_targeting),
    ]
    for name, loader in loaders:
        try:
            loader()
        except FileNotFoundError as e:
            print(f"  FAIL config/{name}.json — {e}")
            return False

    print("  OK  All 8 config files loaded.")

    # 2. Database
    try:
        initialise_database()
        conn = get_connection()
        conn.execute("SELECT 1 FROM jobs LIMIT 1")
        conn.close()
        print("  OK  Database reachable.")
    except Exception as e:
        print(f"  FAIL Database — {e}")
        return False

    # 3. Template files
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
        help="Run the automated pipeline: scrape → match → tailor → answers → digest.",
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

    if args.phase:
        print(f"\nRunning phase: {args.phase}")
        PHASES[args.phase]()
    else:
        run_full_pipeline()


if __name__ == "__main__":
    main()
