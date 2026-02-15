"""Pipeline orchestrator: fetch -> analyze -> report -> deliver.

Supports multiple subjects via --subject flag. Each subject has its own
keywords, prompt, and field mappings under subjects/<name>/.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path so `python src/main.py` works
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.analyzer import analyze_articles, analyze_articles_batch  # noqa: E402
from src.fetcher import fetch_all_articles  # noqa: E402
from src.reporter import generate_report  # noqa: E402
from src.subject_loader import list_subjects, load_subject  # noqa: E402
from src.telegram_bot import TelegramDelivery  # noqa: E402

logger = logging.getLogger(__name__)

PROJECT_ROOT = _PROJECT_ROOT


def setup_logging(verbose: bool = False) -> None:
    """Configure logging format and level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def save_run_log(log_data: dict, subject_slug: str = "") -> Path:
    """Save structured JSON log for this pipeline run."""
    if subject_slug:
        log_dir = PROJECT_ROOT / "logs" / subject_slug
    else:
        log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    log_path = log_dir / filename
    log_path.write_text(json.dumps(log_data, indent=2, default=str), encoding="utf-8")
    return log_path


def _get_telegram_bot(
    subject_name: str = "Monitor", subject_emoji: str = ""
) -> TelegramDelivery | None:
    """Create TelegramDelivery instance from env vars, or None if missing."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        return None
    return TelegramDelivery(token, chat_id,
                            subject_name=subject_name, subject_emoji=subject_emoji)


def run_pipeline(
    subject: dict, days_override: int | None = None, dry_run: bool = False,
    use_batch_api: bool = False,
) -> dict:
    """Execute the full pipeline for a single subject. Returns run summary dict."""
    config = subject["config"]
    subject_name = subject["name"]
    subject_slug = subject["slug"]
    subject_emoji = subject["emoji"]

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    run_data = {
        "start_time": now.isoformat(),
        "date": date_str,
        "subject": subject_slug,
        "dry_run": dry_run,
        "errors": [],
    }

    # --- FETCH ---
    keywords = config.get("keywords", [])
    lookback = days_override or config.get("days_lookback", 1)
    max_articles = config.get("max_articles_per_run", 100)

    if not keywords:
        logger.warning("No keywords configured for subject '%s'", subject_slug)
        run_data["articles_fetched"] = 0
        run_data["end_time"] = datetime.now().isoformat()
        return run_data

    logger.info(
        "[%s] Fetching articles for %d keywords (lookback=%dd, max=%d)",
        subject_slug,
        len(keywords),
        lookback,
        max_articles,
    )
    articles = fetch_all_articles(
        keywords, lookback_days=lookback, max_articles=max_articles
    )
    run_data["articles_fetched"] = len(articles)

    if not articles:
        logger.info("[%s] No articles fetched from any keyword", subject_slug)
        run_data["articles_kept"] = 0
        run_data["articles_killed"] = 0

        if not dry_run and config.get("telegram_enabled"):
            bot = _get_telegram_bot(subject_name, subject_emoji)
            if bot:
                stats = {
                    "date": date_str,
                    "total_scanned": 0,
                    "kept_count": 0,
                    "killed_count": 0,
                }
                try:
                    bot.send_no_results(stats)
                except Exception as e:
                    logger.error("Telegram delivery failed: %s", e)

        run_data["end_time"] = datetime.now().isoformat()
        return run_data

    # --- DRY RUN ---
    if dry_run:
        logger.info("[%s] Dry run mode — printing %d articles to console",
                     subject_slug, len(articles))
        for i, article in enumerate(articles, 1):
            print(
                f"\n[{i}] {article['title']}\n"
                f"    Source: {article['source']}\n"
                f"    URL: {article['url']}\n"
                f"    Published: {article['published']}\n"
                f"    Keyword: {article['keyword']}"
            )
        run_data["articles_kept"] = 0
        run_data["articles_killed"] = 0
        run_data["end_time"] = datetime.now().isoformat()
        return run_data

    # --- ANALYZE ---
    model = config.get("model", "claude-sonnet-4-20250514")
    api_mode = "Batch API (50% discount)" if use_batch_api else "standard API"
    logger.info("[%s] Analyzing %d articles with %s via %s",
                subject_slug, len(articles), model, api_mode)

    custom_fields = subject.get("custom_fields", {})
    extra_fields = custom_fields.get("extra_fields", [])

    analyze_fn = analyze_articles_batch if use_batch_api else analyze_articles
    analyzed = analyze_fn(
        articles,
        system_prompt=subject["system_prompt"],
        model=model,
        extra_fields=extra_fields,
        subject_slug=subject_slug,
    )

    # Extract token usage metadata appended by analyzer
    token_usage = None
    if analyzed and isinstance(analyzed[-1], dict) and "_token_usage" in analyzed[-1]:
        token_usage = analyzed.pop()["_token_usage"]
        run_data["token_usage"] = token_usage
        logger.info(
            "[%s] API cost estimate: input=%d tokens, output=%d tokens, "
            "cache_read=%d tokens",
            subject_slug,
            token_usage.get("input_tokens", 0),
            token_usage.get("output_tokens", 0),
            token_usage.get("cache_read_input_tokens", 0),
        )

    run_data["articles_analyzed"] = len(analyzed)

    min_score = config.get("min_opportunity_score", 5)
    kept = [
        a
        for a in analyzed
        if a.get("decision") == "KEEP" and a.get("score", 0) >= min_score
    ]
    killed = [a for a in analyzed if a not in kept]
    kept.sort(key=lambda a: a.get("score", 0), reverse=True)

    run_data["articles_kept"] = len(kept)
    run_data["articles_killed"] = len(killed)

    logger.info("[%s] Analysis complete: %d kept, %d killed",
                subject_slug, len(kept), len(killed))

    # --- REPORT ---
    report_path = generate_report(
        analyzed,
        min_score=min_score,
        subject_name=subject_name,
        subject_slug=subject_slug,
        template_path=subject["template_path"],
        custom_fields=custom_fields,
    )
    run_data["report_path"] = str(report_path) if report_path else None

    # --- SAVE OPPORTUNITIES JSON (for Stage 2 research pipeline) ---
    if kept:
        json_dir = PROJECT_ROOT / "reports" / subject_slug
        json_dir.mkdir(parents=True, exist_ok=True)
        json_path = json_dir / f"{date_str}.json"
        opportunities = [
            {
                "headline": a.get("headline", ""),
                "source_url": a.get("source_url", a.get("url", "")),
                "classification": a.get("classification", ""),
                "score": a.get("score", 0),
                "city": a.get("city", ""),
                "state": a.get("state", ""),
                "location_details": a.get("location_details", ""),
                "stage": a.get("stage", ""),
                "initiator": a.get("initiator", ""),
                "timeline": a.get("timeline", ""),
                "reasoning": a.get("reasoning", ""),
                "next_steps": a.get("next_steps", ""),
                "subject": subject_slug,
                "date": date_str,
            }
            for a in kept
        ]
        # Include subject-specific custom fields
        if custom_fields and "extra_fields" in custom_fields:
            for opp, article in zip(opportunities, kept):
                for field_spec in custom_fields["extra_fields"]:
                    field_name = field_spec["field"]
                    opp[field_name] = article.get(field_name, "")
        json_path.write_text(json.dumps(opportunities, indent=2), encoding="utf-8")
        run_data["opportunities_json"] = str(json_path)
        logger.info("[%s] Saved %d opportunities to %s", subject_slug, len(kept), json_path)

    # --- DELIVER VIA TELEGRAM ---
    if config.get("telegram_enabled"):
        bot = _get_telegram_bot(subject_name, subject_emoji)
        if bot:
            high_priority = sum(1 for a in kept if a.get("score", 0) >= 8)
            stats = {
                "date": date_str,
                "total_scanned": len(articles),
                "kept_count": len(kept),
                "killed_count": len(killed),
                "high_priority_count": high_priority,
            }

            try:
                if kept:
                    bot.send_summary(kept, stats)
                    if report_path and report_path.exists():
                        bot.send_report(report_path)
                else:
                    bot.send_no_results(stats)
            except Exception as e:
                logger.error("Telegram delivery failed: %s", e)
                run_data["errors"].append(f"Telegram: {e}")
    else:
        logger.info("Telegram disabled in config")

    run_data["end_time"] = datetime.now().isoformat()
    return run_data


def test_telegram() -> None:
    """Send a test message to Telegram and exit."""
    bot = _get_telegram_bot()
    if not bot:
        sys.exit(1)

    success = bot.send_test()
    if success:
        logger.info("Test message sent successfully")
    else:
        logger.error("Test message failed")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Monitoring Pipeline")
    parser.add_argument(
        "--subject",
        type=str,
        default=None,
        help="Subject slug to run (e.g., rezoning, infrastructure)",
    )
    parser.add_argument(
        "--all-subjects",
        action="store_true",
        help="Run all available subjects sequentially",
    )
    parser.add_argument(
        "--list-subjects",
        action="store_true",
        help="List all available subjects and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch articles and print to console, skip API call and Telegram",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Override days_lookback from config",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to Telegram and exit",
    )
    parser.add_argument(
        "--batch-api",
        action="store_true",
        help="Use Anthropic Batch API for 50%% cost reduction (results may take minutes)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    # --- List subjects ---
    if args.list_subjects:
        subjects = list_subjects()
        if not subjects:
            print("No subjects found in subjects/ directory.")
            sys.exit(1)
        print("Available subjects:")
        for s in subjects:
            print(f"  {s['slug']:20s} {s['name']} — {s['description']}")
        return

    # --- Test Telegram ---
    if args.test_telegram:
        test_telegram()
        return

    # --- Validate args ---
    if not args.subject and not args.all_subjects:
        parser.error("--subject <name> or --all-subjects is required")

    # --- Run all subjects ---
    if args.all_subjects:
        subjects = list_subjects()
        if not subjects:
            logger.error("No subjects found in subjects/ directory")
            sys.exit(1)

        any_failed = False
        for s in subjects:
            slug = s["slug"]
            logger.info("=" * 60)
            logger.info("Running subject: %s", slug)
            logger.info("=" * 60)
            try:
                subject = load_subject(slug)
                run_data = run_pipeline(
                    subject, days_override=args.days, dry_run=args.dry_run,
                    use_batch_api=args.batch_api,
                )
                log_path = save_run_log(run_data, subject_slug=slug)
                logger.info("[%s] Run log saved to %s", slug, log_path)

                print(
                    f"\n[{slug}] Pipeline complete: "
                    f"{run_data.get('articles_fetched', 0)} fetched, "
                    f"{run_data.get('articles_kept', 0)} kept, "
                    f"{run_data.get('articles_killed', 0)} killed"
                )

                if run_data.get("report_path"):
                    print(f"[{slug}] Report: {run_data['report_path']}")

                if run_data.get("errors"):
                    logger.warning("[%s] Completed with %d errors",
                                   slug, len(run_data["errors"]))
                    any_failed = True

            except KeyboardInterrupt:
                logger.info("Pipeline interrupted by user")
                sys.exit(130)
            except Exception as e:
                logger.exception("[%s] Pipeline failed: %s", slug, e)
                any_failed = True
                continue  # Continue to next subject

        if any_failed:
            sys.exit(1)
        return

    # --- Run single subject ---
    try:
        subject = load_subject(args.subject)
        run_data = run_pipeline(
            subject, days_override=args.days, dry_run=args.dry_run,
            use_batch_api=args.batch_api,
        )
        log_path = save_run_log(run_data, subject_slug=args.subject)
        logger.info("Run log saved to %s", log_path)

        print(
            f"\nPipeline complete: "
            f"{run_data.get('articles_fetched', 0)} fetched, "
            f"{run_data.get('articles_kept', 0)} kept, "
            f"{run_data.get('articles_killed', 0)} killed"
        )

        if run_data.get("report_path"):
            print(f"Report: {run_data['report_path']}")

        if run_data.get("errors"):
            logger.warning("Run completed with %d errors", len(run_data["errors"]))
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.exception("Pipeline failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
