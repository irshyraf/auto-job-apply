"""
app.py — Unified Streamlit frontend for the Automated Job Application Engine.

Single entry point: streamlit run app.py

Pages:
  Dashboard         — metrics overview + 'Scrape now' button
  New matches       — Stage 1 review (approve/skip before API cost)
  Review content    — Stage 2 review (tailored CV, answers, cover letter)
  Application tracker — full history and status tracking
  Settings          — configure scraping, filters, and budget limits
"""

import sys
from pathlib import Path

# Ensure project root is on path so modules import correctly
sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st

# ---------------------------------------------------------------------------
# Page configuration (must be first Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Job engine",
    page_icon="⚙",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Accessibility: Add language attribute and skip-nav link
# ---------------------------------------------------------------------------

st.markdown(
    '<html lang="en">',
    unsafe_allow_html=True,
)

st.markdown(
    '<a href="#main-content" style="position: absolute; top: -40px; left: 0; background: #000; color: #fff; padding: 8px; text-decoration: none; z-index: 100;" tabindex="0">Skip to main content</a>',
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Load badge counts for sidebar (pending_stage_1 and pending_stage_2)
# ---------------------------------------------------------------------------

def _get_badge_counts() -> dict:
    try:
        from database import get_connection
        conn = get_connection()
        rows = conn.execute("""
            SELECT status, COUNT(*) as n FROM jobs
            WHERE status IN ('pending_stage_1','pending_stage_2')
            GROUP BY status
        """).fetchall()
        conn.close()
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "stage1": counts.get("pending_stage_1", 0),
            "stage2": counts.get("pending_stage_2", 0),
        }
    except Exception:
        return {"stage1": 0, "stage2": 0}


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

badges = _get_badge_counts()

with st.sidebar:
    st.markdown("## Job engine")
    st.markdown("---")

    pages = {
        "Dashboard":             "dashboard",
        "New matches":           "new_matches",
        "Review content":        "review_content",
        "Application tracker":   "application_tracker",
        "Settings":              "settings",
    }

    # Build labels with optional badge counts
    def _label(name: str, key: str) -> str:
        if key == "new_matches" and badges["stage1"] > 0:
            return f"{name}  **{badges['stage1']}**"
        if key == "review_content" and badges["stage2"] > 0:
            return f"{name}  **{badges['stage2']}**"
        return name

    if "page" not in st.session_state:
        st.session_state["page"] = "dashboard"

    for name, key in pages.items():
        if key in ("settings",):
            st.markdown("---")
        label = _label(name, key)
        if st.button(label, key=f"nav_{key}", use_container_width=True):
            st.session_state["page"] = key
            st.rerun()

# ---------------------------------------------------------------------------
# Route to selected page
# ---------------------------------------------------------------------------

page = st.session_state.get("page", "dashboard")

if page == "dashboard":
    from pages.dashboard import render
    render()
elif page == "new_matches":
    from pages.new_matches import render
    render()
elif page == "review_content":
    from pages.review_content import render
    render()
elif page == "application_tracker":
    from pages.application_tracker import render
    render()
elif page == "settings":
    from pages.settings import render
    render()
