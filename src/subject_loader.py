"""Load subject configuration, prompt, and template path."""

import logging
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUBJECTS_DIR = PROJECT_ROOT / "subjects"
GLOBAL_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "default_report.html"

# Keys that exist in global config and can be overridden per-subject
_GLOBAL_DEFAULTS = {
    "model": "claude-haiku-4-5-20250414",
    "max_retries": 3,
    "retry_delay_seconds": 5,
    "telegram_enabled": True,
    "max_articles_per_run": 100,
    "min_opportunity_score": 5,
    "days_lookback": 1,
}


def _load_global_config() -> dict:
    """Load the global config.yaml with defaults."""
    if not GLOBAL_CONFIG_PATH.exists():
        logger.warning("Global config not found at %s, using defaults", GLOBAL_CONFIG_PATH)
        return dict(_GLOBAL_DEFAULTS)

    with open(GLOBAL_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    for key, default in _GLOBAL_DEFAULTS.items():
        config.setdefault(key, default)

    return config


def load_subject(subject_slug: str) -> dict:
    """Load subject config, prompt, and template path.

    Returns dict with keys:
      - config: merged dict (subject.yaml overrides global config.yaml)
      - system_prompt: str contents of prompt.md
      - template_path: Path to report.html (subject-specific or default)
      - custom_fields: dict from subject.yaml
      - name: str display name
      - slug: str
      - emoji: str
      - description: str
    """
    subject_dir = SUBJECTS_DIR / subject_slug

    if not subject_dir.exists():
        available = [s["slug"] for s in list_subjects()]
        logger.error(
            "Subject '%s' not found. Available subjects: %s",
            subject_slug,
            ", ".join(available) or "(none)",
        )
        sys.exit(1)

    # Load subject.yaml
    subject_yaml_path = subject_dir / "subject.yaml"
    if not subject_yaml_path.exists():
        logger.error("Missing subject.yaml in %s", subject_dir)
        sys.exit(1)

    with open(subject_yaml_path, encoding="utf-8") as f:
        subject_config = yaml.safe_load(f) or {}

    # Load prompt.md
    prompt_path = subject_dir / "prompt.md"
    if not prompt_path.exists():
        logger.error("Missing prompt.md in %s", subject_dir)
        sys.exit(1)

    system_prompt = prompt_path.read_text(encoding="utf-8")

    # Determine template path: subject-specific or default
    subject_template = subject_dir / "report.html"
    template_path = subject_template if subject_template.exists() else DEFAULT_TEMPLATE_PATH

    # Merge configs: global defaults <- global config <- subject overrides
    global_config = _load_global_config()
    merged = dict(global_config)

    # Subject-level overrides for pipeline settings
    for key in ("max_articles_per_run", "min_opportunity_score", "days_lookback",
                "model", "max_retries", "retry_delay_seconds", "telegram_enabled"):
        if key in subject_config:
            merged[key] = subject_config[key]

    # Copy subject-specific keys into merged config
    merged["keywords"] = subject_config.get("keywords", [])
    merged["target_states"] = subject_config.get("target_states", [])

    custom_fields = subject_config.get("custom_fields", {})

    return {
        "config": merged,
        "system_prompt": system_prompt,
        "template_path": template_path,
        "custom_fields": custom_fields,
        "name": subject_config.get("name", subject_slug),
        "slug": subject_slug,
        "emoji": subject_config.get("emoji", ""),
        "description": subject_config.get("description", ""),
    }


def list_subjects() -> list[dict]:
    """List all available subjects (skip _template).

    Returns list of {slug, name, description} dicts.
    """
    if not SUBJECTS_DIR.exists():
        return []

    subjects = []
    for entry in sorted(SUBJECTS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue
        subject_yaml = entry / "subject.yaml"
        if not subject_yaml.exists():
            continue

        with open(subject_yaml, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        subjects.append({
            "slug": entry.name,
            "name": config.get("name", entry.name),
            "description": config.get("description", ""),
        })

    return subjects
