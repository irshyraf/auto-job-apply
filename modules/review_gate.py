"""
review_gate.py — Phase 5: Review Gate (Streamlit UI)

The single human checkpoint in the pipeline. Shows every pending application
as a card with all the context needed to approve, edit, or skip in ~60 seconds.

Visual rules (from review_gate_ux.json):
  Tier 1/2  — greyed out, muted, auto-approved
  Tier 3/4  — amber border, full opacity, requires review
  Flagged   — red border, blocks Approve button

Run:
    streamlit run modules/review_gate.py
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))
from database import get_connection, initialise_database

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Review Gate — Auto Job Apply",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

st.markdown("""
<style>
  /* Global */
  .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }

  /* Card wrapper */
  .rg-card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
    padding: 1.4rem 1.6rem;
    margin-bottom: 1.6rem;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }

  /* Header */
  .rg-job-title { font-size: 1.2rem; font-weight: 700; color: #111827; }
  .rg-company   { font-size: 0.95rem; color: #374151; }
  .rg-meta      { font-size: 0.82rem; color: #6b7280; margin-top: 2px; }

  /* Match score badge */
  .badge-high   { background:#dcfce7; color:#166534; padding:3px 10px; border-radius:99px; font-size:0.78rem; font-weight:600; }
  .badge-mid    { background:#fef9c3; color:#854d0e; padding:3px 10px; border-radius:99px; font-size:0.78rem; font-weight:600; }
  .badge-low    { background:#fee2e2; color:#991b1b; padding:3px 10px; border-radius:99px; font-size:0.78rem; font-weight:600; }

  /* Section label */
  .rg-section-label {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 1.5px;
    text-transform: uppercase; color: #9ca3af; margin-bottom: 4px;
  }

  /* Profile box */
  .rg-profile {
    background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 6px; padding: 10px 14px;
    font-size: 0.88rem; line-height: 1.55; color: #374151;
  }

  /* Answer rows — Tier 1/2 greyed out */
  .ans-auto {
    background: #f9fafb; border: 1px solid #f1f5f9;
    border-radius: 6px; padding: 8px 12px; margin-bottom: 6px;
    opacity: 0.6;
  }
  .ans-auto .ans-label { font-size: 0.75rem; color: #9ca3af; font-weight: 600; }
  .ans-auto .ans-value { font-size: 0.85rem; color: #6b7280; }

  /* Tier 3/4 amber */
  .ans-review {
    background: #fffbeb; border: 2px solid #f59e0b;
    border-radius: 6px; padding: 10px 14px; margin-bottom: 8px;
  }
  .ans-review .ans-label { font-size: 0.75rem; color: #b45309; font-weight: 700; }
  .ans-review .ans-value { font-size: 0.87rem; color: #374151; line-height: 1.55; }
  .ans-story-tag { font-size: 0.7rem; color: #b45309; margin-top: 4px; }

  /* Flagged — red */
  .ans-flagged {
    background: #fff1f2; border: 2px solid #ef4444;
    border-radius: 6px; padding: 10px 14px; margin-bottom: 8px;
  }
  .ans-flagged .ans-label { font-size: 0.75rem; color: #dc2626; font-weight: 700; }
  .ans-flagged .ans-value { font-size: 0.87rem; color: #374151; }

  /* Cover letter preview */
  .cl-preview {
    background: #f0f9ff; border: 1px solid #bae6fd;
    border-radius: 6px; padding: 10px 14px;
    font-size: 0.85rem; font-style: italic; color: #374151;
  }

  /* Divider */
  .rg-divider { border-top: 1px solid #f1f5f9; margin: 14px 0; }

  /* Summary bar */
  .summary-pill {
    display:inline-block; background:#f3f4f6; border-radius:99px;
    padding:4px 12px; font-size:0.8rem; color:#374151; margin-right:6px;
  }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_pending_jobs(sort_by: str) -> list[dict]:
    conn = get_connection()
    order = {
        "Match score (highest first)": "match_score DESC",
        "Newest first":                "date_scraped DESC",
        "Simplest first":              "(SELECT COUNT(*) FROM application_answers a WHERE a.job_id=jobs.id AND a.needs_review=1) ASC, match_score DESC",
    }.get(sort_by, "match_score DESC")
    rows = conn.execute(f"SELECT * FROM jobs WHERE status='pending_review' ORDER BY {order}").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_answers(job_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM application_answers
        WHERE job_id=?
        ORDER BY
            CASE WHEN flagged=1 THEN 3
                 WHEN tier>=3    THEN 2
                 ELSE 1 END,
            tier, id
    """, (job_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute("""
        UPDATE jobs SET status='approved', review_approved_at=? WHERE id=?
    """, (datetime.now().isoformat(), job_id))
    conn.execute("UPDATE application_answers SET approved=1 WHERE job_id=?", (job_id,))
    conn.commit()
    conn.close()


def skip_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='filtered_out', match_notes='Skipped in Review Gate' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def save_answer_edit(answer_id: int, new_text: str) -> None:
    conn = get_connection()
    conn.execute("""
        UPDATE application_answers
        SET answer_text=?, user_edited=1, updated_at=datetime('now')
        WHERE id=?
    """, (new_text, answer_id))
    conn.commit()
    conn.close()


def has_flagged_answers(job_id: int) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as n FROM application_answers WHERE job_id=? AND flagged=1", (job_id,)
    ).fetchone()
    conn.close()
    return row["n"] > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_cv_profile_text(job: dict) -> str | None:
    """Extract tailored profile text from the saved CV JSON sidecar file."""
    notes = job.get("match_notes") or ""
    # Parse PDF path from match_notes: "... | PDF: /path/to/file.pdf"
    import re
    m = re.search(r"PDF:\s*(.+\.pdf)", notes)
    if not m:
        return None
    pdf_path = Path(m.group(1).strip())
    json_path = pdf_path.with_suffix(".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return data.get("profile_text")
        except Exception:
            return None
    return None


def get_cv_pdf_path(job: dict) -> str | None:
    import re
    notes = job.get("match_notes") or ""
    m = re.search(r"PDF:\s*(.+\.pdf)", notes)
    return m.group(1).strip() if m else None


def get_cover_letter_text(answers: list[dict]) -> str | None:
    for a in answers:
        if a["field_name"] == "cover_letter":
            return a["answer_text"]
    return None


def jd_summary(description: str) -> str:
    """Extract roughly 2-3 sentences from the start of the JD."""
    if not description:
        return "No description available."
    import re
    sentences = re.split(r"(?<=[.!?])\s+", description.strip())
    # Skip very short fragment sentences
    clean = [s.strip() for s in sentences if len(s.strip()) > 40][:3]
    return " ".join(clean) or description[:300]


def match_badge(score: float | None) -> str:
    if score is None:
        return '<span class="badge-low">No score</span>'
    if score >= 0.65:
        return f'<span class="badge-high">Match {score:.0%}</span>'
    if score >= 0.45:
        return f'<span class="badge-mid">Match {score:.0%}</span>'
    return f'<span class="badge-low">Match {score:.0%}</span>'


def setup_badge(work_setup: str | None) -> str:
    icons = {"remote": "🌐 Remote", "hybrid": "🔄 Hybrid", "on-site": "🏢 On-site"}
    return icons.get((work_setup or "").lower(), work_setup or "Unknown")


# ---------------------------------------------------------------------------
# Render a single answer row
# ---------------------------------------------------------------------------

def render_answer(answer: dict, edit_mode: bool) -> None:
    aid       = answer["id"]
    label     = answer["field_name"]
    text      = answer["answer_text"] or ""
    tier      = answer["tier"]
    flagged   = bool(answer["flagged"])
    story_id  = answer.get("story_id")
    edited    = bool(answer.get("user_edited"))

    edit_key = f"edit_{aid}"

    if flagged:
        css_class = "ans-flagged"
        tier_label = "⚑ FLAGGED · manual required"
    elif tier in (3, 4):
        css_class = "ans-review"
        tier_label = f"Tier {tier} · review required"
    else:
        css_class = "ans-auto"
        tier_label = f"Tier {tier} · auto-filled"

    if edited:
        tier_label += " ✏️"

    if edit_mode:
        st.markdown(f'<div class="rg-section-label">{label} — {tier_label}</div>', unsafe_allow_html=True)
        new_val = st.text_area(
            label=label,
            value=st.session_state.get(edit_key, text),
            key=edit_key,
            height=100 if len(text) < 200 else 160,
            label_visibility="collapsed",
        )
        if new_val != text:
            st.session_state[edit_key] = new_val
    else:
        story_tag = f'<div class="ans-story-tag">Source: {story_id}</div>' if story_id else ""
        display_text = text.replace("\n", "<br>") if tier >= 3 else text
        st.markdown(f"""
        <div class="{css_class}">
          <div class="ans-label">{label}</div>
          <div class="ans-value">{display_text}</div>
          {story_tag}
        </div>
        """, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Render one full application card
# ---------------------------------------------------------------------------

def render_card(job: dict, answers: list[dict], idx: int) -> None:
    job_id    = job["id"]
    title     = job["job_title"]
    company   = job["company_name"]
    location  = job.get("location") or ""
    setup     = job.get("work_setup") or ""
    sal_min   = job.get("salary_min")
    sal_max   = job.get("salary_max")
    score     = job.get("match_score")
    source    = job.get("source_board", "")
    source_url = job.get("source_url", "")
    variant   = job.get("cv_variant_used", "AF_Resume")

    salary_str = "Salary not listed"
    if sal_min and sal_max:
        salary_str = f"£{sal_min:,} – £{sal_max:,}"
    elif sal_min:
        salary_str = f"£{sal_min:,}+"

    is_blocked     = has_flagged_answers(job_id)
    edit_mode_key  = f"edit_mode_{job_id}"
    if edit_mode_key not in st.session_state:
        st.session_state[edit_mode_key] = False

    profile_text = get_cv_profile_text(job)
    cv_pdf_path  = get_cv_pdf_path(job)
    cover_letter = get_cover_letter_text(answers)
    answers_only = [a for a in answers if a["field_name"] != "cover_letter"]

    t1_count = sum(1 for a in answers_only if a["tier"] <= 2)
    t3_count = sum(1 for a in answers_only if a["tier"] == 3)
    t4_count = sum(1 for a in answers_only if a["tier"] == 4)
    flag_count = sum(1 for a in answers_only if a["flagged"])

    with st.container():
        st.markdown('<div class="rg-card">', unsafe_allow_html=True)

        # ── Header ──
        col_title, col_badge = st.columns([4, 1])
        with col_title:
            st.markdown(f'<div class="rg-job-title">{title}</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="rg-company">{company}</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="rg-meta">{location} &nbsp;·&nbsp; {setup_badge(setup)} &nbsp;·&nbsp; {salary_str} &nbsp;·&nbsp; via {source.title()}</div>',
                unsafe_allow_html=True,
            )
        with col_badge:
            st.markdown(f'<div style="text-align:right;margin-top:6px">{match_badge(score)}</div>', unsafe_allow_html=True)

        st.markdown('<div class="rg-divider"></div>', unsafe_allow_html=True)

        # ── Job summary ──
        st.markdown('<div class="rg-section-label">Role summary</div>', unsafe_allow_html=True)
        st.markdown(f'<div style="font-size:0.86rem;color:#374151;line-height:1.5">{jd_summary(job.get("description_text",""))}</div>', unsafe_allow_html=True)

        st.markdown('<div class="rg-divider"></div>', unsafe_allow_html=True)

        # ── CV info ──
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("CV Variant", variant.replace("AF_", "").replace("_", " "))
        with m2:
            st.metric("Cover Letter", "Generated ✓" if cover_letter else "Not generated")
        with m3:
            st.metric("Questions", f"{t1_count} auto · {t3_count+t4_count} AI · {flag_count} flagged")

        # ── Document links ──
        st.markdown('<div class="rg-section-label" style="margin-top:10px">Documents</div>', unsafe_allow_html=True)
        dl_cols = st.columns(3)
        with dl_cols[0]:
            if cv_pdf_path and Path(cv_pdf_path).exists():
                with open(cv_pdf_path, "rb") as f:
                    st.download_button("📄 Download CV", f.read(), file_name=Path(cv_pdf_path).name,
                                       mime="application/pdf", key=f"dl_cv_{job_id}")
            else:
                st.caption("CV not yet generated")
        with dl_cols[1]:
            if source_url:
                st.link_button("🔗 Job Advert", source_url)
            else:
                st.caption("No URL")
        with dl_cols[2]:
            st.caption(f"Posted: {job.get('date_posted') or 'Unknown'}")

        st.markdown('<div class="rg-divider"></div>', unsafe_allow_html=True)

        # ── Tailored profile ──
        if profile_text:
            st.markdown('<div class="rg-section-label">Tailored Profile</div>', unsafe_allow_html=True)
            if st.session_state[edit_mode_key]:
                profile_text = st.text_area(
                    "Profile text", value=profile_text,
                    key=f"profile_{job_id}", height=100, label_visibility="collapsed"
                )
            else:
                st.markdown(f'<div class="rg-profile">{profile_text}</div>', unsafe_allow_html=True)
            st.markdown('<div class="rg-divider"></div>', unsafe_allow_html=True)

        # ── Application answers ──
        st.markdown('<div class="rg-section-label">Application Answers</div>', unsafe_allow_html=True)

        if not answers_only:
            st.caption("No answers generated yet — run answer_gen first.")
        else:
            # T1/T2 in expander (greyed out, skip-by-default)
            auto_answers = [a for a in answers_only if a["tier"] <= 2]
            if auto_answers:
                with st.expander(f"Auto-filled ({len(auto_answers)} fields — Tier 1/2)", expanded=False):
                    for a in auto_answers:
                        render_answer(a, st.session_state[edit_mode_key])

            # T3/T4 always expanded
            ai_answers = [a for a in answers_only if a["tier"] >= 3 and not a["flagged"]]
            for a in ai_answers:
                render_answer(a, st.session_state[edit_mode_key])

            # Flagged last
            flagged_answers = [a for a in answers_only if a["flagged"]]
            for a in flagged_answers:
                render_answer(a, st.session_state[edit_mode_key])

        # ── Cover letter preview ──
        if cover_letter:
            st.markdown('<div class="rg-divider"></div>', unsafe_allow_html=True)
            st.markdown('<div class="rg-section-label">Cover Letter</div>', unsafe_allow_html=True)
            first_para = cover_letter.split("\n\n")[0] if "\n\n" in cover_letter else cover_letter[:200]
            word_count = len(cover_letter.split())
            st.markdown(f'<div class="cl-preview">"{first_para[:200]}..."</div>', unsafe_allow_html=True)
            st.caption(f"{word_count} words")
            with st.expander("Read full cover letter"):
                if st.session_state[edit_mode_key]:
                    st.text_area("Cover letter", value=cover_letter,
                                 key=f"cl_{job_id}", height=300, label_visibility="collapsed")
                else:
                    st.markdown(cover_letter.replace("\n", "\n\n"))

        st.markdown('<div class="rg-divider"></div>', unsafe_allow_html=True)

        # ── Action bar ──
        a1, a2, a3 = st.columns([2, 1, 1])

        with a1:
            if is_blocked:
                st.error("⚑ Submission blocked — fill in flagged field(s) first", icon="🚫")
            else:
                if st.button(
                    "✅ Approve & Queue",
                    key=f"approve_{job_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    # Save any pending edits first
                    for a in answers_only:
                        ek = f"edit_{a['id']}"
                        if ek in st.session_state and st.session_state[ek] != (a["answer_text"] or ""):
                            save_answer_edit(a["id"], st.session_state[ek])
                    approve_job(job_id)
                    st.success(f"✅ Approved — {title} @ {company}")
                    st.rerun()

        with a2:
            edit_label = "💾 Save Edits" if st.session_state[edit_mode_key] else "✏️ Edit"
            if st.button(edit_label, key=f"edit_btn_{job_id}", use_container_width=True):
                if st.session_state[edit_mode_key]:
                    # Save edits to DB
                    for a in answers_only:
                        ek = f"edit_{a['id']}"
                        if ek in st.session_state and st.session_state[ek] != (a["answer_text"] or ""):
                            save_answer_edit(a["id"], st.session_state[ek])
                    st.session_state[edit_mode_key] = False
                    st.success("Edits saved.")
                    st.rerun()
                else:
                    st.session_state[edit_mode_key] = True
                    st.rerun()

        with a3:
            if st.button("⏭ Skip", key=f"skip_{job_id}", use_container_width=True):
                skip_job(job_id)
                st.warning(f"Skipped — {title} @ {company}")
                st.rerun()

        st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar(jobs: list[dict]) -> tuple[str, str]:
    with st.sidebar:
        st.title("🎯 Review Gate")
        st.caption("Auto Job Apply — Phase 5")
        st.markdown("---")

        sort_by = st.selectbox(
            "Sort by",
            ["Match score (highest first)", "Newest first", "Simplest first"],
        )
        filter_by = st.selectbox(
            "Filter",
            ["All", "Has AI questions (T3/T4)", "Has cover letter", "Simple only (T1/T2)"],
        )

        st.markdown("---")

        # Stats
        approved = sum(1 for j in jobs)   # pending jobs shown
        conn = get_connection()
        total_approved  = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='approved'").fetchone()[0]
        total_submitted = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='submitted'").fetchone()[0]
        conn.close()

        st.metric("Pending Review", len(jobs))
        st.metric("Approved (queued)", total_approved)
        st.metric("Submitted", total_submitted)

        st.markdown("---")

        # Approve all (only when no flagged blocking)
        approvable = [j for j in jobs if not has_flagged_answers(j["id"])]
        if st.button(
            f"✅ Approve All ({len(approvable)})",
            type="primary",
            disabled=len(approvable) == 0,
            use_container_width=True,
        ):
            for j in approvable:
                approve_job(j["id"])
            st.success(f"Approved {len(approvable)} applications.")
            st.rerun()

        if len(approvable) < len(jobs):
            st.caption(f"⚑ {len(jobs) - len(approvable)} application(s) blocked by flagged field")

    return sort_by, filter_by


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    initialise_database()

    # Sidebar controls
    all_jobs = load_pending_jobs("match_score DESC")
    sort_by, filter_by = render_sidebar(all_jobs)

    # Reload with chosen sort
    jobs = load_pending_jobs(sort_by)

    # Apply filter
    if filter_by == "Has AI questions (T3/T4)":
        conn = get_connection()
        jobs = [j for j in jobs if conn.execute(
            "SELECT COUNT(*) FROM application_answers WHERE job_id=? AND tier>=3 AND flagged=0", (j["id"],)
        ).fetchone()[0] > 0]
        conn.close()
    elif filter_by == "Has cover letter":
        conn = get_connection()
        jobs = [j for j in jobs if conn.execute(
            "SELECT COUNT(*) FROM application_answers WHERE job_id=? AND field_name='cover_letter'", (j["id"],)
        ).fetchone()[0] > 0]
        conn.close()
    elif filter_by == "Simple only (T1/T2)":
        conn = get_connection()
        jobs = [j for j in jobs if conn.execute(
            "SELECT COUNT(*) FROM application_answers WHERE job_id=? AND tier>=3", (j["id"],)
        ).fetchone()[0] == 0]
        conn.close()

    # Header
    st.title("Review Gate")
    if not jobs:
        st.success("🎉 Nothing left to review. All caught up.")
        st.caption("Approve more applications or run the scraper to find new jobs.")
        return

    # Summary bar
    total_ai = sum(
        1 for j in jobs
        for _ in load_answers(j["id"])
        if _["tier"] >= 3 and not _["flagged"] and _["field_name"] != "cover_letter"
    )
    total_flagged = sum(1 for j in jobs if has_flagged_answers(j["id"]))
    est_mins = round(len(jobs) * 60 / 60)

    st.markdown(
        f'<span class="summary-pill">📋 {len(jobs)} applications</span>'
        f'<span class="summary-pill">🤖 {total_ai} AI answers to review</span>'
        f'<span class="summary-pill">⚑ {total_flagged} flagged</span>'
        f'<span class="summary-pill">⏱ ~{est_mins} min estimated</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    # Cards
    for i, job in enumerate(jobs):
        answers = load_answers(job["id"])
        render_card(job, answers, i)


if __name__ == "__main__":
    main()
