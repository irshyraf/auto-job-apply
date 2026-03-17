"""
pages/new_matches.py — Stage 1 Review Gate

Lightweight cards for approving or skipping scraped matches BEFORE any API cost.
Approving a card triggers CV tailoring + pre-answer generation (with a spinner).
"""

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
.match-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 12px;
}
.job-title  { font-size: 16px; font-weight: 700; color: #1e293b; }
.job-meta   { font-size: 13px; color: #64748b; margin-top: 2px; }
.jd-summary { font-size: 13px; color: #475569; margin-top: 10px; line-height: 1.5; }
.score-badge {
    display: inline-block;
    background: #dbeafe; color: #1d4ed8;
    border-radius: 9999px; padding: 2px 10px;
    font-size: 12px; font-weight: 700;
}
</style>
"""


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_stage1_jobs(sort_by: str = "match_score") -> list:
    conn = get_connection()
    order = {
        "Match score (highest first)": "match_score DESC",
        "Salary (highest first)":      "salary_max DESC NULLS LAST",
        "Newest first":                "date_scraped DESC",
    }.get(sort_by, "match_score DESC")

    rows = conn.execute(f"""
        SELECT id, job_title, company_name, location, work_setup,
               salary_min, salary_max, match_score, description_text,
               source_url, date_scraped
        FROM jobs
        WHERE status = 'pending_stage_1'
        ORDER BY {order}
        LIMIT 10
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _salary_str(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"£{lo:,}–£{hi:,}"
    if lo:
        return f"£{lo:,}+"
    if hi:
        return f"up to £{hi:,}"
    return "Salary not stated"


def _jd_summary(job: dict) -> str:
    """Return first ~300 chars of JD as a quick preview."""
    text = (job.get("description_text") or "").strip()
    if not text:
        return "No description available."
    # Trim to ~300 chars at a sentence boundary if possible
    if len(text) <= 300:
        return text
    truncated = text[:300]
    last_period = truncated.rfind(".")
    if last_period > 150:
        return truncated[:last_period + 1]
    return truncated + "…"


def _approve_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='approved_stage_1' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def _skip_job(job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE jobs SET status='skipped_stage_1' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def _approve_all(job_ids: list) -> None:
    if not job_ids:
        return
    conn = get_connection()
    placeholders = ",".join("?" * len(job_ids))
    conn.execute(f"UPDATE jobs SET status='approved_stage_1' WHERE id IN ({placeholders})", job_ids)
    conn.commit()
    conn.close()


def _skip_all(job_ids: list) -> None:
    if not job_ids:
        return
    conn = get_connection()
    placeholders = ",".join("?" * len(job_ids))
    conn.execute(f"UPDATE jobs SET status='skipped_stage_1' WHERE id IN ({placeholders})", job_ids)
    conn.commit()
    conn.close()


def _run_tailoring(job_id: int) -> dict:
    """Tailor CV + pre-generate answers for one job."""
    from modules.cv_tailor import tailor_single_job
    from modules.answer_gen import generate_answers_for_job

    tailor_result = tailor_single_job(job_id)
    if not tailor_result["success"]:
        return {"success": False, "error": tailor_result.get("error", "CV tailoring failed")}

    answer_result = generate_answers_for_job(job_id, want_cover_letter=True)
    # Answer gen failure is non-blocking — CV tailored OK is the important part
    return {
        "success":      True,
        "pdf_path":     tailor_result["pdf_path"],
        "answer_count": answer_result.get("answer_count", 0),
        "flagged":      answer_result.get("flagged", 0),
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render() -> None:
    st.markdown(CARD_CSS, unsafe_allow_html=True)

    st.title("New matches")
    st.caption("Approve which jobs are worth tailoring. No API cost until you approve.")

    sort_options = ["Match score (highest first)", "Salary (highest first)", "Newest first"]
    col_batch1, col_batch2, col_sort = st.columns([1.5, 1.5, 3])

    with col_sort:
        sort_by = st.selectbox("Sort by", sort_options, label_visibility="collapsed")

    jobs = _load_stage1_jobs(sort_by)

    if not jobs:
        st.info("No new matches. Click **Scrape now** on the Dashboard to find jobs.")
        return

    job_ids = [j["id"] for j in jobs]

    with col_batch1:
        if st.button("✓ Approve all", use_container_width=True):
            _approve_all(job_ids)
            st.toast(f"All {len(job_ids)} jobs queued for tailoring.", icon="✅")
            with st.spinner(f"Tailoring {len(job_ids)} CV(s)…"):
                from modules.cv_tailor import tailor_single_job
                from modules.answer_gen import generate_answers_for_job
                for jid in job_ids:
                    tailor_single_job(jid)
                    generate_answers_for_job(jid, want_cover_letter=True)
            st.toast("Tailoring complete — review in Review content.", icon="✅")
            st.session_state["page"] = "review_content"
            st.rerun()

    with col_batch2:
        if st.button("✗ Skip all", use_container_width=True):
            _skip_all(job_ids)
            st.toast(f"Skipped {len(job_ids)} jobs.", icon="ℹ️")
            st.rerun()

    st.markdown("---")

    for job in jobs:
        job_id    = job["id"]
        score_pct = int((job.get("match_score") or 0) * 100)
        salary    = _salary_str(job)
        setup     = job.get("work_setup") or "unknown"
        summary   = _jd_summary(job)
        url       = job.get("source_url") or "#"

        with st.container():
            st.markdown(f"""
                <div class="match-card">
                    <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                        <div>
                            <div class="job-title">{job['job_title']}</div>
                            <div class="job-meta">
                                {job['company_name']} · {job.get('location','London')} · {setup.title()} · {salary}
                            </div>
                        </div>
                        <div class="score-badge">{score_pct}%</div>
                    </div>
                    <div class="jd-summary">{summary}</div>
                </div>
            """, unsafe_allow_html=True)

            btn_col1, btn_col2, btn_col3 = st.columns([1.5, 1.5, 3])
            with btn_col1:
                approve = st.button("✓ Approve", key=f"approve_{job_id}", type="primary", use_container_width=True)
            with btn_col2:
                skip = st.button("✗ Skip", key=f"skip_{job_id}", use_container_width=True)
            with btn_col3:
                st.markdown(f"[View job advert →]({url})")

            if approve:
                _approve_job(job_id)
                with st.spinner(f"Tailoring CV for {job['job_title']} at {job['company_name']}…"):
                    result = _run_tailoring(job_id)
                if result["success"]:
                    flagged_note = f" ({result['flagged']} answer(s) need manual review)" if result.get("flagged") else ""
                    st.toast(f"Tailored — {result['answer_count']} answer(s) pre-generated{flagged_note}", icon="✅")
                else:
                    st.error(f"Tailoring failed: {result.get('error')}")
                st.rerun()

            if skip:
                _skip_job(job_id)
                st.rerun()
