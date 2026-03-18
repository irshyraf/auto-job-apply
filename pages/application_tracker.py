"""
pages/application_tracker.py — Application Tracker

Full history of every application. Filterable by status, searchable,
sortable. Row expands to show tailored CV, cover letter, all answers.
Also auto-marks no_response after 14 days.
"""

import csv
import io
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from database import get_connection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANUAL_STATUSES = ["submitted", "in_progress", "no_response", "interview", "rejected", "withdrawn"]

TAB_FILTERS = {
    "All":        None,
    "Submitted":  ["submitted", "in_progress", "no_response", "interview", "rejected", "withdrawn"],
    "In review":  ["new", "matched", "pending_stage_1", "approved_stage_1", "researched", "pending_stage_2", "approved", "queued"],
    "Skipped":    ["skipped_stage_1", "skipped_stage_2"],
}

STATUS_COLOUR = {
    "new":             "⚪",
    "matched":         "🟡",
    "submitted":       "🟢",
    "interview":       "🟢",
    "in_progress":     "🔵",
    "pending_stage_1": "🟡",
    "pending_stage_2": "🟡",
    "approved_stage_1":"🔵",
    "approved":        "🔵",
    "queued":          "⚪",
    "researched":      "🟡",
    "no_response":     "⚪",
    "rejected":        "🔴",
    "withdrawn":       "⚪",
    "skipped_stage_1": "⚪",
    "skipped_stage_2": "⚪",
    "filtered_out":    "⚪",
}


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _auto_update_no_response() -> None:
    """Mark submitted jobs with no update after 14 days as no_response."""
    try:
        from modules.tracker import auto_update_no_response
        auto_update_no_response()
    except Exception:
        pass


@st.cache_data(ttl=30)
def _load_jobs(status_filter: list | None = None, search: str = "") -> list:
    conn = get_connection()
    query = """
        SELECT id, job_title, company_name, salary_min, salary_max,
               cv_variant_used, submitted_at, date_scraped, status,
               match_notes, source_url
        FROM jobs
    """
    params = []
    conditions = []

    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        conditions.append(f"status IN ({placeholders})")
        params.extend(status_filter)

    if search:
        conditions.append("(LOWER(job_title) LIKE ? OR LOWER(company_name) LIKE ?)")
        params.extend([f"%{search.lower()}%", f"%{search.lower()}%"])

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY COALESCE(submitted_at, date_scraped) DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@st.cache_data(ttl=30)
def _load_answers(job_id: int) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT field_name, tier, answer_text, story_id, flagged
        FROM application_answers
        WHERE job_id=?
        ORDER BY flagged DESC, tier DESC
    """, (job_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_pdf_path(job: dict) -> str | None:
    import re
    notes = job.get("match_notes") or ""
    m = re.search(r"PDF:\s*(.+\.pdf)", notes)
    if m:
        p = Path(m.group(1).strip())
        return str(p) if p.exists() else None
    return None


def _salary_str(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"£{lo:,}–£{hi:,}"
    if lo:
        return f"£{lo:,}+"
    return "—"


def _update_status(job_id: int, new_status: str) -> None:
    if new_status not in MANUAL_STATUSES:
        return
    conn = get_connection()
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (new_status, job_id))
    conn.commit()
    conn.close()


def _jobs_to_csv(jobs: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Role", "Company", "Salary", "CV Variant", "Applied Date", "Status"])
    for j in jobs:
        writer.writerow([
            j["id"],
            j["job_title"],
            j["company_name"],
            _salary_str(j),
            j.get("cv_variant_used") or "",
            (j.get("submitted_at") or j.get("date_scraped") or "")[:10],
            j["status"],
        ])
    return buf.getvalue()


@st.cache_data(ttl=30)
def _count_by_tab(search: str = "") -> dict:
    conn = get_connection()
    if search:
        like_val = f"%{search.lower()}%"
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM jobs "
            "WHERE (LOWER(job_title) LIKE ? OR LOWER(company_name) LIKE ?) "
            "GROUP BY status",
            (like_val, like_val),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM jobs GROUP BY status"
        ).fetchall()
    conn.close()
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())

    tab_counts = {"All": total}
    for tab, statuses in TAB_FILTERS.items():
        if statuses is None:
            continue
        tab_counts[tab] = sum(counts.get(s, 0) for s in statuses)
    return tab_counts


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("Application tracker")
    st.caption("All applications across every status.")

    # Auto-update stale submitted jobs
    _auto_update_no_response()

    # Search box
    search = st.text_input("Search by role or company", placeholder="e.g. Gartner, Account Executive", label_visibility="collapsed")

    # Tab counts
    tab_counts = _count_by_tab(search)
    tab_labels = [f"{name} ({tab_counts.get(name, 0)})" for name in TAB_FILTERS]
    selected_tab = st.tabs(tab_labels)

    for i, (tab_name, statuses) in enumerate(TAB_FILTERS.items()):
        with selected_tab[i]:
            jobs = _load_jobs(status_filter=statuses, search=search)

            if not jobs:
                st.info("No applications in this category.")
                continue

            # CSV export
            csv_data = _jobs_to_csv(jobs)
            st.download_button(
                "⬇ Download as CSV",
                data=csv_data,
                file_name="applications.csv",
                mime="text/csv",
                key=f"csv_{tab_name}"
            )

            # Table header
            st.markdown('<div role="table" aria-label="Application history">', unsafe_allow_html=True)
            cols = st.columns([3, 2.5, 1.5, 1.5, 1.5, 2])
            headers = ["Role", "Company", "Salary", "CV Variant", "Date", "Status"]
            for col, h in zip(cols, headers):
                col.markdown(f'<div role="columnheader" scope="col"><strong>{h}</strong></div>', unsafe_allow_html=True)
            st.markdown("---")

            for job in jobs:
                job_id   = job["id"]
                dot      = STATUS_COLOUR.get(job["status"], "⚪")
                date_str = (job.get("submitted_at") or job.get("date_scraped") or "")[:10]

                row_cols = st.columns([3, 2.5, 1.5, 1.5, 1.5, 2])
                row_cols[0].write(job["job_title"])
                row_cols[1].write(job["company_name"])
                row_cols[2].write(_salary_str(job))
                row_cols[3].write(job.get("cv_variant_used") or "—")
                row_cols[4].write(date_str)
                row_cols[5].write(f"{dot} {job['status']}")

                # Expandable detail row
                with st.expander(f"Details — {job['job_title']} @ {job['company_name']}"):
                    detail_cols = st.columns(3)

                    pdf_path = _get_pdf_path(job)
                    if pdf_path:
                        detail_cols[0].markdown(f"[View tailored CV (PDF)]({Path(pdf_path).as_uri()})")
                    else:
                        detail_cols[0].write("No tailored CV")

                    if job.get("source_url"):
                        detail_cols[1].markdown(f"[View job advert →]({job['source_url']})")

                    if job.get("submitted_at"):
                        detail_cols[2].write(f"Submitted: {job['submitted_at'][:16]}")

                    # Answers
                    answers = _load_answers(job_id)
                    if answers:
                        st.markdown("**Answers**")
                        for ans in answers:
                            if ans["field_name"] == "cover_letter":
                                continue
                            flag_icon = " ⚑" if ans.get("flagged") else ""
                            text_preview = (ans.get("answer_text") or "")[:150]
                            st.markdown(
                                f"**T{ans['tier']}** {flag_icon} `{ans['field_name']}`: {text_preview}"
                            )

                    # Manual status update (terminal statuses only)
                    if job["status"] in ("submitted", "in_progress", "no_response", "interview", "rejected", "withdrawn"):
                        st.markdown("**Update status**")
                        new_status = st.selectbox(
                            "Status",
                            MANUAL_STATUSES,
                            index=MANUAL_STATUSES.index(job["status"]) if job["status"] in MANUAL_STATUSES else 0,
                            key=f"status_sel_{job_id}",
                            label_visibility="collapsed"
                        )
                        if st.button("Save status", key=f"save_status_{job_id}"):
                            _update_status(job_id, new_status)
                            st.toast(f"Status updated to '{new_status}'.", icon="✅")
                            st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)
