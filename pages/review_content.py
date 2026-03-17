"""
pages/review_content.py — Stage 2 Review Gate

Full content review for jobs that have been tailored.
Each job is submitted individually — no batch submit.
"""

import json
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from database import get_connection


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CARD_CSS = """
<style>
.review-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 20px 24px;
    margin-bottom: 16px;
}
.card-header    { font-size: 16px; font-weight: 700; color: #1e293b; }
.card-meta      { font-size: 13px; color: #64748b; margin-top: 2px; }
.profile-box    { background:#f8fafc; border-left:3px solid #94a3b8; padding:10px 14px;
                  border-radius:0 6px 6px 0; font-size:13px; color:#475569;
                  margin: 12px 0; line-height:1.6; }
.answer-t12     { opacity:0.55; font-size:13px; padding:6px 0; }
.answer-t34     { border-left:3px solid #f59e0b; padding:8px 12px; margin:6px 0;
                  border-radius:0 6px 6px 0; background:#fffbeb; font-size:13px; }
.answer-flag    { border-left:3px solid #dc2626; padding:8px 12px; margin:6px 0;
                  border-radius:0 6px 6px 0; background:#fef2f2; font-size:13px; }
.answer-label   { font-weight:600; font-size:12px; text-transform:uppercase;
                  letter-spacing:0.04em; color:#475569; margin-bottom:2px; }
.cl-preview     { font-style:italic; color:#475569; font-size:13px; }
.score-badge    { display:inline-block; background:#dbeafe; color:#1d4ed8;
                  border-radius:9999px; padding:2px 10px; font-size:12px; font-weight:700; }
.tag-autofill   { font-size:11px; color:#64748b; margin-left:6px; }
.story-tag      { font-size:11px; color:#7c3aed; margin-left:6px; }
</style>
"""


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_stage2_jobs() -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, job_title, company_name, salary_min, salary_max,
               match_score, cv_variant_used, match_notes, source_url
        FROM jobs
        WHERE status = 'pending_stage_2'
        ORDER BY match_score DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_answers(job_id: int) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, field_name, tier, answer_text, answer_source,
               story_id, needs_review, flagged, user_edited
        FROM application_answers
        WHERE job_id=?
        ORDER BY flagged DESC, tier DESC, id ASC
    """, (job_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_pdf_path(job: dict) -> str | None:
    notes = job.get("match_notes") or ""
    m = re.search(r"PDF:\s*(.+\.pdf)", notes)
    if m:
        p = Path(m.group(1).strip())
        return str(p) if p.exists() else None
    return None


def _get_cover_letter(job_id: int) -> str | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT answer_text FROM application_answers WHERE job_id=? AND field_name='cover_letter'",
        (job_id,)
    ).fetchone()
    conn.close()
    return row["answer_text"] if row else None


def _get_tailored_profile(job: dict) -> str | None:
    pdf_path = _get_pdf_path(job)
    if not pdf_path:
        return None
    json_path = pdf_path.replace(".pdf", ".json")
    if Path(json_path).exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                cv_data = json.load(f)
            return cv_data.get("profile_text")
        except Exception:
            pass
    return None


def _salary_str(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"£{lo:,}–£{hi:,}"
    if lo:
        return f"£{lo:,}+"
    return ""


def _approve_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def _skip_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='skipped_stage_2' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def _save_answer_edit(answer_id: int, new_text: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE application_answers SET answer_text=?, user_edited=1, updated_at=datetime('now') WHERE id=?",
        (new_text, answer_id)
    )
    conn.commit()
    conn.close()


def _save_to_answer_bank(story_id: str, new_text: str) -> None:
    """Overwrite the 'result' field of a STAR story in answer_bank.json."""
    try:
        bank_path = Path(__file__).parent.parent / "config" / "answer_bank.json"
        with open(bank_path, encoding="utf-8") as f:
            bank = json.load(f)
        for story in bank:
            if story.get("story_id") == story_id:
                story["result"] = new_text
                break
        with open(bank_path, "w", encoding="utf-8") as f:
            json.dump(bank, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render() -> None:
    st.markdown(CARD_CSS, unsafe_allow_html=True)

    st.title("Review content")
    st.caption("Review each application's tailored CV, answers, and cover letter. Click Submit on each one individually.")

    jobs = _load_stage2_jobs()

    if not jobs:
        st.info("No applications waiting for content review. Approve jobs on the **New matches** page first.")
        return

    for job in jobs:
        job_id    = job["id"]
        score_pct = int((job.get("match_score") or 0) * 100)
        salary    = _salary_str(job)
        variant   = job.get("cv_variant_used") or "Default"
        pdf_path  = _get_pdf_path(job)
        url       = job.get("source_url") or "#"
        answers   = _load_answers(job_id)
        cover_letter = _get_cover_letter(job_id)
        profile   = _get_tailored_profile(job)
        has_flagged = any(a["flagged"] for a in answers)

        with st.expander(
            f"**{job['job_title']}** — {job['company_name']}  |  {salary}  |  Score: {score_pct}%",
            expanded=True
        ):
            # Card header row
            hcol1, hcol2 = st.columns([5, 1])
            with hcol1:
                st.markdown(
                    f"**{job['job_title']}** · {job['company_name']} · {salary}  "
                    f"<span class='score-badge'>{score_pct}%</span>  "
                    f"<span class='tag-autofill'>CV: {variant}</span>",
                    unsafe_allow_html=True
                )
                cl_status = "Cover letter: ready" if cover_letter else "Cover letter: not generated"
                st.caption(cl_status)

            # Document links
            link_parts = []
            if pdf_path:
                link_parts.append(f"[View tailored CV (PDF)]({Path(pdf_path).as_uri()})")
            if cover_letter:
                link_parts.append("[View cover letter ↓]")
            link_parts.append(f"[View job advert →]({url})")
            st.markdown("  ·  ".join(link_parts))

            # Tailored profile
            if profile:
                st.markdown(f'<div class="profile-box">{profile}</div>', unsafe_allow_html=True)

            # Answers
            st.markdown("**Application answers**")
            for ans in answers:
                field = ans["field_name"]
                if field == "cover_letter":
                    continue
                tier     = ans["tier"]
                text     = ans.get("answer_text") or ""
                flagged  = ans.get("flagged", 0)
                edited   = ans.get("user_edited", 0)
                story_id = ans.get("story_id")

                if flagged:
                    st.markdown(
                        f'<div class="answer-flag">'
                        f'<div class="answer-label">⚑ {field} — MANUAL REVIEW REQUIRED</div>'
                        f'<div>{text}</div>'
                        f'</div>',
                        unsafe_allow_html=True
                    )
                elif tier in (1, 2):
                    st.markdown(
                        f'<div class="answer-t12">'
                        f'{field}: <em>{text[:120]}</em>'
                        f'<span class="tag-autofill">auto-filled</span>'
                        f'</div>',
                        unsafe_allow_html=True
                    )
                else:
                    story_note = f'<span class="story-tag">{story_id}</span>' if story_id else ""
                    edited_note = " ✏" if edited else ""
                    st.markdown(
                        f'<div class="answer-t34">'
                        f'<div class="answer-label">{field}{story_note}{edited_note}</div>'
                        f'<div>{text}</div>'
                        f'</div>',
                        unsafe_allow_html=True
                    )

            # Cover letter preview
            if cover_letter:
                words = len(cover_letter.split())
                first_line = cover_letter.split("\n")[0] if "\n" in cover_letter else cover_letter[:100]
                with st.expander(f"Cover letter ({words} words) — click to expand"):
                    st.markdown(f'<div class="cl-preview">{first_line}</div>', unsafe_allow_html=True)
                    st.markdown("---")
                    st.write(cover_letter)

            st.markdown("---")

            # Edit mode
            if f"edit_mode_{job_id}" not in st.session_state:
                st.session_state[f"edit_mode_{job_id}"] = False

            if st.session_state[f"edit_mode_{job_id}"]:
                st.markdown("**Edit mode**")
                editable_answers = [a for a in answers if a["tier"] in (3, 4) or a.get("flagged")]
                for ans in editable_answers:
                    field = ans["field_name"]
                    st.text_area(
                        field,
                        value=ans.get("answer_text") or "",
                        key=f"edit_{job_id}_{ans['id']}"
                    )
                    if ans.get("story_id"):
                        st.checkbox(
                            "Save this version to your Answer Bank for future use",
                            key=f"bank_{job_id}_{ans['id']}"
                        )

                # Cover letter edit
                cl_answers = []
                if cover_letter:
                    cl_answers = [a for a in answers if a["field_name"] == "cover_letter"]
                    if cl_answers:
                        st.text_area("Cover letter", value=cover_letter, height=200, key=f"edit_cl_{job_id}")

                ecol1, ecol2 = st.columns(2)
                with ecol1:
                    if st.button("Save", key=f"save_{job_id}", type="primary"):
                        for ans in editable_answers:
                            new_val = st.session_state.get(f"edit_{job_id}_{ans['id']}", ans.get("answer_text") or "")
                            if new_val != (ans.get("answer_text") or ""):
                                _save_answer_edit(ans["id"], new_val)
                                if st.session_state.get(f"bank_{job_id}_{ans['id']}") and ans.get("story_id"):
                                    _save_to_answer_bank(ans["story_id"], new_val)
                        if cl_answers:
                            new_cl = st.session_state.get(f"edit_cl_{job_id}", cover_letter)
                            if new_cl != cover_letter:
                                _save_answer_edit(cl_answers[0]["id"], new_cl)
                        st.session_state[f"edit_mode_{job_id}"] = False
                        st.toast("Changes saved.", icon="✅")
                        st.rerun()
                with ecol2:
                    if st.button("Cancel", key=f"cancel_{job_id}"):
                        st.session_state[f"edit_mode_{job_id}"] = False
                        st.rerun()
            else:
                # Action buttons
                act_col1, act_col2, act_col3 = st.columns([2, 1.5, 1.5])

                with act_col1:
                    submit_disabled = has_flagged
                    submit_help = "Fill all flagged answers (red borders) before submitting." if has_flagged else ""
                    if st.button(
                        "Submit",
                        key=f"submit_{job_id}",
                        type="primary",
                        disabled=submit_disabled,
                        help=submit_help,
                        use_container_width=True
                    ):
                        # Move to approved first so submitter can find it
                        _approve_job(job_id)
                        with st.spinner(f"Submitting to {job['company_name']}…"):
                            from modules.submitter import submit_single_job
                            result = submit_single_job(job_id)
                        if result["success"]:
                            st.toast(
                                f"{job['job_title']} at {job['company_name']} submitted successfully.",
                                icon="✅"
                            )
                        else:
                            # Revert approved → pending_stage_2 for retry
                            conn = get_connection()
                            conn.execute("UPDATE jobs SET status='pending_stage_2' WHERE id=? AND status='approved'", (job_id,))
                            conn.commit()
                            conn.close()
                            st.error(f"Submission failed: {result.get('error', 'Unknown error')}")
                        st.rerun()

                with act_col2:
                    if st.button("Edit", key=f"edit_{job_id}", use_container_width=True):
                        st.session_state[f"edit_mode_{job_id}"] = True
                        st.rerun()

                with act_col3:
                    if st.button("Skip", key=f"skip_{job_id}", use_container_width=True):
                        _skip_job(job_id)
                        st.rerun()
