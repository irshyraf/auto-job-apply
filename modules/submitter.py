"""
submitter.py — Phase 6: Application Submission via Playwright

Navigates to each approved job's application URL, fills every form field
using the answers stored in application_answers, and submits.

SAFETY RULES (hard, non-negotiable):
  1. --dry-run is the DEFAULT. Nothing is ever submitted without --submit flag.
  2. Any job with flagged=1 answers is skipped entirely. No exceptions.
  3. A screenshot is saved before the submit click for every application.
  4. Status is set to 'in_progress' at start and only 'submitted' on success.
     If anything fails, status reverts to 'approved' so it can be retried.

ATS support:
  Tier A (full support): Greenhouse, Lever
  Tier B (generic):      Any ATS — uses label-text matching to fill fields
  Special:               Indeed pages — clicks through to the underlying ATS

Usage:
    python3 -m modules.submitter --job-id 56 --dry-run    # default, safe
    python3 -m modules.submitter --job-id 56 --submit     # actually submits
    python3 -m modules.submitter --all --dry-run          # dry-run all approved
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from database import get_connection

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT   = Path(__file__).parent.parent
OUTPUT_DIR     = PROJECT_ROOT / "output"
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# ATS detection
# ---------------------------------------------------------------------------

ATS_PATTERNS = {
    "greenhouse":      [r"boards\.greenhouse\.io", r"app\.greenhouse\.io"],
    "lever":           [r"jobs\.lever\.co"],
    "workday":         [r"myworkdayjobs\.com", r"workday\.com/.*jobs"],
    "smartrecruiters": [r"jobs\.smartrecruiters\.com"],
    "bamboohr":        [r"bamboohr\.com/jobs"],
    "ashby":           [r"jobs\.ashbyhq\.com"],
    "teamtailor":      [r"\.teamtailor\.com"],
    "pinpoint":        [r"pinpointhq\.com"],
    "indeed":          [r"uk\.indeed\.com", r"indeed\.com/viewjob"],
    "reed":            [r"reed\.co\.uk/jobs"],
}


def detect_ats(url: str) -> str:
    for ats, patterns in ATS_PATTERNS.items():
        if any(re.search(p, url, re.IGNORECASE) for p in patterns):
            return ats
    return "generic"


# ---------------------------------------------------------------------------
# Label → answer matching
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _match_answer(label_text: str, answers: list[dict]) -> dict | None:
    """
    Find the best matching answer for a form field label.
    Returns the answer dict or None if no confident match.
    """
    norm_label = _normalise(label_text)

    # Exact match first
    for ans in answers:
        if _normalise(ans["field_name"]) == norm_label:
            return ans

    # Substring match — label contains our field name or vice versa
    for ans in answers:
        norm_field = _normalise(ans["field_name"])
        if norm_field in norm_label or norm_label in norm_field:
            return ans

    # Keyword overlap — share ≥2 meaningful words
    label_words = {w for w in norm_label.split() if len(w) > 3}
    best, best_overlap = None, 0
    for ans in answers:
        field_words = {w for w in _normalise(ans["field_name"]).split() if len(w) > 3}
        overlap = len(label_words & field_words)
        if overlap > best_overlap:
            best_overlap = overlap
            best = ans

    return best if best_overlap >= 2 else None


# ---------------------------------------------------------------------------
# CV and cover letter file paths
# ---------------------------------------------------------------------------

def _get_cv_pdf_path(job: dict) -> Path | None:
    notes = job.get("match_notes") or ""
    m = re.search(r"PDF:\s*(.+\.pdf)", notes)
    if m:
        p = Path(m.group(1).strip())
        return p if p.exists() else None
    return None


def _ensure_cover_letter(job_id: int, job: dict) -> str | None:
    """
    Return stored cover letter text, generating it first if not yet in DB.
    Only called when the form actually has a cover letter field.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT answer_text FROM application_answers WHERE job_id=? AND field_name='cover_letter'",
        (job_id,)
    ).fetchone()
    conn.close()

    if row and row["answer_text"]:
        return row["answer_text"]

    # Not yet generated — generate now
    print(f"  Generating cover letter for job {job_id}...")
    from modules.answer_gen import generate_cover_letter, _save_answer
    text = generate_cover_letter(job)
    if text:
        conn2 = get_connection()
        _save_answer(conn2, job_id, {
            "field_name":    "cover_letter",
            "field_type":    "cover_letter",
            "tier":          3,
            "answer_text":   text,
            "answer_source": "claude",
            "needs_review":  1,
            "flagged":       0,
        })
        conn2.commit()
        conn2.close()
    return text


def _get_cover_letter_pdf_path(job_id: int, job: dict | None = None) -> Path | None:
    """
    Return a PDF of the cover letter, generating the text first if needed.
    Pass job dict to enable on-the-fly generation.
    """
    if job is not None:
        text = _ensure_cover_letter(job_id, job)
    else:
        conn = get_connection()
        row = conn.execute(
            "SELECT answer_text FROM application_answers WHERE job_id=? AND field_name='cover_letter'",
            (job_id,)
        ).fetchone()
        conn.close()
        text = row["answer_text"] if (row and row["answer_text"]) else None

    if not text:
        return None

    text = row["answer_text"]
    out_path = OUTPUT_DIR / f"cover_letter_{job_id}.pdf"

    # Use Node + a minimal HTML template to render the cover letter as PDF
    # (reuses the same Playwright/Chromium infrastructure as the CV renderer)
    cl_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @page {{ size: A4; margin: 22mm; }}
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 10.5pt;
         line-height: 1.6; color: #374151; }}
  p {{ margin: 0 0 12pt; }}
</style></head><body>
{"".join(f"<p>{para.strip()}</p>" for para in text.split(chr(10)+chr(10)) if para.strip())}
</body></html>"""

    import tempfile, subprocess
    from pathlib import Path as P

    render_script = PROJECT_ROOT / "templates" / "render_cv.js"
    # We'll use a tiny inline renderer since render_cv.js expects JSON schema
    inline_script = f"""
const {{ chromium }} = require('playwright');
const fs = require('fs');
(async () => {{
  const browser = await chromium.launch({{ headless: true }});
  const page = await browser.newPage();
  await page.setContent({json.dumps(cl_html)}, {{ waitUntil: 'networkidle' }});
  await page.pdf({{ path: {json.dumps(str(out_path))}, format: 'A4',
    margin: {{ top:'22mm', right:'22mm', bottom:'22mm', left:'22mm' }},
    printBackground: true }});
  await browser.close();
  console.log('Cover letter PDF saved:', {json.dumps(str(out_path))});
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as tmp:
        tmp.write(inline_script)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["node", tmp_path],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT / "templates"),
        )
        if result.returncode == 0 and out_path.exists():
            return out_path
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return None


# ---------------------------------------------------------------------------
# Generic form filler — works across any ATS
# ---------------------------------------------------------------------------

async def _get_field_label(page: Page, element) -> str:
    """Extract the human-readable label for a form element."""
    # 1. aria-label attribute
    aria = await element.get_attribute("aria-label") or ""
    if aria.strip():
        return aria.strip()

    # 2. Associated <label> via id/for
    el_id = await element.get_attribute("id") or ""
    if el_id:
        try:
            label_el = page.locator(f'label[for="{el_id}"]')
            if await label_el.count() > 0:
                txt = await label_el.first.inner_text()
                if txt.strip():
                    return txt.strip()
        except Exception:
            pass

    # 3. placeholder
    placeholder = await element.get_attribute("placeholder") or ""
    if placeholder.strip():
        return placeholder.strip()

    # 4. name attribute
    name = await element.get_attribute("name") or ""
    if name.strip():
        return name.replace("_", " ").replace("-", " ").strip()

    # 5. Surrounding text (parent element text minus child input text)
    try:
        parent = await element.evaluate_handle("el => el.closest('div,li,p,fieldset')")
        parent_text = await page.evaluate("el => el ? el.innerText : ''", parent)
        if parent_text.strip():
            return parent_text.strip()[:80]
    except Exception:
        pass

    return ""


async def _fill_text(page: Page, element, value: str) -> None:
    await element.click()
    await element.fill("")
    await element.type(value, delay=30)


async def _fill_select(page: Page, element, value: str) -> bool:
    """Try to select the option that best matches value. Returns True on success."""
    options = await element.evaluate("""
        el => Array.from(el.options).map(o => ({value: o.value, text: o.text.trim()}))
    """)
    norm_value = _normalise(value)

    # Exact text match
    for opt in options:
        if _normalise(opt["text"]) == norm_value:
            await element.select_option(value=opt["value"])
            return True

    # Substring match
    for opt in options:
        if norm_value in _normalise(opt["text"]) or _normalise(opt["text"]) in norm_value:
            await element.select_option(value=opt["value"])
            return True

    # Yes/No mapping
    yn_map = {"yes": ["yes", "true", "1"], "no": ["no", "false", "0"]}
    for yn, variants in yn_map.items():
        if norm_value.startswith(yn):
            for opt in options:
                if _normalise(opt["text"]) in variants:
                    await element.select_option(value=opt["value"])
                    return True

    return False


async def _fill_radio(page: Page, element, value: str) -> bool:
    """Click the radio button whose label best matches value."""
    norm_value = _normalise(value)
    name = await element.get_attribute("name") or ""
    if not name:
        return False

    radios = page.locator(f'input[type="radio"][name="{name}"]')
    count = await radios.count()
    for i in range(count):
        radio = radios.nth(i)
        label = await _get_field_label(page, radio)
        if _normalise(label) and (norm_value in _normalise(label) or _normalise(label) in norm_value):
            await radio.click()
            return True
    return False


async def generic_fill_form(
    page: Page,
    answers: list[dict],
    job: dict,
    dry_run: bool = True,
    log: list | None = None,
) -> dict:
    """
    Generic form filler. Scans all visible inputs and matches against answers.
    Returns {"filled": int, "skipped": int, "file_uploads": int, "unmatched": list}
    """
    if log is None:
        log = []

    stats = {"filled": 0, "skipped": 0, "file_uploads": 0, "unmatched": []}
    answers_by_name = {a["field_name"]: a for a in answers if not a["flagged"]}
    cv_pdf = _get_cv_pdf_path(job)
    # Cover letter PDF is generated lazily — only when form has a cover letter field
    cl_pdf = None

    # Gather all form inputs
    inputs     = await page.locator("input:visible, textarea:visible").all()
    selects    = await page.locator("select:visible").all()
    file_inputs = await page.locator("input[type='file']").all()

    # --- Text inputs and textareas ---
    for el in inputs:
        input_type = (await el.get_attribute("type") or "text").lower()
        if input_type in ("submit", "button", "hidden", "file", "checkbox", "radio"):
            continue

        label = await _get_field_label(page, el)
        if not label:
            continue

        answer = _match_answer(label, answers)
        if not answer:
            # Generate on-the-fly for this specific field
            from modules.answer_gen import classify_and_answer, _save_answer
            generated = classify_and_answer(label, input_type, job, char_limit=None)
            if generated and generated.get("answer_text"):
                conn_g = get_connection()
                _save_answer(conn_g, job["id"], generated)
                conn_g.commit()
                conn_g.close()
                answers.append(generated)
                answer = generated
                log.append(f"  GEN   [{generated['tier']}] {label[:50]}")
            else:
                stats["unmatched"].append(label[:60])
                continue

        log.append(f"  FILL  [{answer['tier']}] {label[:50]} → {(answer['answer_text'] or '')[:60]}")
        if not dry_run:
            try:
                await _fill_text(page, el, answer["answer_text"] or "")
                await asyncio.sleep(0.4)
                stats["filled"] += 1
            except Exception as e:
                log.append(f"  WARN  Could not fill '{label[:40]}': {e}")
                stats["skipped"] += 1
        else:
            stats["filled"] += 1

    # --- Textareas (may not be caught above on some pages) ---
    textareas = await page.locator("textarea:visible").all()
    for el in textareas:
        label = await _get_field_label(page, el)
        if not label:
            continue
        answer = _match_answer(label, answers)
        if not answer:
            from modules.answer_gen import classify_and_answer, _save_answer
            generated = classify_and_answer(label, "textarea", job, char_limit=None)
            if generated and generated.get("answer_text"):
                conn_g = get_connection()
                _save_answer(conn_g, job["id"], generated)
                conn_g.commit()
                conn_g.close()
                answers.append(generated)
                answer = generated
                log.append(f"  GEN   [{generated['tier']}] {label[:50]}")
            else:
                continue
        log.append(f"  FILL  [{answer['tier']}] {label[:50]} → {(answer['answer_text'] or '')[:60]}")
        if not dry_run:
            try:
                await _fill_text(page, el, answer["answer_text"] or "")
                await asyncio.sleep(0.4)
                stats["filled"] += 1
            except Exception as e:
                stats["skipped"] += 1

    # --- Select dropdowns ---
    for el in selects:
        label = await _get_field_label(page, el)
        if not label:
            continue
        answer = _match_answer(label, answers)
        if not answer:
            stats["unmatched"].append(f"[dropdown] {label[:50]}")
            continue
        log.append(f"  FILL  [select] {label[:50]} → {(answer['answer_text'] or '')[:40]}")
        if not dry_run:
            matched = await _fill_select(page, el, answer["answer_text"] or "")
            if matched:
                stats["filled"] += 1
            else:
                log.append(f"  WARN  No matching option for '{label[:40]}' — value: {answer['answer_text']}")
                stats["skipped"] += 1
        else:
            stats["filled"] += 1

    # --- Radio buttons ---
    radio_inputs = await page.locator("input[type='radio']:visible").all()
    seen_names = set()
    for el in radio_inputs:
        name = await el.get_attribute("name") or ""
        if name in seen_names:
            continue
        seen_names.add(name)
        label = await _get_field_label(page, el)
        if not label:
            continue
        answer = _match_answer(label, answers)
        if not answer:
            continue
        log.append(f"  FILL  [radio] {label[:50]} → {answer['answer_text']}")
        if not dry_run:
            await _fill_radio(page, el, answer["answer_text"] or "")
            stats["filled"] += 1
        else:
            stats["filled"] += 1

    # --- File uploads ---
    for el in file_inputs:
        label = await _get_field_label(page, el)
        label_lower = label.lower()

        # Detect CV vs cover letter upload by label text
        if any(kw in label_lower for kw in ["cv", "resume", "curriculum"]):
            if cv_pdf:
                log.append(f"  UPLOAD  CV → {cv_pdf.name}")
                if not dry_run:
                    await el.set_input_files(str(cv_pdf))
                    stats["file_uploads"] += 1
            else:
                log.append(f"  WARN  CV upload requested but no PDF found")
                stats["skipped"] += 1

        elif any(kw in label_lower for kw in ["cover", "letter", "covering"]):
            # Generate cover letter on-the-fly if not yet done
            if cl_pdf is None:
                cl_pdf = _get_cover_letter_pdf_path(job["id"], job)
            if cl_pdf:
                log.append(f"  UPLOAD  Cover letter → {cl_pdf.name}")
                if not dry_run:
                    await el.set_input_files(str(cl_pdf))
                    stats["file_uploads"] += 1
            else:
                log.append(f"  WARN  Cover letter generation failed")
                stats["skipped"] += 1

    return stats


# ---------------------------------------------------------------------------
# Indeed click-through handler
# ---------------------------------------------------------------------------

async def _clickthrough_indeed(page: Page) -> bool:
    """
    On an Indeed job page, find and click the Apply button.
    Returns True if successfully navigated away from Indeed.
    """
    apply_selectors = [
        'a[data-jk]:has-text("Apply")',
        'a.jobsearch-IndeedApplyButton-newDesign',
        'button:has-text("Apply now")',
        'a:has-text("Apply on company site")',
        'a:has-text("Apply")',
        '#applyButtonLinkContainer a',
    ]
    for sel in apply_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Greenhouse-specific handler
# ---------------------------------------------------------------------------

async def fill_greenhouse(page: Page, answers: list[dict], job: dict, dry_run: bool, log: list) -> dict:
    """
    Greenhouse boards have a predictable structure.
    Fields: #first_name, #last_name, #email, #phone, #resume (file), #cover_letter (file),
    and custom questions in #custom_fields > .field
    """
    # Wait for the application form
    try:
        await page.wait_for_selector("#application_form, form#application", timeout=8000)
    except PWTimeout:
        log.append("  WARN  Greenhouse form not found — falling back to generic filler")
        return await generic_fill_form(page, answers, job, dry_run, log)

    # Simple field map: selector → our field_name key
    GH_FIELDS = {
        "#first_name":   "First name",
        "#last_name":    "Last name / Surname",
        "#email":        "Email address",
        "#phone":        "Phone number",
        "#cover_letter_text": "cover_letter",
    }
    stats = {"filled": 0, "skipped": 0, "file_uploads": 0, "unmatched": []}
    answer_map = {_normalise(a["field_name"]): a for a in answers}

    for selector, field_name in GH_FIELDS.items():
        el = page.locator(selector)
        if await el.count() == 0:
            continue
        answer = answer_map.get(_normalise(field_name))
        if not answer:
            continue
        log.append(f"  FILL  [gh] {field_name} → {(answer['answer_text'] or '')[:60]}")
        if not dry_run:
            await _fill_text(page, el.first, answer["answer_text"] or "")
            stats["filled"] += 1
        else:
            stats["filled"] += 1

    # File uploads
    cv_pdf = _get_cv_pdf_path(job)
    resume_input = page.locator("input#resume[type='file'], input[name='resume'][type='file']")
    if cv_pdf and await resume_input.count() > 0:
        log.append(f"  UPLOAD  [gh] CV → {cv_pdf.name}")
        if not dry_run:
            await resume_input.first.set_input_files(str(cv_pdf))
            stats["file_uploads"] += 1

    # Any remaining custom fields — fall back to generic
    extra = await generic_fill_form(page, answers, job, dry_run, log)
    stats["filled"]       += extra["filled"]
    stats["skipped"]      += extra["skipped"]
    stats["file_uploads"] += extra["file_uploads"]
    return stats


# ---------------------------------------------------------------------------
# Lever-specific handler
# ---------------------------------------------------------------------------

async def fill_lever(page: Page, answers: list[dict], job: dict, dry_run: bool, log: list) -> dict:
    """
    Lever has a clean form structure: .application-form with labelled inputs.
    Falls through to generic filler since Lever forms are well-labelled.
    """
    try:
        await page.wait_for_selector(".application-form, form.application", timeout=8000)
    except PWTimeout:
        log.append("  WARN  Lever form not found — falling back to generic filler")

    return await generic_fill_form(page, answers, job, dry_run, log)


# ---------------------------------------------------------------------------
# Core submit function for one job
# ---------------------------------------------------------------------------

async def submit_job(job: dict, answers: list[dict], dry_run: bool = True) -> dict:
    """
    Navigate to the job URL, fill the form, and submit (or dry-run).
    Returns {"status": "submitted"|"dry_run_ok"|"failed"|"blocked",
             "log": [...], "screenshot": str|None, "ref": str|None}
    """
    job_id   = job["id"]
    url      = job["source_url"]
    ats      = detect_ats(url)
    log_lines = []
    screenshot_path = None

    log_lines.append(f"  Job: {job['job_title']} @ {job['company_name']}")
    log_lines.append(f"  URL: {url}")
    log_lines.append(f"  ATS: {ats} | Dry run: {dry_run}")

    # Safety: abort if any flagged answers
    flagged = [a for a in answers if a.get("flagged")]
    if flagged:
        msg = f"  BLOCKED: {len(flagged)} flagged answer(s) — fill manually before submitting"
        log_lines.append(msg)
        return {"status": "blocked", "log": log_lines, "screenshot": None, "ref": None}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        try:
            log_lines.append(f"  Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            # If Indeed page, click through to the actual application
            if ats == "indeed":
                log_lines.append("  Clicking through Indeed apply button...")
                clicked = await _clickthrough_indeed(page)
                if clicked:
                    await asyncio.sleep(2)
                    ats = detect_ats(page.url)
                    log_lines.append(f"  Landed on: {page.url[:80]} (ATS: {ats})")
                else:
                    log_lines.append("  WARN  Could not find Apply button on Indeed page")

            # Dispatch to platform handler
            HANDLERS = {
                "greenhouse": fill_greenhouse,
                "lever":      fill_lever,
            }
            handler = HANDLERS.get(ats, generic_fill_form)

            if handler == generic_fill_form:
                fill_stats = await generic_fill_form(page, answers, job, dry_run, log_lines)
            else:
                fill_stats = await handler(page, answers, job, dry_run, log_lines)

            log_lines.append(
                f"  Fields: {fill_stats['filled']} filled, "
                f"{fill_stats['skipped']} skipped, "
                f"{fill_stats['file_uploads']} uploaded"
            )
            if fill_stats["unmatched"]:
                log_lines.append(f"  Unmatched fields: {fill_stats['unmatched'][:5]}")

            # Screenshot before submit
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_company = re.sub(r"[^\w]", "_", job["company_name"])[:30]
            shot_path = SCREENSHOTS_DIR / f"{safe_company}_{job_id}_{ts}.png"
            await page.screenshot(path=str(shot_path), full_page=True)
            screenshot_path = str(shot_path)
            log_lines.append(f"  Screenshot: {shot_path.name}")

            if dry_run:
                log_lines.append("  DRY RUN — form filled but NOT submitted.")
                await browser.close()
                return {"status": "dry_run_ok", "log": log_lines,
                        "screenshot": screenshot_path, "ref": None}

            # --- Submit ---
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Submit application")',
                'button:has-text("Submit")',
                'button:has-text("Apply")',
                'button:has-text("Send application")',
            ]
            submitted = False
            for sel in submit_selectors:
                btn = page.locator(sel).last
                if await btn.count() > 0:
                    log_lines.append(f"  Clicking submit: {sel}")
                    await btn.click()
                    await asyncio.sleep(3)
                    submitted = True
                    break

            if not submitted:
                log_lines.append("  WARN  Submit button not found — manual submission required")
                await browser.close()
                return {"status": "failed", "log": log_lines,
                        "screenshot": screenshot_path, "ref": None}

            # Wait for confirmation
            await page.wait_for_load_state("networkidle", timeout=10000)

            # Try to extract application reference
            ref = None
            ref_patterns = [
                r"application\s+(?:ref|reference|id|number)[:\s#]*([A-Z0-9\-]+)",
                r"reference[:\s#]*([A-Z0-9\-]+)",
                r"#([A-Z0-9]{6,})",
            ]
            page_text = await page.inner_text("body")
            for pattern in ref_patterns:
                m = re.search(pattern, page_text, re.IGNORECASE)
                if m:
                    ref = m.group(1)
                    log_lines.append(f"  Application reference: {ref}")
                    break

            # Post-submit screenshot
            shot_path2 = SCREENSHOTS_DIR / f"{safe_company}_{job_id}_{ts}_confirmation.png"
            await page.screenshot(path=str(shot_path2), full_page=True)
            log_lines.append(f"  Confirmation screenshot: {shot_path2.name}")

            await browser.close()
            return {"status": "submitted", "log": log_lines,
                    "screenshot": screenshot_path, "ref": ref}

        except Exception as e:
            log_lines.append(f"  ERROR: {e}")
            try:
                await browser.close()
            except Exception:
                pass
            return {"status": "failed", "log": log_lines,
                    "screenshot": screenshot_path, "ref": None}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_approved_jobs(job_ids: list[int] | None = None) -> list[dict]:
    conn = get_connection()
    if job_ids:
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE id IN ({placeholders}) AND status='approved'",
            job_ids,
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs WHERE status='approved'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_answers(job_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM application_answers WHERE job_id=?", (job_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _mark_in_progress(job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='in_progress' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def _mark_submitted(job_id: int, ref: str | None) -> None:
    conn = get_connection()
    conn.execute("""
        UPDATE jobs SET status='submitted', submitted_at=?, application_ref=?
        WHERE id=?
    """, (datetime.now().isoformat(), ref, job_id))
    conn.commit()
    conn.close()


def _mark_failed(job_id: int) -> None:
    """Revert to 'approved' so it can be retried."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

_RATE_LIMIT_SECONDS = {
    "linkedin":  5,
    "indeed":    2,
    "reed":      2,
    "glassdoor": 3,
    "google":    3,
    "default":   2,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_submit(job_ids: list[int] | None = None, dry_run: bool = True) -> dict:
    """
    Submit all approved jobs (or a specific subset).
    dry_run=True by default — pass dry_run=False to actually submit.

    Returns {"submitted": int, "dry_run_ok": int, "failed": int, "blocked": int}
    """
    jobs = _load_approved_jobs(job_ids)

    if not jobs:
        print("  No approved jobs to submit.")
        return {"submitted": 0, "dry_run_ok": 0, "failed": 0, "blocked": 0}

    mode_label = "DRY RUN" if dry_run else "LIVE SUBMIT"
    print(f"\n  [{mode_label}] Processing {len(jobs)} approved job(s)...\n")

    stats = {"submitted": 0, "dry_run_ok": 0, "failed": 0, "blocked": 0}

    for job in jobs:
        job_id  = job["id"]
        answers = _load_answers(job_id)

        print(f"  [{job_id}] {job['job_title']} @ {job['company_name']}")

        if not dry_run:
            _mark_in_progress(job_id)

        # Run async submission
        result = asyncio.run(submit_job(job, answers, dry_run=dry_run))

        # Print log
        for line in result["log"]:
            print(line)

        status = result["status"]
        stats[status] = stats.get(status, 0) + 1

        if status == "submitted":
            _mark_submitted(job_id, result["ref"])
            print(f"  ✅ Submitted — ref: {result['ref'] or 'none captured'}\n")
        elif status == "dry_run_ok":
            print(f"  ✓ Dry run complete — screenshot: {Path(result['screenshot']).name if result['screenshot'] else 'none'}\n")
        elif status == "blocked":
            if not dry_run:
                _mark_failed(job_id)
            print()
        else:
            if not dry_run:
                _mark_failed(job_id)
            print(f"  ✗ Failed — reverted to approved for retry\n")

        # Rate limit between applications
        board = job.get("source_board", "default")
        delay = _RATE_LIMIT_SECONDS.get(board, _RATE_LIMIT_SECONDS["default"])
        if len(jobs) > 1:
            time.sleep(delay)

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Submit approved job applications")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-id", type=int, nargs="+", help="Submit specific job ID(s)")
    group.add_argument("--all", action="store_true", help="Submit all approved jobs")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Fill form but DO NOT click Submit (default — safe)",
    )
    mode.add_argument(
        "--submit", action="store_true", default=False,
        help="Actually click Submit. Use with care.",
    )

    args = parser.parse_args()
    is_dry_run = not args.submit

    job_ids = args.job_id if not args.all else None

    if not is_dry_run:
        print("\n⚠  LIVE SUBMIT MODE — applications will be submitted for real.")
        confirm = input("  Type YES to confirm: ").strip()
        if confirm != "YES":
            print("  Aborted.")
            sys.exit(0)

    results = run_submit(job_ids=job_ids, dry_run=is_dry_run)

    print("=== Submission Complete ===")
    if is_dry_run:
        print(f"  Dry run OK:  {results.get('dry_run_ok', 0)}")
    else:
        print(f"  Submitted:   {results.get('submitted', 0)}")
    print(f"  Blocked:     {results.get('blocked', 0)}")
    print(f"  Failed:      {results.get('failed', 0)}")
