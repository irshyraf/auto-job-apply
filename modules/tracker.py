"""
tracker.py — Phase 7: Status Tracking and Reporting

Two things this module does:

1. AUTO-UPDATES (run on a schedule or manually):
   - Marks submitted applications as 'no_response' after NO_RESPONSE_DAYS (default 14)
   - Never touches manually-set statuses (interview, rejected)

2. DASHBOARD (Streamlit):
   - Pipeline funnel: every status with counts
   - Submitted table: company, role, days since submission, status
   - Follow-up list: no_response > 7 days — worth a nudge
   - Interview tracker
   - Rejection log (useful for spotting patterns)

3. MANUAL STATUS UPDATE (CLI):
   - python3 -m modules.tracker --update <job_id> <status>
   - Statuses: interview | rejected | no_response | submitted | withdrawn

Run dashboard:
    streamlit run modules/tracker.py

Run auto-update + digest:
    python3 -m modules.tracker --digest

Update a single job:
    python3 -m modules.tracker --update 56 interview
"""

import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from database import get_connection, initialise_database

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NO_RESPONSE_DAYS   = 14   # mark as no_response after this many days with no update
FOLLOW_UP_DAYS     = 7    # surface for follow-up after this many days with no response
ACTIVE_STATUSES    = ("submitted", "in_progress")
TERMINAL_STATUSES  = ("interview", "rejected", "no_response", "withdrawn")

STATUS_LABELS = {
    "scraped":          "🔍 Scraped",
    "filtered_out":     "🚫 Filtered out",
    "pending_stage_1":  "👁  Stage 1 — new matches",
    "approved_stage_1": "⚙️  Queued for tailoring",
    "skipped_stage_1":  "⏭  Skipped at Stage 1",
    "queued":           "🗂  Queued (next run)",
    "pending_stage_2":  "📝 Stage 2 — review content",
    "skipped_stage_2":  "⏭  Skipped at Stage 2",
    "approved":         "✅ Approved",
    "in_progress":      "⚙️  In progress",
    "submitted":        "📤 Submitted",
    "no_response":      "😶 No response",
    "interview":        "🎉 Interview",
    "rejected":         "❌ Rejected",
    "withdrawn":        "↩️  Withdrawn",
}

# ---------------------------------------------------------------------------
# Core auto-update logic
# ---------------------------------------------------------------------------

def auto_update_no_response() -> int:
    """
    Mark submitted jobs as 'no_response' if they've been waiting
    longer than NO_RESPONSE_DAYS since submitted_at.
    Returns the number of jobs updated.
    """
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=NO_RESPONSE_DAYS)).isoformat()
    result = conn.execute("""
        UPDATE jobs
        SET status = 'no_response'
        WHERE status = 'submitted'
          AND submitted_at IS NOT NULL
          AND submitted_at < ?
    """, (cutoff,))
    updated = result.rowcount
    conn.commit()
    conn.close()
    return updated


MANUAL_STATUSES = {"submitted", "in_progress", "no_response", "interview", "rejected", "withdrawn"}


def update_status(job_id: int, new_status: str) -> bool:
    """Manually set the status of a job. Returns True on success.
    Only allows terminal/response statuses — cannot bypass the review gate."""
    if new_status not in MANUAL_STATUSES:
        print(f"Unknown status '{new_status}'. Allowed: {sorted(MANUAL_STATUSES)}")
        return False
    conn = get_connection()
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (new_status, job_id))
    conn.commit()
    conn.close()
    print(f"Job {job_id} → {new_status}")
    return True


# ---------------------------------------------------------------------------
# Digest data
# ---------------------------------------------------------------------------

def _days_since(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str[:19])
        return (datetime.now() - dt).days
    except ValueError:
        return None


def get_pipeline_counts() -> dict[str, int]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM jobs GROUP BY status"
    ).fetchall()
    conn.close()
    return {r["status"]: r["n"] for r in rows}


def get_submitted_jobs() -> list[dict]:
    """All submitted/in-flight/responded jobs, enriched with days_since."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, job_title, company_name, location, work_setup,
               source_url, submitted_at, status, application_ref, match_score
        FROM jobs
        WHERE status IN ('submitted','in_progress','no_response','interview','rejected','withdrawn')
        ORDER BY submitted_at DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["days_since"] = _days_since(d.get("submitted_at"))
        result.append(d)
    return result


def get_followup_needed() -> list[dict]:
    """Submitted jobs that have been waiting more than FOLLOW_UP_DAYS with no update."""
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    rows = conn.execute("""
        SELECT id, job_title, company_name, source_url, submitted_at
        FROM jobs
        WHERE status IN ('submitted', 'no_response')
          AND submitted_at < ?
        ORDER BY submitted_at ASC
    """, (cutoff,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["days_since"] = _days_since(d.get("submitted_at"))
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Text digest (CLI output)
# ---------------------------------------------------------------------------

def generate_digest() -> str:
    updated = auto_update_no_response()
    counts  = get_pipeline_counts()
    submitted = get_submitted_jobs()
    followups = get_followup_needed()

    lines = []
    lines.append("=" * 56)
    lines.append(f"  AUTO JOB APPLY — Daily Digest  {date.today().isoformat()}")
    lines.append("=" * 56)

    # Pipeline funnel
    lines.append("\nPIPELINE FUNNEL")
    lines.append("-" * 30)
    order = [
        "pending_stage_1", "approved_stage_1", "queued",
        "pending_stage_2", "approved",
        "in_progress", "submitted", "no_response", "interview",
        "rejected", "withdrawn",
        "skipped_stage_1", "skipped_stage_2", "filtered_out", "scraped",
    ]
    for status in order:
        n = counts.get(status, 0)
        if n > 0:
            label = STATUS_LABELS.get(status, status)
            lines.append(f"  {label:<26} {n:>4}")

    # Submitted / active
    active = [j for j in submitted if j["status"] in ("submitted", "in_progress")]
    if active:
        lines.append(f"\nACTIVE APPLICATIONS ({len(active)})")
        lines.append("-" * 30)
        for j in active:
            days = j["days_since"]
            days_str = f"{days}d ago" if days is not None else "?"
            lines.append(f"  {j['job_title'][:30]:<30}  {j['company_name'][:22]:<22}  {days_str}")

    # Follow-up needed
    if followups:
        lines.append(f"\nFOLLOW-UP WORTH CONSIDERING ({len(followups)})")
        lines.append("-" * 30)
        for j in followups:
            days = j["days_since"]
            lines.append(f"  {j['job_title'][:30]:<30}  {j['company_name'][:22]:<22}  {days}d ago")

    # Interviews
    interviews = [j for j in submitted if j["status"] == "interview"]
    if interviews:
        lines.append(f"\nINTERVIEWS BOOKED 🎉 ({len(interviews)})")
        lines.append("-" * 30)
        for j in interviews:
            lines.append(f"  {j['job_title'][:30]:<30}  {j['company_name']}")

    # Rejections
    rejected = [j for j in submitted if j["status"] == "rejected"]
    if rejected:
        lines.append(f"\nREJECTED ({len(rejected)})")
        lines.append("-" * 30)
        for j in rejected:
            lines.append(f"  {j['job_title'][:30]:<30}  {j['company_name']}")

    if updated:
        lines.append(f"\n  [{updated} application(s) auto-marked no_response today]")

    lines.append("\n" + "=" * 56)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Streamlit dashboard
# ---------------------------------------------------------------------------

try:
    import streamlit as st
    _STREAMLIT_AVAILABLE = True
except ImportError:
    _STREAMLIT_AVAILABLE = False


def run_dashboard() -> None:
    if not _STREAMLIT_AVAILABLE:
        print("Streamlit not installed. Run: pip install streamlit")
        return

    st.set_page_config(
        page_title="Tracker — Auto Job Apply",
        page_icon="📊",
        layout="wide",
    )

    st.markdown("""
    <style>
      .block-container { padding-top: 1.5rem; }
      .metric-card {
        background:#f8fafc; border:1px solid #e5e7eb; border-radius:8px;
        padding:14px 18px; text-align:center;
      }
      .metric-num { font-size:2rem; font-weight:700; color:#111827; line-height:1.1; }
      .metric-lbl { font-size:0.78rem; color:#6b7280; margin-top:3px; }
      .status-pill {
        display:inline-block; padding:2px 10px; border-radius:99px;
        font-size:0.78rem; font-weight:600;
      }
      .pill-submitted  { background:#dbeafe; color:#1e40af; }
      .pill-interview  { background:#dcfce7; color:#166534; }
      .pill-rejected   { background:#fee2e2; color:#991b1b; }
      .pill-noresponse { background:#fef9c3; color:#854d0e; }
      .pill-other      { background:#f3f4f6; color:#374151; }
      table { width:100%; border-collapse:collapse; }
      th { font-size:0.75rem; color:#6b7280; text-transform:uppercase;
           letter-spacing:1px; border-bottom:2px solid #e5e7eb; padding:6px 8px; text-align:left; }
      td { font-size:0.85rem; color:#374151; padding:7px 8px;
           border-bottom:1px solid #f1f5f9; vertical-align:top; }
    </style>
    """, unsafe_allow_html=True)

    initialise_database()

    # Auto-update no_response on page load
    updated = auto_update_no_response()

    st.title("📊 Application Tracker")
    if updated:
        st.info(f"{updated} application(s) auto-marked as no_response today.")

    counts    = get_pipeline_counts()
    submitted = get_submitted_jobs()
    followups = get_followup_needed()

    # ── Top metrics ──
    total_submitted = counts.get("submitted", 0) + counts.get("in_progress", 0)
    total_interviews = counts.get("interview", 0)
    total_rejected   = counts.get("rejected", 0)
    total_noresponse = counts.get("no_response", 0)
    pending = counts.get("pending_stage_1", 0) + counts.get("pending_stage_2", 0)

    m = st.columns(5)
    for col, (num, lbl) in zip(m, [
        (total_submitted,  "Active"),
        (total_interviews, "Interviews"),
        (total_rejected,   "Rejected"),
        (total_noresponse, "No Response"),
        (pending,          "Pending Review"),
    ]):
        col.markdown(
            f'<div class="metric-card"><div class="metric-num">{num}</div>'
            f'<div class="metric-lbl">{lbl}</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Tabs ──
    tab_active, tab_followup, tab_funnel, tab_update = st.tabs([
        "📤 Submitted", "💬 Follow-up", "🔢 Funnel", "✏️ Update Status"
    ])

    # --- Submitted tab ---
    with tab_active:
        if not submitted:
            st.caption("No submitted applications yet.")
        else:
            def _pill(status):
                cls = {
                    "submitted":   "pill-submitted",
                    "interview":   "pill-interview",
                    "rejected":    "pill-rejected",
                    "no_response": "pill-noresponse",
                }.get(status, "pill-other")
                lbl = STATUS_LABELS.get(status, status)
                return f'<span class="status-pill {cls}">{lbl}</span>'

            rows_html = ""
            for j in submitted:
                days = j["days_since"]
                days_str = f"{days}d" if days is not None else "—"
                ref  = j.get("application_ref") or "—"
                rows_html += (
                    f"<tr><td>{j['job_title']}</td>"
                    f"<td>{j['company_name']}</td>"
                    f"<td>{j.get('work_setup','') or '—'}</td>"
                    f"<td>{days_str}</td>"
                    f"<td>{ref}</td>"
                    f"<td>{_pill(j['status'])}</td></tr>"
                )

            st.markdown(f"""
            <table>
              <thead><tr>
                <th>Role</th><th>Company</th><th>Setup</th>
                <th>Days</th><th>Ref</th><th>Status</th>
              </tr></thead>
              <tbody>{rows_html}</tbody>
            </table>""", unsafe_allow_html=True)

    # --- Follow-up tab ---
    with tab_followup:
        if not followups:
            st.success("Nothing needs following up right now.")
        else:
            st.caption(
                f"{len(followups)} application(s) with no response after {FOLLOW_UP_DAYS}+ days. "
                "A short, polite follow-up email is worth sending."
            )
            for j in followups:
                days = j["days_since"]
                with st.container():
                    c1, c2, c3 = st.columns([3, 2, 1])
                    c1.markdown(f"**{j['job_title']}**  \n{j['company_name']}")
                    c2.caption(f"Applied {days} days ago")
                    if j.get("source_url"):
                        c3.link_button("View listing", j["source_url"])
                st.divider()

    # --- Funnel tab ---
    with tab_funnel:
        st.markdown("### Full Pipeline")
        order = [
            "pending_stage_1", "approved_stage_1", "queued",
            "pending_stage_2", "approved",
            "in_progress", "submitted", "no_response", "interview",
            "rejected", "withdrawn",
            "skipped_stage_1", "skipped_stage_2", "filtered_out", "scraped",
        ]
        total = sum(counts.values()) or 1
        for status in order:
            n = counts.get(status, 0)
            label = STATUS_LABELS.get(status, status)
            pct = n / total * 100
            c1, c2, c3 = st.columns([3, 1, 6])
            c1.markdown(label)
            c2.markdown(f"**{n}**")
            c3.progress(min(pct / 100, 1.0))

    # --- Update status tab ---
    with tab_update:
        st.caption("Manually update an application status — e.g. when you get an email response.")
        conn = get_connection()
        active_rows = conn.execute("""
            SELECT id, job_title, company_name, status
            FROM jobs
            WHERE status NOT IN ('scraped','filtered_out','pending_stage_1','approved_stage_1','queued','pending_stage_2','skipped_stage_1','skipped_stage_2')
            ORDER BY submitted_at DESC NULLS LAST
            LIMIT 50
        """).fetchall()
        conn.close()

        if not active_rows:
            st.caption("No applications to update yet.")
        else:
            options = {
                f"[{r['id']}] {r['job_title']} @ {r['company_name']} ({r['status']})": r["id"]
                for r in active_rows
            }
            selected_label = st.selectbox("Select application", list(options.keys()))
            selected_id    = options[selected_label]
            new_status     = st.selectbox(
                "New status",
                ["submitted", "no_response", "interview", "rejected", "withdrawn"],
            )
            if st.button("Update status", type="primary"):
                update_status(selected_id, new_status)
                st.success(f"Updated job {selected_id} → {new_status}")
                st.rerun()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    # Detect if Streamlit is running us (it passes its own argv)
    if any("streamlit" in arg for arg in sys.argv):
        run_dashboard()
        sys.exit(0)

    parser = argparse.ArgumentParser(description="Application tracker")
    parser.add_argument("--digest",  action="store_true", help="Print daily digest and auto-update statuses")
    parser.add_argument("--update",  nargs=2, metavar=("JOB_ID", "STATUS"),
                        help="Manually set a job's status, e.g. --update 56 interview")
    parser.add_argument("--dashboard", action="store_true", help="Launch Streamlit dashboard")
    args = parser.parse_args()

    if args.update:
        job_id, status = int(args.update[0]), args.update[1]
        update_status(job_id, status)

    elif args.dashboard:
        import subprocess
        subprocess.run([sys.executable, "-m", "streamlit", "run", __file__])

    else:
        # Default: print digest
        print(generate_digest())
