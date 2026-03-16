"""
config_loader.py — Central loader for all JSON configuration files.

All modules import from here rather than reading JSON files themselves.
This ensures:
  - One place to handle file-not-found errors clearly.
  - Configs are loaded once and cached.
  - Path resolution is consistent regardless of where the script is run from.
"""

import json
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "config"


def _load(filename: str) -> dict:
    """Load a JSON config file by filename. Raises clearly if missing."""
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Expected all config files to be in: {CONFIG_DIR}"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Public accessors — each returns the full parsed JSON as a dict/list.
# Use @lru_cache so each file is read from disk only once per process.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def personal_data() -> dict:
    """Aafreen's personal profile, right-to-work, screening answers, documents."""
    return _load("personal_data_vault.json")


@lru_cache(maxsize=None)
def answer_bank() -> dict:
    """STAR stories (AB-001 … AB-014) keyed by story ID."""
    return _load("answer_bank.json")


@lru_cache(maxsize=None)
def tone_voice() -> dict:
    """Writing style and tone-of-voice rules for all AI-generated text."""
    return _load("tone_voice_guide.json")


@lru_cache(maxsize=None)
def target_profile() -> dict:
    """Target job titles, dealbreakers, salary range, and CV variant mapping."""
    return _load("target_profile.json")


@lru_cache(maxsize=None)
def cv_tailoring_prompt() -> dict:
    """System prompt, API config, and validation rules for CV tailoring (Module 3)."""
    return _load("cv_tailoring_prompt.json")


@lru_cache(maxsize=None)
def question_classification_rules() -> dict:
    """Tier 1–4 classification rules and answer generation config (Module 4)."""
    return _load("question_classification_rules.json")


@lru_cache(maxsize=None)
def review_gate_ux() -> dict:
    """Review Gate UI layout, card design, and interaction rules (Module 5)."""
    return _load("review_gate_ux.json")


@lru_cache(maxsize=None)
def job_board_targeting() -> dict:
    """Job board list, search queries, scrape schedule, and dedup rules (Module 1)."""
    return _load("job_board_targeting.json")


def reload_all() -> None:
    """
    Clear the lru_cache and force all configs to be re-read from disk.
    Useful during development or if config files are updated at runtime.
    """
    personal_data.cache_clear()
    answer_bank.cache_clear()
    tone_voice.cache_clear()
    target_profile.cache_clear()
    cv_tailoring_prompt.cache_clear()
    question_classification_rules.cache_clear()
    review_gate_ux.cache_clear()
    job_board_targeting.cache_clear()


# ---------------------------------------------------------------------------
# Quick sanity-check — run this file directly to verify all configs load.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    loaders = {
        "personal_data_vault.json":         personal_data,
        "answer_bank.json":                 answer_bank,
        "tone_voice_guide.json":            tone_voice,
        "target_profile.json":             target_profile,
        "cv_tailoring_prompt.json":         cv_tailoring_prompt,
        "question_classification_rules.json": question_classification_rules,
        "review_gate_ux.json":              review_gate_ux,
        "job_board_targeting.json":         job_board_targeting,
    }

    all_ok = True
    for name, loader in loaders.items():
        try:
            data = loader()
            print(f"  OK  {name}")
        except Exception as e:
            print(f"  FAIL  {name} — {e}")
            all_ok = False

    if all_ok:
        print("\nAll config files loaded successfully.")
    else:
        print("\nSome config files failed to load. Check the errors above.")
