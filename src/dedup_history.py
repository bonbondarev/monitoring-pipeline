"""Cross-day deduplication — skip articles already seen in recent reports."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path(__file__).parent.parent / "reports"


def load_seen_urls(
    subjects: list[str], lookback_days: int = 7,
    reports_dir: Path | None = None,
) -> set[str]:
    """Load source_url values from the last N days of opportunity JSON files.

    Reads from reports/<subject>/YYYY-MM-DD.json going back lookback_days.
    Silently skips missing files.

    Args:
        subjects: List of subject slugs to scan.
        lookback_days: Number of days to look back. Default 7.
        reports_dir: Override path to reports directory.

    Returns:
        Set of URLs already seen in recent reports.
    """
    if reports_dir is None:
        reports_dir = _REPORTS_DIR

    seen: set[str] = set()
    today = datetime.now().date()

    for subject in subjects:
        for day_offset in range(1, lookback_days + 1):
            date = today - timedelta(days=day_offset)
            json_path = reports_dir / subject / f"{date.isoformat()}.json"

            if not json_path.exists():
                continue

            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for opp in data:
                        url = opp.get("source_url", "")
                        if url:
                            seen.add(url)
            except (json.JSONDecodeError, OSError):
                continue

    logger.info(
        "Cross-day dedup: loaded %d seen URLs from last %d days (%s)",
        len(seen), lookback_days, ", ".join(subjects),
    )
    return seen
