"""
researcher.py — Phase 1.5: Company Research

For each job with status='approved_stage_1':
  1. Skip if company_dossier already populated (idempotent)
  2. Playwright fetches company "About" page (8s timeout, headless Chrome)
  3. If scrape fails/empty -> fall back to JD text only
  4. Claude Sonnet generates 2-3 sentence company dossier + industry sector
  5. Update job row: company_dossier + company_sector populated
  6. Status unchanged (stays approved_stage_1)

Hard rules enforced here:
  - NEVER scrape aggressively (1.5s delay between Playwright requests)
  - Always gracefully fall back to JD text if scraping fails
  - Never block pipeline on research failure

Usage:
    python3 -m modules.researcher                   # research all approved_stage_1 jobs
    python3 -m modules.researcher --job-id 4        # research a single job by DB id
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from database import (
    get_connection,
    calculate_api_cost,
    log_api_usage,
    check_budget_allows,
    BudgetExceededError,
)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# Web Scraping with Playwright (fallback gracefully)
# ---------------------------------------------------------------------------

def _scrape_company_about(company_name: str) -> str | None:
    """
    Attempt to scrape company's About page using Playwright.
    Returns cleaned text (up to 1500 chars) or None if scraping fails.
    """
    # Construct URL: company name -> lowercase, spaces to hyphens
    slug = re.sub(r'\s+', '-', company_name.lower())
    slug = re.sub(r'[^a-z0-9-]', '', slug)  # remove special chars
    url = f"https://www.{slug}.com/about"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=8000)  # 8 second timeout
            page.wait_for_load_state("networkidle", timeout=8000)

            # Extract visible text from body, strip noise
            body_text = page.evaluate("""
                () => {
                    const body = document.body;
                    // Remove script, style, nav, footer tags
                    ['script', 'style', 'nav', 'footer'].forEach(tag => {
                        body.querySelectorAll(tag).forEach(el => el.remove());
                    });
                    return body.innerText;
                }
            """)

            browser.close()

            # Clean and cap at 1500 chars
            if body_text and body_text.strip():
                text = body_text.strip()[:1500]
                return text if len(text) > 50 else None
            return None

    except (PWTimeout, Exception) as e:
        # Silently fall back to JD text
        return None


# ---------------------------------------------------------------------------
# Claude API call for company research
# ---------------------------------------------------------------------------

def _call_claude_for_research(scraped_text: str | None, jd_snippet: str, company_name: str) -> tuple[str | None, dict | None]:
    """
    Call Claude Sonnet to generate company dossier + sector.
    Returns (json_text, usage_dict) or (None, None) on failure.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = (
        "You are a company research analyst. Your task is to generate a brief company overview "
        "for a job application assistant. Return a JSON object with exactly these keys:\n"
        "{\n"
        '  "dossier": "<2–3 sentences describing the company, its mission, and relevance to sales/BD/marketing roles>",\n'
        '  "sector": "<1–3 word industry label, e.g. SaaS, Financial Services, E-commerce>"\n'
        "}\n"
        "Be concise, factual, and focused on what a job applicant would care about."
    )

    # Build user content: scraped text (if available) + JD snippet
    user_content_parts = []

    if scraped_text:
        user_content_parts.append(
            {"type": "text", "text": f"Company website 'About' page content:\n\n{scraped_text}"}
        )

    user_content_parts.append(
        {
            "type": "text",
            "text": f"Company name: {company_name}\n\nJob description excerpt:\n\n{jd_snippet}"
        }
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            temperature=0.5,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content_parts}],
        )

        text = message.content[0].text.strip()
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
            "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
        }
        return text, usage

    except Exception as e:
        return None, None


# ---------------------------------------------------------------------------
# Research a single job
# ---------------------------------------------------------------------------

def research_job(job: dict) -> dict:
    """
    Research company for a single job.
    Returns {"status": "ok"|"skipped"|"budget_exceeded"|"failed", ...}
    """
    # --- Skip if already researched ---
    if job.get("company_dossier"):
        return {
            "status": "skipped",
            "reason": "already_researched",
            "dossier": job.get("company_dossier"),
        }

    # --- Budget check ---
    try:
        check_budget_allows(0.02)  # Estimate ~$0.02 for company research
    except BudgetExceededError as e:
        return {
            "status": "budget_exceeded",
            "error": str(e),
        }

    # --- Playwright scrape (graceful fallback) ---
    scraped_text = None
    try:
        scraped_text = _scrape_company_about(job["company_name"])
        time.sleep(1.5)  # Rate limiting between requests
    except Exception as e:
        # Silently continue with JD-only fallback
        pass

    # --- Prepare JD snippet for Claude ---
    jd_snippet = (job.get("description_text") or "No job description available.")[:800]

    # --- Claude call with retry ---
    json_text = None
    usage_data = None

    for attempt in (1, 2):
        try:
            json_text, usage_data = _call_claude_for_research(scraped_text, jd_snippet, job["company_name"])
            if json_text:
                # Strip markdown code fences if Claude added them
                json_text = re.sub(r"^```(?:json)?\s*", "", json_text, flags=re.MULTILINE)
                json_text = re.sub(r"\s*```$", "", json_text, flags=re.MULTILINE)
                result = json.loads(json_text)
                break
            result = None
        except (json.JSONDecodeError, Exception) as e:
            result = None
            if attempt == 1:
                time.sleep(3)

    # --- Fallback if both attempts failed ---
    if result is None:
        dossier = "[Research unavailable]"
        sector = None
        status = "failed"
    else:
        dossier = result.get("dossier", "").strip()
        sector = result.get("sector", "").strip() or None

        # Log API usage
        if usage_data:
            cost = calculate_api_cost(
                input_tokens=usage_data["input_tokens"],
                output_tokens=usage_data["output_tokens"],
                cache_creation_tokens=usage_data["cache_creation_input_tokens"],
                cache_read_tokens=usage_data["cache_read_input_tokens"],
                model="claude-sonnet-4-20250514"
            )
            log_api_usage(
                job_id=job["id"],
                module="researcher",
                call_type="research",
                input_tokens=usage_data["input_tokens"],
                output_tokens=usage_data["output_tokens"],
                cost_usd=cost
            )

        status = "ok"

    # --- Write to DB ---
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET company_dossier=?, company_sector=? WHERE id=?",
        (dossier, sector, job["id"])
    )
    conn.commit()
    conn.close()

    return {
        "status": status,
        "dossier": dossier,
        "sector": sector,
    }


# ---------------------------------------------------------------------------
# Main batch function
# ---------------------------------------------------------------------------

def run_research(job_ids: list[int] | None = None) -> dict:
    """
    Research companies for all jobs with status='approved_stage_1', or a specific subset.
    Returns {"ok": int, "skipped": int, "failed": int, "budget_exceeded": int}
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ERROR] ANTHROPIC_API_KEY is not set. Check your .env file.")
        return {"ok": 0, "skipped": 0, "failed": 0, "budget_exceeded": 0}

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
        print("  No jobs to research.")
        conn.close()
        return {"ok": 0, "skipped": 0, "failed": 0, "budget_exceeded": 0}

    print(f"  Researching {len(rows)} job(s)...\n")
    stats = {"ok": 0, "skipped": 0, "failed": 0, "budget_exceeded": 0}
    conn.close()

    for row in rows:
        job = dict(row)
        print(f"  [{job['id']}] {job['company_name']}")

        result = research_job(job)
        status = result["status"]

        if status == "ok":
            stats["ok"] += 1
            print(f"      → OK: {result['dossier'][:60]}...")
        elif status == "skipped":
            stats["skipped"] += 1
            print(f"      → SKIPPED (already researched)")
        elif status == "budget_exceeded":
            stats["budget_exceeded"] += 1
            print(f"      → BUDGET EXCEEDED: {result['error']}")
        else:
            stats["failed"] += 1
            print(f"      → FAILED")

    return stats


# ---------------------------------------------------------------------------
# UI-callable wrapper
# ---------------------------------------------------------------------------

def research_single_job(job_id: int) -> dict:
    """
    Research a single job. Called from Streamlit UI.
    Returns {"success": bool, "error": str | None, ...result}
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "success": False,
            "error": "ANTHROPIC_API_KEY not set",
        }

    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    if not row:
        return {"success": False, "error": f"Job {job_id} not found"}

    job = dict(row)
    result = research_job(job)

    if result["status"] in ("ok", "skipped"):
        return {
            "success": True,
            "error": None,
            **result,
        }
    else:
        return {
            "success": False,
            "error": result.get("error") or f"Research failed: {result['status']}",
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research companies for approved_stage_1 jobs")
    parser.add_argument("--job-id", type=int, help="Research a single job by ID")
    args = parser.parse_args()

    if args.job_id:
        print(f"Researching job {args.job_id}...\n")
        result = research_single_job(args.job_id)
        print(f"\nResult: {json.dumps(result, indent=2)}")
    else:
        print("Starting company research...\n")
        stats = run_research()
        print(f"\nStats: OK={stats['ok']} Skipped={stats['skipped']} Failed={stats['failed']} BudgetExceeded={stats['budget_exceeded']}")
