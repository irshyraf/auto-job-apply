"""
answer_gen.py — Phase 4: Question Classification and Answer Generation

Classifies every form field for a job application and generates answers.

Classification pipeline (runs in order, stops at first match):
  Step 1 — Tier 1 special rules   : visa expiry → FLAG; criminal/disability/equality → auto-fill
  Step 2 — Tier 1 factual         : name, email, phone, right to work, salary, etc. → vault
  Step 3 — Tier 2 common          : reason for looking, availability, how did you hear → vault
  Step 4 — Tier 4 competency      : "tell me about a time..." → best STAR story from answer bank
  Step 5 — Tier 3 role-specific   : fallback → AI-generated using JD + company dossier

Also generates cover letters on request.
Stores every answer in the application_answers table.

Usage:
    python3 -m modules.answer_gen --job-id 56            # demo with simulated fields
    python3 -m modules.answer_gen --job-id 56 --cover-letter
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config_loader import answer_bank, personal_data, question_classification_rules, tone_voice
from database import get_connection

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import anthropic

# ---------------------------------------------------------------------------
# Helpers — keyword matching
# ---------------------------------------------------------------------------

def _matches_any(text: str, keywords: list[str]) -> bool:
    """Return True if any keyword appears (case-insensitive) in text."""
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)


def _matches_tier4_trigger(text: str) -> bool:
    rules = question_classification_rules()
    triggers = rules["tier_4_rules"]["trigger_patterns"]
    return _matches_any(text, triggers)


def _matches_tier3_trigger(text: str) -> bool:
    rules = question_classification_rules()
    triggers = rules["tier_3_rules"]["trigger_patterns"]
    return _matches_any(text, triggers)


# ---------------------------------------------------------------------------
# Tier 1 special rules
# ---------------------------------------------------------------------------

def _classify_special(label: str) -> dict | None:
    """
    Check Tier 1 special rules first.
    Returns an answer dict or None if no special rule matched.
    """
    rules = question_classification_rules()["tier_1_rules"]["special_rules"]
    lab = label.lower()

    for rule in rules:
        if _matches_any(lab, rule["keywords"]):
            if rule["action"] == "FLAG_FOR_MANUAL_REVIEW":
                return {
                    "tier":         1,
                    "answer_text":  "MANUAL REVIEW REQUIRED — do not auto-fill",
                    "answer_source": "flagged",
                    "needs_review": 1,
                    "flagged":      1,
                }
            else:  # AUTO_FILL
                return {
                    "tier":         1,
                    "answer_text":  rule["answer"],
                    "answer_source": "auto_vault",
                    "needs_review": 0,
                    "flagged":      0,
                }
    return None


# ---------------------------------------------------------------------------
# Tier 1 factual
# ---------------------------------------------------------------------------

def _classify_tier1(label: str, source_board: str) -> dict | None:
    rules = question_classification_rules()["tier_1_rules"]["patterns"]
    lab = label.lower()

    for rule in rules:
        if _matches_any(lab, rule["keywords"]):
            return {
                "tier":         1,
                "answer_text":  rule["answer"],
                "answer_source": "auto_vault",
                "needs_review": 0,
                "flagged":      0,
            }
    return None


# ---------------------------------------------------------------------------
# Tier 2 common screening
# ---------------------------------------------------------------------------

def _classify_tier2(label: str, source_board: str) -> dict | None:
    rules = question_classification_rules()["tier_2_rules"]["patterns"]
    lab = label.lower()

    board_display = {
        "linkedin": "LinkedIn", "indeed": "Indeed UK",
        "reed": "Reed", "glassdoor": "Glassdoor",
        "google": "Google Jobs", "totaljobs": "Totaljobs", "cwjobs": "CWJobs",
    }.get(source_board.lower(), source_board.title())

    for rule in rules:
        if _matches_any(lab, rule["keywords"]):
            answer = rule["answer"]
            # Dynamic: replace [job board name] with actual board
            answer = answer.replace("[job board name]", board_display)
            return {
                "tier":         2,
                "answer_text":  answer,
                "answer_source": "auto_vault",
                "needs_review": 0,
                "flagged":      0,
            }
    return None


# ---------------------------------------------------------------------------
# Tier 4 — select best STAR story + adapt via Claude
# ---------------------------------------------------------------------------

def _select_story(question_label: str) -> dict:
    """
    Pick the best STAR story from answer_bank.json for this question.
    Strategy: count tag overlaps, prefer Caring Hearts > Welstand > Far Out if tied.
    """
    bank = answer_bank()
    q = question_label.lower()

    ROLE_PRIORITY = {
        "Caring Hearts": 0,
        "Welstand":      1,
        "Far Out":       2,
        "Cross-cutting": 3,
    }

    best_story  = bank[0]
    best_score  = -1

    for story in bank:
        tag_score = sum(1 for tag in story["competency_tags"] if tag.lower() in q)
        # Also check question_match field for semantic similarity
        qm = story.get("question_match", "").lower()
        qm_words = [w for w in qm.split() if len(w) > 4]
        qm_score = sum(1 for w in qm_words if w in q)
        total = tag_score * 2 + qm_score

        # Role priority tiebreaker
        role_prio = next(
            (v for k, v in ROLE_PRIORITY.items() if k in story.get("source_role", "")), 99
        )

        if total > best_score or (total == best_score and role_prio < ROLE_PRIORITY.get(
            next((k for k in ROLE_PRIORITY if k in (best_story.get("source_role", ""))), "Cross-cutting"), 99
        )):
            best_score = total
            best_story = story

    return best_story


def _generate_tier4(question_label: str, story: dict, job: dict) -> str:
    """Call Claude to adapt the chosen STAR story to this specific question."""
    cfg = question_classification_rules()["tier_4_rules"]["answer_generation"]
    tv  = tone_voice()

    system_prompt = cfg["system_prompt"]
    user_prompt = f"""## QUESTION
{question_label}

## SELECTED STAR STORY (ID: {story['story_id']} — {story['title']})

Situation: {story['situation']}
Task: {story['task']}
Action: {story['action']}
Result: {story['result']}

## JOB CONTEXT
Title: {job.get('job_title', '')}
Company: {job.get('company_name', '')}
JD excerpt (first 800 chars): {(job.get('description_text') or '')[:800]}

## TONE GUIDE (key rules)
{json.dumps(tv.get('never_do', []), indent=2)}

Write a 150-250 word answer using this story. Natural STAR structure — no mechanical headers.
Keep Aafreen's warm, grounded voice. Never fabricate. Use the story as written."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        temperature=cfg["temperature"],
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------------------
# Tier 3 — role-specific AI answer
# ---------------------------------------------------------------------------

def _generate_tier3(question_label: str, job: dict, char_limit: int | None = None) -> str:
    """Call Claude to generate a role-specific answer."""
    cfg = question_classification_rules()["tier_3_rules"]["answer_generation"]
    tv  = tone_voice()
    vault = personal_data()

    word_guidance = cfg["word_count_short_field"] if (char_limit and char_limit < 500) else cfg["word_count_default"]

    # Build a compact CV summary for context
    wh = vault["work_history"]
    cv_summary = "\n".join([
        f"- {r['job_title']} @ {r['company']}, {r['dates']}: {'; '.join(r['key_achievements'][:3])}"
        for r in wh
    ])

    user_prompt = f"""## QUESTION
{question_label}

## JOB
Title: {job.get('job_title', '')}
Company: {job.get('company_name', '')}
Location: {job.get('location', '')}
JD (first 1000 chars): {(job.get('description_text') or '')[:1000]}

## COMPANY DOSSIER
{job.get('company_dossier') or 'Not available.'}

## CANDIDATE CV SUMMARY
{cv_summary}

## TONE RULES (never do)
{json.dumps(tv.get('never_do', []), indent=2)}

Write a {word_guidance} answer. Warm, specific, genuine. Not template-sounding."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        temperature=cfg["temperature"],
        system=cfg["system_prompt"],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------------------
# Cover letter generation
# ---------------------------------------------------------------------------

def generate_cover_letter(job: dict) -> str:
    """Generate a full cover letter for this job using Claude."""
    cfg  = question_classification_rules()["cover_letter_rules"]["generation_config"]
    tv   = tone_voice()
    vault = personal_data()

    wh = vault["work_history"]
    cv_summary = "\n".join([
        f"- {r['job_title']} @ {r['company']}, {r['dates']}: {'; '.join(r['key_achievements'][:4])}"
        for r in wh
    ])

    cl_rules = tv.get("cover_letter_rules", {})

    user_prompt = f"""## JOB
Title: {job.get('job_title', '')}
Company: {job.get('company_name', '')}
Location: {job.get('location', '')}
JD: {(job.get('description_text') or '')[:1500]}

## COMPANY DOSSIER
{job.get('company_dossier') or 'Not available.'}

## CANDIDATE CV
{cv_summary}

## COVER LETTER RULES
{json.dumps(cl_rules, indent=2)}

## TONE GUIDE (never do)
{json.dumps(tv.get('never_do', []), indent=2)}

Write the cover letter now. Under 300 words. 3-4 paragraphs."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        temperature=cfg["temperature"],
        system=cfg["system_prompt"],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------------------
# Main classification function
# ---------------------------------------------------------------------------

def classify_and_answer(
    label: str,
    field_type: str,
    job: dict,
    char_limit: int | None = None,
) -> dict:
    """
    Run the full 5-step pipeline for a single form field.
    Returns a dict ready to INSERT into application_answers.
    """
    source_board = job.get("source_board", "")

    # Step 1 — Special rules (visa expiry, criminal, disability, equality)
    result = _classify_special(label)
    if result:
        return {**result, "field_name": label, "field_type": field_type,
                "story_id": None, "competency_tags": None}

    # Step 2 — Tier 1 factual
    result = _classify_tier1(label, source_board)
    if result:
        return {**result, "field_name": label, "field_type": field_type,
                "story_id": None, "competency_tags": None}

    # Step 3 — Tier 2 common screening
    result = _classify_tier2(label, source_board)
    if result:
        return {**result, "field_name": label, "field_type": field_type,
                "story_id": None, "competency_tags": None}

    # Step 4 — Tier 4 competency/STAR
    if _matches_tier4_trigger(label):
        story = _select_story(label)
        answer_text = _generate_tier4(label, story, job)
        return {
            "field_name":      label,
            "field_type":      field_type,
            "tier":            4,
            "answer_text":     answer_text,
            "answer_source":   "ai_generated",
            "story_id":        story["story_id"],
            "competency_tags": json.dumps(story["competency_tags"]),
            "needs_review":    1,
            "flagged":         0,
        }

    # Step 5 — Tier 3 fallback (role-specific AI)
    answer_text = _generate_tier3(label, job, char_limit)
    return {
        "field_name":      label,
        "field_type":      field_type,
        "tier":            3,
        "answer_text":     answer_text,
        "answer_source":   "ai_generated",
        "story_id":        None,
        "competency_tags": None,
        "needs_review":    1,
        "flagged":         0,
    }


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def _save_answer(conn, job_id: int, answer: dict) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO application_answers
            (job_id, field_name, field_type, tier, competency_tags,
             answer_text, answer_source, story_id,
             needs_review, flagged, updated_at)
        VALUES
            (:job_id, :field_name, :field_type, :tier, :competency_tags,
             :answer_text, :answer_source, :story_id,
             :needs_review, :flagged, datetime('now'))
    """, {**answer, "job_id": job_id})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_answer_gen(
    job_id: int,
    fields: list[dict],
    want_cover_letter: bool = False,
) -> dict:
    """
    Generate and store answers for all provided form fields.

    Args:
        job_id:            DB id of the job.
        fields:            List of {"label": str, "field_type": str, "char_limit": int|None}
        want_cover_letter: If True, also generate a cover letter.

    Returns:
        {"t1": int, "t2": int, "t3": int, "t4": int, "flagged": int, "cover_letter": bool}
    """
    conn = get_connection()
    job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        print(f"  Job {job_id} not found.")
        conn.close()
        return {}

    job = dict(job_row)
    stats = {"t1": 0, "t2": 0, "t3": 0, "t4": 0, "flagged": 0, "cover_letter": False}

    print(f"  Generating answers for: {job['job_title']} @ {job['company_name']}")
    print(f"  {len(fields)} field(s) to classify\n")

    for field in fields:
        label      = field["label"]
        field_type = field.get("field_type", "text_long")
        char_limit = field.get("char_limit")

        answer = classify_and_answer(label, field_type, job, char_limit)
        _save_answer(conn, job_id, answer)

        tier = answer["tier"]
        stats[f"t{tier}"] += 1
        if answer.get("flagged"):
            stats["flagged"] += 1

        flag_str  = " ⚑ FLAGGED" if answer.get("flagged") else ""
        review_str = " [needs review]" if answer.get("needs_review") else ""
        story_str  = f"  story={answer['story_id']}" if answer.get("story_id") else ""
        print(f"  T{tier}{flag_str}{review_str}  {label[:60]}{story_str}")
        if tier in (3, 4):
            preview = (answer["answer_text"] or "")[:120].replace("\n", " ")
            print(f"         → {preview}...")
        print()

    # Cover letter
    if want_cover_letter:
        print("  Generating cover letter...")
        cl_text = generate_cover_letter(job)
        _save_answer(conn, job_id, {
            "field_name":      "cover_letter",
            "field_type":      "cover_letter",
            "tier":            3,
            "answer_text":     cl_text,
            "answer_source":   "ai_generated",
            "story_id":        None,
            "competency_tags": None,
            "needs_review":    1,
            "flagged":         0,
        })
        stats["cover_letter"] = True
        print(f"  Cover letter generated ({len(cl_text.split())} words)\n")

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# CLI — demo with a realistic field set for the given job
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Generate answers for a job application")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--cover-letter", action="store_true")
    args = parser.parse_args()

    # Realistic demo field set covering all tiers
    demo_fields = [
        # Tier 1 factual
        {"label": "First name",                                    "field_type": "text_short"},
        {"label": "Last name / Surname",                           "field_type": "text_short"},
        {"label": "Email address",                                 "field_type": "text_short"},
        {"label": "Phone number",                                  "field_type": "text_short"},
        {"label": "Do you have the right to work in the UK?",      "field_type": "radio"},
        {"label": "Do you require visa sponsorship?",              "field_type": "radio"},
        {"label": "What is your salary expectation?",              "field_type": "text_short"},
        {"label": "What is your notice period?",                   "field_type": "text_short"},
        # Tier 1 special
        {"label": "Please state your visa expiry date",            "field_type": "text_short"},
        {"label": "Do you have a criminal record or unspent convictions?", "field_type": "radio"},
        {"label": "Do you consider yourself to have a disability?","field_type": "radio"},
        {"label": "What is your gender?",                          "field_type": "dropdown"},
        # Tier 2
        {"label": "How did you hear about this role?",             "field_type": "dropdown"},
        {"label": "Why are you looking for a new role?",           "field_type": "text_long"},
        # Tier 4 competency
        {"label": "Tell me about a time you exceeded a target or commercial goal", "field_type": "text_long"},
        {"label": "Describe a situation where you had to adapt to a significant change", "field_type": "text_long"},
        # Tier 3 role-specific
        {"label": "Why do you want to work at this company?",      "field_type": "text_long"},
        {"label": "What relevant experience do you have for this position?", "field_type": "text_long"},
    ]

    stats = run_answer_gen(
        job_id=args.job_id,
        fields=demo_fields,
        want_cover_letter=args.cover_letter,
    )

    print("=== Answer Generation Complete ===")
    print(f"  Tier 1 (auto):    {stats.get('t1', 0)}")
    print(f"  Tier 2 (auto):    {stats.get('t2', 0)}")
    print(f"  Tier 3 (AI):      {stats.get('t3', 0)}")
    print(f"  Tier 4 (STAR/AI): {stats.get('t4', 0)}")
    print(f"  Flagged:          {stats.get('flagged', 0)}")
    print(f"  Cover letter:     {'yes' if stats.get('cover_letter') else 'no'}")
