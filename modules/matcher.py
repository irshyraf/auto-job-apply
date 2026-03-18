"""
matcher.py — Phase 2b: Job Matching and Scoring

Reads all jobs with status='new' from the database and scores each one
using only rule-based logic (no Claude API calls in this phase).

Scoring breakdown (max 1.0):
  Title match        0.40  — Tier 1 title = 0.40, Tier 2 = 0.25, soft keyword match = 0.20
  Salary             0.20  — present + meets minimum = 0.20, absent = 0.10
  Work setup         0.15  — remote=0.15, hybrid=0.10, on-site=0.05
  Keyword boost      0.15  — BD/partnerships/commercial keywords in title/desc
  Description length 0.10  — full description available = 0.10

After scoring:
  Dealbreaker detected  → status='filtered_out', dealbreaker_found=1
  match_score < 0.35    → status='filtered_out'
  match_score >= 0.35   → status='matched'

CV variant is selected from target_profile.json cv_variant_selection map.

Usage (directly):
    python3 -m modules.matcher
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config_loader import target_profile
from database import get_connection

# ---------------------------------------------------------------------------
# Load targeting config once at module level
# ---------------------------------------------------------------------------

_profile = None


def _get_profile():
    global _profile
    if _profile is None:
        _profile = target_profile()
    return _profile


# ---------------------------------------------------------------------------
# Title sets (lower-cased for matching)
# ---------------------------------------------------------------------------

def _build_title_sets():
    p = _get_profile()
    tier1 = [t.lower() for t in p["target_job_titles"]["tier_1_primary"]]
    tier2 = [t.lower() for t in p["target_job_titles"]["tier_2_secondary"]]
    return tier1, tier2


# ---------------------------------------------------------------------------
# CV variant selection
# ---------------------------------------------------------------------------

# Maps keyword groups → CV variant name
_CV_VARIANT_RULES = [
    (
        ["business development", "partnerships", "commercial", "new business", "revenue", "growth executive", "client development"],
        "AF_Business_Development",
    ),
    (
        ["account executive", "account manager", "account coordinator", "client services"],
        "AF_Agency_Account_Management",
    ),
    (
        ["marketing executive", "digital marketing", "campaign", "demand gen", "demand generation", "content", "crm", "email marketing"],
        "AF_Marketing",
    ),
    (
        ["sales", "sdr"],
        "AF_Sales",
    ),
]


def _select_cv_variant(job_title: str) -> str:
    t = job_title.lower()
    for keywords, variant in _CV_VARIANT_RULES:
        if any(kw in t for kw in keywords):
            return variant
    return "AF_Resume"  # fallback for ambiguous titles


# ---------------------------------------------------------------------------
# Dealbreaker detection
# ---------------------------------------------------------------------------

# Title prefixes that indicate too-senior roles (checked before scoring)
_SENIORITY_PREFIXES = re.compile(
    r"^(senior|sr\.?\s|head\s+of|director|chief\s|vp\s|vice\s+president|principal\s|lead\s+[a-z]+\s+manager|managing\s+director)",
    re.IGNORECASE,
)

_DEALBREAKER_PATTERNS = [
    r"\bcommission[\s\-]only\b",
    r"\bself[\s\-]employed\b",
    r"\bno\s+base\s+salary\b",
    r"\bOTE\s+only\b",
    r"\bfixed[\s\-]term\b",
    r"\bFTC\b",
    r"\btemporary\s+contract\b",
    r"\bfrequent\s+cold\s+call",
    r"\b100%\s+outbound\b",
    r"\bvisa\s+sponsor",            # requires sponsorship (we don't need it but flag for safety)
    r"\bunpaid\b",
    r"\bwork\s+experience\b",
    r"\binternship\b",
    r"\bvolunteer\b",
    r"\boutside\s+ir35\b",          # contract/day-rate roles
    r"\binside\s+ir35\b",
    r"\b£[\d,]+\s*/\s*(?:day|pd)\b",  # day rates e.g. £900/day
    r"\bper\s+day\s+rate\b",
    r"\bcontract\s+outside\b",
]

_DEALBREAKER_RE = [re.compile(p, re.IGNORECASE) for p in _DEALBREAKER_PATTERNS]


def _detect_dealbreaker(title: str, description: str) -> tuple[bool, str]:
    """
    Returns (is_dealbreaker, reason_string).
    Checks both title and description.
    """
    text = f"{title} {description or ''}"
    for pattern in _DEALBREAKER_RE:
        m = pattern.search(text)
        if m:
            return True, f"Dealbreaker pattern matched: '{m.group()}'"
    return False, ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_BOOST_KEYWORDS = [
    "business development", "partnerships", "commercial", "new business",
    "account management", "client development", "growth", "revenue",
    "marketing", "campaign", "demand generation", "bd",
]


def _score_job(job: dict) -> tuple[float, str]:
    """
    Calculate match_score (0.0–1.0) and a human-readable notes string.
    """
    tier1_titles, tier2_titles = _build_title_sets()
    title = (job["job_title"] or "").lower()
    description = (job["description_text"] or "").lower()
    salary_min = job["salary_min"]
    salary_max = job["salary_max"]
    work_setup = (job["work_setup"] or "").lower()

    score = 0.0
    notes = []

    # --- Title match (up to 0.40) ---
    # First try exact phrase match (all words), then fall back to keyword match
    title_score = 0.0
    title_match_note = None

    for t in tier1_titles:
        # Allow partial overlap: if core words of target title appear in job title
        words = [w for w in t.split() if len(w) > 3]
        if all(w in title for w in words):
            title_score = 0.40
            title_match_note = f"Tier 1 title match ({t})"
            break

    if title_score == 0.0:
        for t in tier2_titles:
            words = [w for w in t.split() if len(w) > 3]
            if all(w in title for w in words):
                title_score = 0.25
                title_match_note = f"Tier 2 title match ({t})"
                break

    # Fallback: if no exact phrase match, check for key business keywords (partial credit)
    if title_score == 0.0:
        key_keywords = [
            "business development", "account executive", "account manager",
            "partnerships", "client development", "commercial", "growth",
            "new business", "sales development", "revenue", "account coordinator"
        ]
        matched_kw = [kw for kw in key_keywords if kw in title]
        if matched_kw:
            # Partial credit: matches core function but not exact title
            title_score = 0.20
            title_match_note = f"Soft title match: {matched_kw[0]}"

    if title_score > 0.0:
        notes.append(title_match_note)
    else:
        notes.append("No title match")

    score += title_score

    # --- Salary (up to 0.20) ---
    if salary_min is not None:
        if salary_min >= 28_000:
            score += 0.20
            notes.append(f"Salary OK (min £{salary_min:,})")
        else:
            score += 0.05
            notes.append(f"Salary below minimum (£{salary_min:,})")
    else:
        # No salary listed — include but reduce score slightly
        score += 0.10
        notes.append("No salary listed")

    # --- Work setup (up to 0.15) ---
    if work_setup == "remote":
        score += 0.15
        notes.append("Remote")
    elif work_setup == "hybrid":
        score += 0.10
        notes.append("Hybrid")
    else:
        score += 0.05
        notes.append("On-site")

    # --- Keyword boost (up to 0.15) ---
    boost_hits = [kw for kw in _BOOST_KEYWORDS if kw in title or kw in description[:500]]
    if boost_hits:
        kw_score = min(len(boost_hits) * 0.03, 0.15)
        score += kw_score
        notes.append(f"Keywords: {', '.join(boost_hits[:3])}")

    # --- Description quality (up to 0.10) ---
    if len(description) > 300:
        score += 0.10
        notes.append("Full description")
    elif len(description) > 50:
        score += 0.05

    # Cap at 1.0
    score = min(round(score, 3), 1.0)
    return score, " | ".join(notes)


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run_match() -> dict:
    """
    Score all jobs with status='new'.
    Returns {"matched": int, "filtered_out": int, "already_processed": int}
    """
    conn = get_connection()

    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'new'"
    ).fetchall()

    stats = {"matched": 0, "filtered_out": 0, "already_processed": 0}

    if not rows:
        print("  No jobs with status='new' to process.")
        conn.close()
        return stats

    print(f"  Processing {len(rows)} scraped jobs...")

    for row in rows:
        job = dict(row)

        # --- Seniority filter ---
        if _SENIORITY_PREFIXES.match(job["job_title"].strip()):
            conn.execute(
                "UPDATE jobs SET status='filtered_out', match_score=0.0, match_notes=? WHERE id=?",
                (f"Too senior: '{job['job_title']}'", job["id"])
            )
            stats["filtered_out"] += 1
            continue

        # --- Dealbreaker check ---
        is_db, reason = _detect_dealbreaker(
            job["job_title"], job["description_text"] or ""
        )
        if is_db:
            conn.execute(
                "UPDATE jobs SET status='filtered_out', dealbreaker_found=1, match_score=0.0, match_notes=? WHERE id=?",
                (reason, job["id"])
            )
            stats["filtered_out"] += 1
            continue

        # --- Score ---
        score, notes = _score_job(job)

        if score < 0.35:
            conn.execute(
                "UPDATE jobs SET status='filtered_out', match_score=?, match_notes=? WHERE id=?",
                (score, f"Score too low ({score}) | {notes}", job["id"])
            )
            stats["filtered_out"] += 1
            continue

        # --- Select CV variant ---
        cv_variant = _select_cv_variant(job["job_title"])

        conn.execute("""
            UPDATE jobs
            SET status='matched',
                match_score=?,
                match_notes=?,
                cv_variant_used=?
            WHERE id=?
        """, (score, notes, cv_variant, job["id"]))
        stats["matched"] += 1

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running matcher...")
    stats = run_match()
    print("\n=== Match Complete ===")
    print(f"  Pending review:  {stats['matched']}")
    print(f"  Filtered out:    {stats['filtered_out']}")
    print()
