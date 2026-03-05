"""Cross-subject signal detection — find geographically related opportunities."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Prefixes to strip when normalizing geographic names
_STRIP_PREFIXES = [
    "city of ", "town of ", "village of ", "township of ",
    "borough of ", "county of ",
    "unincorporated ", "greater ", "metro ", "new ",
]
_STRIP_SUFFIXES = [
    " county", " parish", " borough", " township",
    " city", " town", " village", " cdp", " area",
]


def detect_cross_signals(
    infra_opps: list[dict], rezone_opps: list[dict]
) -> list[dict]:
    """Find infrastructure + rezoning opportunities in the same geography.

    Matches by: same state AND (same county OR same city).

    Args:
        infra_opps: Kept opportunities from infrastructure subject.
        rezone_opps: Kept opportunities from rezoning subject.

    Returns:
        List of cross_signal dicts sorted by cross_signal_score descending.
    """
    if not infra_opps or not rezone_opps:
        logger.info(
            "Cross-signal skipped: infra=%d, rezone=%d",
            len(infra_opps), len(rezone_opps),
        )
        return []

    cross_signals = []

    for infra in infra_opps:
        for rezone in rezone_opps:
            if _geographic_match(infra, rezone):
                cross_signal = _build_cross_signal(infra, rezone)
                cross_signals.append(cross_signal)

    # Deduplicate — same infra+rezone pair shouldn't appear twice
    seen = set()
    unique = []
    for cs in cross_signals:
        key = (cs["infrastructure"]["headline"], cs["rezoning"]["headline"])
        if key not in seen:
            seen.add(key)
            unique.append(cs)

    unique.sort(key=lambda x: x["cross_signal_score"], reverse=True)

    logger.info(
        "Cross-signal detection: %d infrastructure x %d rezoning → %d matches",
        len(infra_opps), len(rezone_opps), len(unique),
    )
    return unique


def _normalize_geo(name: str) -> str:
    """Normalize a geographic name for fuzzy matching."""
    if not name:
        return ""
    name = name.lower().strip()
    for prefix in _STRIP_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    for suffix in _STRIP_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    # Remove extra whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _geographic_match(opp_a: dict, opp_b: dict) -> bool:
    """Check if two opportunities are in the same geography.

    Match criteria: same state AND (same county OR same city).
    """
    state_a = (opp_a.get("state") or "").upper().strip()
    state_b = (opp_b.get("state") or "").upper().strip()

    if not state_a or not state_b or state_a != state_b:
        return False

    city_a = _normalize_geo(opp_a.get("city", ""))
    city_b = _normalize_geo(opp_b.get("city", ""))

    county_a = _normalize_geo(opp_a.get("county", ""))
    county_b = _normalize_geo(opp_b.get("county", ""))

    # Match on city
    if city_a and city_b and city_a == city_b:
        return True

    # Match on county
    if county_a and county_b and county_a == county_b:
        return True

    # Substring fallback: catches "Springfield" matching "Springfield Township"
    if (city_a and city_b and len(city_a) >= 5 and len(city_b) >= 5
            and (city_a in city_b or city_b in city_a)):
        return True

    return False


def _build_cross_signal(infra: dict, rezone: dict) -> dict:
    """Build a cross-signal object from matched infrastructure + rezoning."""
    infra_score = infra.get("score", 0)
    rezone_score = rezone.get("score", 0)

    # Handle both int and float scores
    try:
        infra_score = float(infra_score)
    except (ValueError, TypeError):
        infra_score = 0
    try:
        rezone_score = float(rezone_score)
    except (ValueError, TypeError):
        rezone_score = 0

    # Cross-signal score: average + 1 bonus, capped at 10
    cross_score = min(10, round((infra_score + rezone_score) / 2 + 1, 1))

    city = infra.get("city") or rezone.get("city") or ""
    county = infra.get("county") or rezone.get("county") or ""
    state = infra.get("state") or rezone.get("state") or ""

    narrative = (
        f"Infrastructure investment ({infra.get('classification', 'unknown')}) "
        f"and rezoning ({rezone.get('classification', 'unknown')}) "
        f"detected in {city}, {state}. "
        f"Infrastructure: {infra.get('headline', '')}. "
        f"Rezoning: {rezone.get('headline', '')}. "
        f"The combination of new infrastructure capacity and increased "
        f"zoning density in the same area significantly amplifies the "
        f"land acquisition opportunity."
    )

    return {
        "infrastructure": infra,
        "rezoning": rezone,
        "cross_signal_score": cross_score,
        "cross_signal_narrative": narrative,
        "city": city,
        "county": county,
        "state": state,
    }


def load_latest_opportunities(
    subject_slug: str, date: str | None = None,
    reports_dir: Path | None = None,
) -> list[dict]:
    """Load the most recent JSON export for a subject.

    Args:
        subject_slug: "infrastructure" or "rezoning".
        date: Date string YYYY-MM-DD. Defaults to today.
        reports_dir: Override path to reports directory.

    Returns:
        List of opportunity dicts, or empty list if not found.
    """
    if reports_dir is None:
        reports_dir = Path(__file__).parent.parent / "reports"

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    json_path = reports_dir / subject_slug / f"{date}.json"

    if not json_path.exists():
        logger.info("No %s opportunities found for %s at %s",
                     subject_slug, date, json_path)
        return []

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            logger.info("Loaded %d %s opportunities for %s",
                         len(data), subject_slug, date)
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load %s: %s", json_path, e)

    return []


def save_cross_signals(
    cross_signals: list[dict], date: str | None = None,
    reports_dir: Path | None = None,
) -> Path:
    """Save cross-signals as JSON for Stage 2 consumption."""
    if reports_dir is None:
        reports_dir = Path(__file__).parent.parent / "reports"

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    out_dir = reports_dir / "cross-signals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.json"

    out_path.write_text(
        json.dumps(cross_signals, indent=2, default=str), encoding="utf-8"
    )
    logger.info("Saved %d cross-signals to %s", len(cross_signals), out_path)
    return out_path
