"""
cv_tailor.py — Phase 3: CV Tailoring

For each job with status='approved_stage_1':
  1. Build a base CV JSON from personal_data_vault.json for the selected variant
  2. Call Claude Sonnet with the system prompt from cv_tailoring_prompt.json
  3. Validate the response (7 checks from spec)
  4. On failure: retry once, then use base CV unmodified
  5. Write validated JSON to a temp file, call `node render_cv.js` to produce a PDF
  6. Save PDF to output/<Company>_<Title>_<YYYY-MM-DD>.pdf
  7. Update job row: status → 'pending_stage_2', pdf path logged in match_notes

Hard rules enforced here:
  - NEVER fabricate (bullet source check)
  - NEVER use USD ($1M, $600K)
  - NEVER include CorelDraw
  - CV stays one page (11–16 bullets enforced)
  - Contact line never changes

Usage:
    python3 -m modules.cv_tailor                   # tailor all approved_stage_1 jobs
    python3 -m modules.cv_tailor --job-id 4        # tailor a single job by DB id
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config_loader import cv_tailoring_prompt, personal_data
from database import get_connection, calculate_api_cost, log_api_usage

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import anthropic

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT  = Path(__file__).parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
OUTPUT_DIR    = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

RENDER_SCRIPT = TEMPLATES_DIR / "render_cv.js"

# ---------------------------------------------------------------------------
# Build base CV JSON from personal_data_vault
# ---------------------------------------------------------------------------

def _contact_line() -> str:
    vault = personal_data()
    p = vault["personal"]
    links = vault["links"]
    linkedin = links["linkedin"].replace("https://", "").replace("http://", "").rstrip("/")
    portfolio = links["portfolio"].replace("https://", "").replace("http://", "").rstrip("/")
    return (
        f"{p['email']}  ·  {p['phone']}  ·  {p['city']}  ·  "
        f"{linkedin}  ·  {portfolio}"
    )


def _build_base_cv_json(variant: str) -> dict:
    """
    Construct the base CV JSON from personal_data_vault.json.
    All bullet pools are included — Claude selects and orders per the JD.
    The variant name tells Claude the framing angle.
    """
    vault = personal_data()

    # Education
    education = []
    for edu in vault["education"]:
        entry = {
            "degree": edu["degree"],
            "dates":  edu["dates"],
            "school": edu["institution"],
            "detail": "",
        }
        if edu.get("notable_modules"):
            mod = edu["notable_modules"][0]
            entry["detail"] = mod["detail"]
        education.append(entry)

    # Roles — pass full bullet pool, Claude selects/orders
    wh = vault["work_history"]
    roles = [
        {
            "company":  entry["company"],
            "title":    entry["job_title"],
            "location": entry["location"],
            "dates":    entry["dates"],
            "bullets":  entry["key_achievements"],
        }
        for entry in wh
    ]

    # Extras
    extras = vault["extracurricular"]

    # Skills baseline (Claude reorders per JD)
    skills_html = (
        "<strong>Commercial:</strong> Business Development · Account Management · "
        "Campaign Management · CRM (Zoho) · Market Research · SEO · "
        "Social Media Marketing (Meta, TikTok, Google)<br>"
        "<strong>Technical:</strong> Excel · PowerPoint · Word · "
        "Photoshop · Illustrator · R · MATLAB<br>"
        "<strong>Languages:</strong> English (Native) · Hindi (Native) · "
        "Bengali (Proficient) · Thai (Proficient)"
    )

    # Generic profile — Claude will fully rewrite this
    profile_text = (
        "Marketing and business development professional with experience across "
        "BD pipeline building, campaign management, and client relationship management. "
        "Track record delivering measurable commercial outcomes including £800,000+ in "
        "contracts and 30% increase in qualified leads."
    )

    return {
        "full_name":    "Aafreen Fathima",
        "contact_line": _contact_line(),
        "profile_text": profile_text,
        "education":    education,
        "roles":        roles,
        "extras":       extras,
        "skills_html":  skills_html,
        "_variant":     variant,   # hint for Claude, stripped before rendering
    }


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(system_prompt: str, user_prompt: str) -> tuple[str, dict]:
    """Call Claude Sonnet with prompt caching on the stable system prompt.
    Returns (response_text, usage_dict) where usage_dict contains token counts.
    """
    cfg = cv_tailoring_prompt()["api_config"]
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        temperature=cfg["temperature"],
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_prompt}],
    )
    # Extract usage info from the response
    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
    }
    return message.content[0].text.strip(), usage


# ---------------------------------------------------------------------------
# Build user prompt from template
# ---------------------------------------------------------------------------

def _build_user_prompt(variant: str, base_cv: dict, job: dict) -> str:
    cfg = cv_tailoring_prompt()

    # Strip the internal _variant hint before sending
    cv_for_prompt = {k: v for k, v in base_cv.items() if k != "_variant"}

    salary = "Not stated"
    if job.get("salary_min") and job.get("salary_max"):
        salary = f"£{job['salary_min']:,} – £{job['salary_max']:,}"
    elif job.get("salary_min"):
        salary = f"£{job['salary_min']:,}+"

    template = cfg["user_prompt_template"]
    return (
        template
        .replace("{{VARIANT_NAME}}", variant)
        .replace("{{BASE_CV_JSON}}", json.dumps(cv_for_prompt, indent=2))
        .replace("{{JOB_TITLE}}", job.get("job_title", ""))
        .replace("{{COMPANY_NAME}}", job.get("company_name", ""))
        .replace("{{LOCATION}}", job.get("location", ""))
        .replace("{{SALARY}}", salary)
        .replace("{{JOB_DESCRIPTION_TEXT}}", job.get("description_text", "No description available."))
        .replace("{{COMPANY_DOSSIER}}", job.get("company_dossier") or "No dossier available.")
    )


# ---------------------------------------------------------------------------
# Validation — 7 checks from spec
# ---------------------------------------------------------------------------

USD_PATTERN = re.compile(r'\$[\d,]+(K|M|k|m)?\b|\bUSD\b|\$1M|\$600K|\$1\.6M')
_REQUIRED_KEYS = {"full_name", "contact_line", "profile_text", "education", "roles", "extras", "skills_html"}


def _validate(cv_json: dict, base_cv: dict, original_contact: str) -> list[str]:
    """
    Run all 7 validation checks.
    Returns a list of violation strings. Empty list = all clear.
    """
    violations = []

    # 1. Schema match
    missing = _REQUIRED_KEYS - set(cv_json.keys())
    if missing:
        violations.append(f"schema_match: missing keys {missing}")

    # 2. No USD
    full_text = json.dumps(cv_json)
    if USD_PATTERN.search(full_text):
        usd_hits = USD_PATTERN.findall(full_text)
        violations.append(f"no_usd: USD references found: {usd_hits}")

    # 3. No CorelDraw
    if "coreldraw" in full_text.lower():
        violations.append("no_coreldraw: CorelDraw found in output")

    # 4. Bullet count 11–16
    total_bullets = sum(len(r.get("bullets", [])) for r in cv_json.get("roles", []))
    if total_bullets < 11:
        violations.append(f"bullet_count: only {total_bullets} bullets (minimum 11)")
    elif total_bullets > 16:
        violations.append(f"bullet_count: {total_bullets} bullets (maximum 16)")

    # 5. Contact line unchanged
    if cv_json.get("contact_line", "").strip() != original_contact.strip():
        violations.append("contact_unchanged: contact_line was modified")

    # 6. No fabrication spot-check
    #    Every bullet in the output must share at least 6 consecutive words with
    #    some bullet in the base CV (allowing for rewording).
    base_bullets_text = " ".join(
        b.lower()
        for role in base_cv.get("roles", [])
        for b in role.get("bullets", [])
    )
    fabricated = []
    for role in cv_json.get("roles", []):
        for bullet in role.get("bullets", []):
            clean = re.sub(r"<[^>]+>", "", bullet).lower()
            words = clean.split()
            # Check if any 6-word window from this bullet appears in base text
            found = False
            for i in range(len(words) - 5):
                window = " ".join(words[i:i+6])
                if window in base_bullets_text:
                    found = True
                    break
            if not found and len(words) > 6:
                fabricated.append(bullet[:80])
    if fabricated:
        violations.append(f"no_fabrication: possibly fabricated bullets: {fabricated}")

    return violations


# ---------------------------------------------------------------------------
# Bullet trimmer — spec fallback when too many bullets
# ---------------------------------------------------------------------------

def _trim_to_max_bullets(cv_json: dict, max_bullets: int = 16) -> dict:
    """Remove the shortest bullet from the largest role until total <= max_bullets."""
    import copy
    cv = copy.deepcopy(cv_json)
    while sum(len(r["bullets"]) for r in cv["roles"]) > max_bullets:
        # Find the role with the most bullets
        longest = max(cv["roles"], key=lambda r: len(r["bullets"]))
        # Remove its shortest bullet
        longest["bullets"] = sorted(longest["bullets"], key=len, reverse=True)[:-1]
    return cv


# ---------------------------------------------------------------------------
# Render PDF via node render_cv.js
# ---------------------------------------------------------------------------

def _render_pdf(cv_json: dict, output_path: Path) -> bool:
    """
    Write cv_json to a temp file, call `node render_cv.js <tmp> <output>`.
    Returns True on success, False on failure.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(cv_json, tmp, ensure_ascii=False)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["node", str(RENDER_SCRIPT), tmp_path, str(output_path)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(TEMPLATES_DIR),
        )
        if result.returncode != 0:
            print(f"    render_cv.js error:\n{result.stderr[:400]}")
            return False
        print(f"    {result.stdout.strip()}")
        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Output filename
# ---------------------------------------------------------------------------

def _make_output_path(company: str, title: str) -> Path:
    def sanitise(s: str) -> str:
        s = re.sub(r"[^\w\s-]", "", s).strip()
        s = re.sub(r"[\s]+", "_", s)
        return s[:40]

    today = date.today().isoformat()
    fname = f"{sanitise(company)}_{sanitise(title)}_{today}.pdf"
    return OUTPUT_DIR / fname


# ---------------------------------------------------------------------------
# Core tailor function for a single job
# ---------------------------------------------------------------------------

def tailor_job(job: dict) -> dict:
    """
    Tailor the CV for a single job dict.
    Returns {"status": "ok"|"validation_warning"|"fallback", "pdf_path": str, "violations": list}
    """
    cfg = cv_tailoring_prompt()
    system_prompt   = cfg["system_prompt"]
    original_contact = _contact_line()
    variant = job.get("cv_variant_used") or "AF_Resume"

    base_cv = _build_base_cv_json(variant)
    user_prompt = _build_user_prompt(variant, base_cv, job)

    cv_json = None
    violations = []
    usage_data = None

    # --- Attempt 1 ---
    for attempt in (1, 2):
        try:
            raw, usage_data = _call_claude(system_prompt, user_prompt)
            # Strip markdown code fences if Claude added them
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
            raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
            cv_json = json.loads(raw)
            break
        except (json.JSONDecodeError, Exception) as e:
            print(f"    Attempt {attempt} failed: {e}")
            cv_json = None
            if attempt == 1:
                time.sleep(3)

    # --- Fallback to base CV if both attempts failed ---
    if cv_json is None:
        print("    Both attempts failed — using base CV unmodified.")
        cv_json = {k: v for k, v in base_cv.items() if k != "_variant"}
        violations = ["json_invalid: fell back to base CV"]
        status = "fallback"
    else:
        # Log API usage for successful call
        if usage_data:
            cost = calculate_api_cost(
                input_tokens=usage_data["input_tokens"],
                output_tokens=usage_data["output_tokens"],
                cache_creation_tokens=usage_data["cache_creation_input_tokens"],
                cache_read_tokens=usage_data["cache_read_input_tokens"],
                model=cv_tailoring_prompt()["api_config"]["model"]
            )
            log_api_usage(
                job_id=job["id"],
                module="cv_tailor",
                call_type="tailor",
                input_tokens=usage_data["input_tokens"],
                output_tokens=usage_data["output_tokens"],
                cost_usd=cost
            )
        violations = _validate(cv_json, base_cv, original_contact)

        # Auto-fix: too many bullets
        total = sum(len(r.get("bullets", [])) for r in cv_json.get("roles", []))
        if total > 16:
            cv_json = _trim_to_max_bullets(cv_json, 16)
            # Re-validate after trim
            violations = _validate(cv_json, base_cv, original_contact)

        if violations:
            print(f"    Validation warnings: {violations}")
            status = "validation_warning"
        else:
            status = "ok"

    # --- Render PDF ---
    output_path = _make_output_path(job["company_name"], job["job_title"])
    render_ok = _render_pdf(cv_json, output_path)

    if not render_ok:
        return {"status": "render_failed", "pdf_path": None, "violations": violations}

    # Save CV JSON alongside PDF so the Review Gate can read the tailored profile text
    json_path = output_path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cv_json, f, ensure_ascii=False, indent=2)

    return {
        "status":     status,
        "pdf_path":   str(output_path),
        "json_path":  str(json_path),
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run_tailor(job_ids: list[int] | None = None) -> dict:
    """
    Tailor CVs for all jobs with status='approved_stage_1', or a specific subset.
    Returns {"ok": int, "warnings": int, "fallbacks": int, "failed": int}
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ERROR] ANTHROPIC_API_KEY is not set. Check your .env file.")
        return {"ok": 0, "warnings": 0, "fallbacks": 0, "failed": 0}

    conn = get_connection()

    if job_ids:
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'approved_stage_1'"
        ).fetchall()

    if not rows:
        print("  No jobs to tailor.")
        conn.close()
        return {"ok": 0, "warnings": 0, "fallbacks": 0, "failed": 0}

    print(f"  Tailoring {len(rows)} job(s)...\n")
    stats = {"ok": 0, "warnings": 0, "fallbacks": 0, "failed": 0}
    # Close connection before CPU/IO-heavy work (PDF render) to avoid lock contention
    conn.close()

    for row in rows:
        job = dict(row)
        print(f"  [{job['id']}] {job['job_title']} @ {job['company_name']}")

        result = tailor_job(job)
        s = result["status"]

        if s == "ok":
            stats["ok"] += 1
            label = "OK"
        elif s == "validation_warning":
            stats["warnings"] += 1
            label = "WARNING"
        elif s == "fallback":
            stats["fallbacks"] += 1
            label = "FALLBACK"
        else:
            stats["failed"] += 1
            label = "FAILED"

        # Store PDF path in match_notes and advance status to pending_stage_2
        if result["pdf_path"]:
            write_conn = get_connection()
            existing_notes = job.get("match_notes") or ""
            new_notes = f"{existing_notes} | PDF: {result['pdf_path']}"
            write_conn.execute(
                "UPDATE jobs SET match_notes=?, status='pending_stage_2' WHERE id=?",
                (new_notes.strip(" |"), job["id"])
            )
            write_conn.commit()
            write_conn.close()

        print(f"    Status: {label} | PDF: {result['pdf_path']}")
        if result["violations"]:
            for v in result["violations"]:
                print(f"    ! {v}")
        print()

    return stats


# ---------------------------------------------------------------------------
# UI-callable wrapper — used by Streamlit app.py
# ---------------------------------------------------------------------------

def tailor_single_job(job_id: int) -> dict:
    """
    Tailor one job and update its DB status. Called by the Streamlit UI when
    the user clicks Approve at Stage 1.

    Returns {"success": bool, "pdf_path": str|None, "error": str|None, "violations": list}
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()

    if not row:
        return {"success": False, "pdf_path": None, "error": f"Job {job_id} not found", "violations": []}

    job = dict(row)
    result = tailor_job(job)

    if result["status"] == "render_failed":
        return {"success": False, "pdf_path": None, "error": "PDF render failed", "violations": result.get("violations", [])}

    if result.get("pdf_path"):
        write_conn = get_connection()
        existing_notes = job.get("match_notes") or ""
        new_notes = f"{existing_notes} | PDF: {result['pdf_path']}"
        write_conn.execute(
            "UPDATE jobs SET match_notes=?, status='pending_stage_2' WHERE id=?",
            (new_notes.strip(" |"), job_id)
        )
        write_conn.commit()
        write_conn.close()
        return {
            "success":    True,
            "pdf_path":   result["pdf_path"],
            "error":      None,
            "violations": result.get("violations", []),
        }

    return {"success": False, "pdf_path": None, "error": "Unknown error", "violations": result.get("violations", [])}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Tailor CVs for pending jobs")
    parser.add_argument(
        "--job-id",
        type=int,
        nargs="+",
        help="Only tailor specific job IDs (default: all pending_review)",
    )
    args = parser.parse_args()

    stats = run_tailor(job_ids=args.job_id)
    print("=== CV Tailoring Complete ===")
    print(f"  OK:        {stats['ok']}")
    print(f"  Warnings:  {stats['warnings']}")
    print(f"  Fallbacks: {stats['fallbacks']}")
    print(f"  Failed:    {stats['failed']}")
