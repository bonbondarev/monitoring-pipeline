"""HTML report generator using Jinja2 templates — subject-agnostic."""

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)


def generate_report(
    analyzed_articles: list[dict],
    min_score: int = 5,
    subject_name: str = "Monitor",
    subject_slug: str = "",
    template_path: Path | None = None,
    custom_fields: dict | None = None,
) -> Path | None:
    """Generate HTML report from analyzed articles.

    Args:
        analyzed_articles: List of dicts from analyzer with decision, score, etc.
        min_score: Minimum score threshold for kept articles.
        subject_name: Display name for the report header/footer.
        subject_slug: Subject slug for output directory namespacing.
        template_path: Path to the Jinja2 template file.
        custom_fields: Dict with change_block config from subject.yaml.

    Returns:
        Path to the generated report file, or None if no articles.
    """
    if not analyzed_articles:
        logger.warning("No analyzed articles to report on")
        return None

    # Split kept/killed
    kept = [
        a
        for a in analyzed_articles
        if a.get("decision") == "KEEP" and a.get("score", 0) >= min_score
    ]
    killed = [a for a in analyzed_articles if a not in kept]

    # Sort kept by score descending
    kept.sort(key=lambda a: a.get("score", 0), reverse=True)

    # Classification breakdown for kept articles
    classification_counts = dict(Counter(a.get("classification", "") for a in kept))

    # Setup Jinja2 — use the template's parent dir as the loader root
    if template_path is None:
        template_path = Path(__file__).parent.parent / "templates" / "default_report.html"

    template_dir = template_path.parent
    template_name = template_path.name
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template(template_name)

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    # Extract change_block for template
    change_block = (custom_fields or {}).get("change_block")

    html = template.render(
        date=date_str,
        subject_name=subject_name,
        scanned_count=len(analyzed_articles),
        kept_count=len(kept),
        killed_count=len(killed),
        classification_counts=classification_counts,
        kept_articles=kept,
        killed_articles=killed,
        change_block=change_block,
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )

    # Save report to reports/<subject>/
    project_root = Path(__file__).parent.parent
    if subject_slug:
        out_dir = project_root / "reports" / subject_slug
    else:
        out_dir = project_root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{date_str}.html"
    report_path.write_text(html, encoding="utf-8")

    logger.info(
        "Report saved to %s (%d kept, %d killed)", report_path, len(kept), len(killed)
    )
    return report_path
