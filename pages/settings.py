"""
pages/settings.py — Settings

Adjust scraping, filtering, and budget parameters without touching code or JSON files.
Save writes updated values back to the relevant config JSON files.
"""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from database import get_monthly_spend

CONFIG_DIR = Path(__file__).parent.parent / "config"


# ---------------------------------------------------------------------------
# Config read/write helpers
# ---------------------------------------------------------------------------

def _read_json(filename: str) -> dict:
    path = CONFIG_DIR / filename
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(filename: str, data: dict) -> None:
    path = CONFIG_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_nested(data: dict, *keys, default=None):
    """Safely traverse nested keys."""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key, default)
        if data is None:
            return default
    return data


def _set_nested(data: dict, value, *keys) -> dict:
    """Set a nested key, creating intermediate dicts as needed."""
    import copy
    result = copy.deepcopy(data)
    node = result
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value
    return result


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("Settings")

    try:
        jbt = _read_json("job_board_targeting.json")
    except Exception as e:
        st.error(f"Could not load job_board_targeting.json: {e}")
        return

    spend = get_monthly_spend()

    # Read current values
    current_job_cap   = _get_nested(jbt, "scrape_schedule", "job_cap", "max_new_matches_per_run", default=10)
    current_min_sal   = _get_nested(jbt, "filters", "salary", "minimum_gbp", default=28000)
    current_location  = _get_nested(jbt, "filters", "location", default="London")

    # Budget limit is stored locally in session_state (not in config) — default $15
    if "budget_limit" not in st.session_state:
        # Try to read from job_board_targeting cost_controls
        st.session_state["budget_limit"] = float(
            _get_nested(jbt, "cost_controls", "monthly_budget_usd", default=15.0)
        )

    changed = False

    # --- Scraping ---
    st.markdown("### Scraping")
    with st.container():
        st.caption("Scraping is triggered manually from the Dashboard. No schedule — you click 'Scrape now' when you want fresh jobs.")
        new_cap = st.number_input(
            "Max jobs per scrape",
            min_value=1, max_value=30,
            value=int(current_job_cap),
            step=1,
            help="Top N matches shown in Stage 1 per scrape run. Higher = more choice, more API cost at tailoring."
        )

    # --- Job filters ---
    st.markdown("### Job filters")
    with st.container():
        sal_col, loc_col = st.columns(2)
        with sal_col:
            new_min_sal = st.number_input(
                "Minimum salary (£)",
                min_value=0, max_value=200000,
                value=int(current_min_sal),
                step=1000,
                help="Jobs below this salary are filtered out at the scraping stage."
            )
        with loc_col:
            new_location = st.text_input(
                "Location",
                value=str(current_location),
                help="Only jobs matching this location string will be kept."
            )

    # --- Budget ---
    st.markdown("### Budget")
    with st.container():
        budget_col1, budget_col2 = st.columns(2)
        with budget_col1:
            new_budget = st.number_input(
                "Monthly spend limit ($)",
                min_value=1, max_value=500,
                value=int(st.session_state["budget_limit"]),
                step=1,
                help="Tailoring is automatically paused when this limit is reached."
            )
        with budget_col2:
            pct = (spend / new_budget * 100) if new_budget > 0 else 0
            colour = "red" if pct >= 80 else "green"
            st.markdown(f"""
                <div style="padding-top:28px; font-size:14px; color:{colour};">
                    Spent this month: <strong>${spend:.2f}</strong>
                    &nbsp;({pct:.0f}% of limit)
                </div>
            """, unsafe_allow_html=True)

    st.markdown("---")

    if st.button("Save changes", type="primary"):
        try:
            # Update job_board_targeting.json
            jbt_updated = _set_nested(jbt, int(new_cap), "scrape_schedule", "job_cap", "max_new_matches_per_run")
            jbt_updated = _set_nested(jbt_updated, int(new_min_sal), "filters", "salary", "minimum_gbp")
            jbt_updated = _set_nested(jbt_updated, new_location.strip(), "filters", "location")
            jbt_updated = _set_nested(jbt_updated, float(new_budget), "cost_controls", "monthly_budget_usd")
            _write_json("job_board_targeting.json", jbt_updated)

            # Update session state budget
            st.session_state["budget_limit"] = float(new_budget)

            # Bust config_loader lru_cache so new values are picked up immediately
            try:
                from config_loader import job_board_targeting
                job_board_targeting.cache_clear()
            except Exception:
                pass

            st.toast("Settings saved.", icon="✅")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to save settings: {e}")
