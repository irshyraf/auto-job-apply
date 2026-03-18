"""
pages/dashboard.py — Dashboard page

Shows:
  - 'Scrape now' button (the ONLY way to trigger a scrape)
  - 4 metric cards: awaiting review, submitted this week, total applied, API spend
  - Budget warning banners
  - Recent activity table (last 10 jobs)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from database import get_connection, get_monthly_spend


# ---------------------------------------------------------------------------
# CSS helpers
# ---------------------------------------------------------------------------

CARD_CSS = """
<style>
.metric-card {
    background: #f8fafc;
    border-radius: 8px;
    padding: 16px 20px;
    border: 1px solid #e2e8f0;
}
.metric-label { font-size: 12px; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.metric-value { font-size: 28px; font-weight: 700; margin-top: 4px; }
.metric-amber { color: #d97706; }
.metric-green { color: #16a34a; }
.metric-red   { color: #dc2626; }
.metric-grey  { color: #334155; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; font-weight: 600; }
.badge-green  { background:#dcfce7; color:#16a34a; }
.badge-amber  { background:#fef3c7; color:#d97706; }
.badge-blue   { background:#dbeafe; color:#1d4ed8; }
.badge-grey   { background:#f1f5f9; color:#475569; }
.badge-red    { background:#fee2e2; color:#dc2626; }
</style>
"""

STATUS_BADGES = {
    "new":             ('<span class="badge badge-grey">New</span>',),
    "matched":         ('<span class="badge badge-yellow">Matched</span>',),
    "submitted":       ('<span class="badge badge-green">Submitted</span>',),
    "pending_stage_1": ('<span class="badge badge-amber">Stage 1 review</span>',),
    "pending_stage_2": ('<span class="badge badge-amber">Stage 2 review</span>',),
    "approved_stage_1":('<span class="badge badge-blue">Approved S1</span>',),
    "researched":      ('<span class="badge badge-yellow">Researched</span>',),
    "skipped_stage_1": ('<span class="badge badge-grey">Skipped</span>',),
    "skipped_stage_2": ('<span class="badge badge-grey">Skipped</span>',),
    "in_progress":     ('<span class="badge badge-blue">Submitting…</span>',),
    "interview":       ('<span class="badge badge-green">Interview</span>',),
    "no_response":     ('<span class="badge badge-grey">No response</span>',),
    "rejected":        ('<span class="badge badge-red">Rejected</span>',),
    "withdrawn":       ('<span class="badge badge-grey">Withdrawn</span>',),
    "queued":          ('<span class="badge badge-grey">Queued</span>',),
    "expired":         ('<span class="badge badge-grey">Expired</span>',),
}


def _badge(status: str) -> str:
    entry = STATUS_BADGES.get(status)
    if entry:
        return entry[0]
    return f'<span class="badge badge-grey">{status}</span>'


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def _get_metrics() -> dict:
    conn = get_connection()
    from datetime import datetime, timedelta
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    awaiting = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('pending_stage_1','pending_stage_2')"
    ).fetchone()[0]
    this_week = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='submitted' AND submitted_at >= ?", (week_ago,)
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='submitted'"
    ).fetchone()[0]
    last_scrape_row = conn.execute(
        "SELECT date_scraped FROM jobs ORDER BY date_scraped DESC LIMIT 1"
    ).fetchone()
    new_matches_row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='pending_stage_1'"
    ).fetchone()
    conn.close()

    spend = get_monthly_spend()
    last_scrape = last_scrape_row["date_scraped"][:16] if last_scrape_row else "Never"
    new_matches = new_matches_row[0]

    return {
        "awaiting":    awaiting,
        "this_week":   this_week,
        "total":       total,
        "spend":       spend,
        "last_scrape": last_scrape,
        "new_matches": new_matches,
    }


@st.cache_data(ttl=30)
def _get_recent_jobs(limit: int = 10) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT job_title, company_name, date_scraped, status
        FROM jobs
        ORDER BY date_scraped DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_budget_limit() -> float:
    """Read monthly spend limit from job_board_targeting.json or return default."""
    try:
        from config_loader import job_board_targeting
        cfg = job_board_targeting()
        return float(cfg.get("cost_controls", {}).get("monthly_budget_usd", 15.0))
    except Exception:
        return 15.0


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render() -> None:
    st.markdown(CARD_CSS, unsafe_allow_html=True)

    metrics       = _get_metrics()
    budget_limit  = _get_budget_limit()
    spend         = metrics["spend"]
    spend_pct     = (spend / budget_limit * 100) if budget_limit > 0 else 0

    # Budget banners
    if spend_pct >= 100:
        st.error(
            f"Monthly budget limit reached (${spend:.2f} / ${budget_limit:.2f}). "
            "Tailoring is disabled. Increase the limit in Settings to continue.",
            icon="🚫"
        )
    elif spend_pct >= 80:
        st.warning(
            f"Approaching monthly budget limit (${spend:.2f} / ${budget_limit:.2f}). "
            "Adjust in Settings if needed.",
            icon="⚠️"
        )

    # Header row
    col_title, col_btn = st.columns([5, 1])
    with col_title:
        st.title("Dashboard")
        st.caption(
            f"Last scrape: {metrics['last_scrape']} — "
            f"{metrics['new_matches']} job(s) awaiting Stage 1 review"
        )
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        scrape_btn = st.button("Scrape now", type="primary", use_container_width=True)

    # Handle scrape
    if scrape_btn:
        budget_over = spend_pct >= 100
        if budget_over:
            st.error("Budget limit reached. Increase the limit in Settings first.")
        else:
            with st.spinner("Scraping jobs and matching…"):
                from main import run_scrape_and_match
                result = run_scrape_and_match()
            st.cache_data.clear()
            st.toast(f"Found {result['new_matches']} new match(es)", icon="✅")
            st.session_state["page"] = "new_matches"
            st.rerun()

    st.markdown("---")

    # Metric cards
    c1, c2, c3, c4 = st.columns(4)

    awaiting_colour = "metric-amber" if metrics["awaiting"] > 0 else "metric-grey"
    spend_colour    = "metric-red" if spend_pct >= 80 else "metric-green"

    with c1:
        st.markdown(f"""
            <div class="metric-card" role="region" aria-label="Awaiting review: {metrics['awaiting']} jobs">
                <div class="metric-label">Awaiting review</div>
                <div class="metric-value {awaiting_colour}">{metrics['awaiting']}</div>
            </div>
        """, unsafe_allow_html=True)

    with c2:
        st.markdown(f"""
            <div class="metric-card" role="region" aria-label="Submitted this week: {metrics['this_week']} jobs">
                <div class="metric-label">Submitted this week</div>
                <div class="metric-value metric-green">{metrics['this_week']}</div>
            </div>
        """, unsafe_allow_html=True)

    with c3:
        st.markdown(f"""
            <div class="metric-card" role="region" aria-label="Total applied: {metrics['total']} jobs">
                <div class="metric-label">Total applied</div>
                <div class="metric-value metric-grey">{metrics['total']}</div>
            </div>
        """, unsafe_allow_html=True)

    with c4:
        st.markdown(f"""
            <div class="metric-card" role="region" aria-label="API spend this month: ${spend:.2f}">
                <div class="metric-label">API spend this month</div>
                <div class="metric-value {spend_colour}">${spend:.2f}</div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Recent activity table
    st.subheader("Recent activity")
    jobs = _get_recent_jobs()

    if not jobs:
        st.info("No jobs yet. Click **Scrape now** to find matches.")
        return

    header_cols = st.columns([3, 2.5, 2, 2])
    header_cols[0].markdown("**Role**")
    header_cols[1].markdown("**Company**")
    header_cols[2].markdown("**Date**")
    header_cols[3].markdown("**Status**")
    st.markdown("---")

    for job in jobs:
        row_cols = st.columns([3, 2.5, 2, 2])
        row_cols[0].write(job["job_title"])
        row_cols[1].write(job["company_name"])
        row_cols[2].write((job["date_scraped"] or "")[:10])
        row_cols[3].markdown(_badge(job["status"]), unsafe_allow_html=True)
