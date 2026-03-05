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

from src.analyzer import analyze_articles  # noqa: E402
from src.cross_signal import (  # noqa: E402
    detect_cross_signals,
    load_latest_opportunities,
    save_cross_signals,
)
from src.dedup import deduplicate_articles, get_dedup_stats  # noqa: E402
from src.dedup_history import load_seen_urls  # noqa: E402
from src.enrichment import enrich_articles, get_enrichment_stats  # noqa: E402
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
    use_batch_api: bool = False, skip_enrichment: bool = False,
    limit: int | None = None, skip_dedup: bool = False,
    dedup_threshold: float = 0.80, no_history_dedup: bool = False,
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

    # Apply --limit if set (process only first N articles)
    if limit and limit > 0 and len(articles) > limit:
        logger.info("[%s] Limiting to first %d articles (of %d)",
                     subject_slug, limit, len(articles))
        articles = articles[:limit]

    if not articles:
        logger.info("[%s] No articles fetched from any keyword", subject_slug)
        run_data["articles_kept"] = 0
        run_data["articles_killed"] = 0

        if not dry_run and config.get("telegram_enabled"):
            bot = _get_telegram_bot(subject_name, subject_emoji)
            if bot:
                try:
                    bot._send_message(
                        f"{bot._header(date_str)}\n\n"
                        f"0 articles fetched from Google News.\n"
                        f"Possible rate-limit or connectivity issue.\n"
                        f"Check RSS feeds manually."
                    )
                except Exception as e:
                    logger.error("Telegram delivery failed: %s", e)

        run_data["end_time"] = datetime.now().isoformat()
        return run_data

    # --- ENRICHMENT ---
    if not skip_enrichment:
        logger.info("[%s] Enriching %d articles with full text", subject_slug, len(articles))
        try:
            enrich_articles(articles)
            run_data["enrichment"] = get_enrichment_stats(articles)
        except Exception as e:
            logger.error("[%s] Enrichment failed, continuing without: %s", subject_slug, e)
            run_data["errors"].append(f"Enrichment: {e}")
    else:
        logger.info("[%s] Skipping enrichment (--skip-enrichment)", subject_slug)

    # --- DEDUPLICATION ---
    if not skip_dedup:
        pre_dedup_count = len(articles)
        logger.info("[%s] Deduplicating %d articles (threshold=%.2f)",
                     subject_slug, len(articles), dedup_threshold)
        try:
            articles = deduplicate_articles(articles, threshold=dedup_threshold)
            run_data["dedup"] = get_dedup_stats(pre_dedup_count, articles)
        except Exception as e:
            logger.error("[%s] Dedup failed, continuing without: %s", subject_slug, e)
            run_data["errors"].append(f"Dedup: {e}")
    else:
        logger.info("[%s] Skipping dedup (--skip-dedup)", subject_slug)

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
    logger.info("[%s] Analyzing %d articles with %s", subject_slug, len(articles), model)

    custom_fields = subject.get("custom_fields", {})
    extra_fields = custom_fields.get("extra_fields", [])

    analyzed, token_usage = analyze_articles(
        articles,
        system_prompt=subject["system_prompt"],
        model=model,
        extra_fields=extra_fields,
        subject_slug=subject_slug,
        use_batch_api=use_batch_api,
    )
    run_data["articles_analyzed"] = len(analyzed)
    run_data["token_usage"] = token_usage

    min_score = config.get("min_opportunity_score", 5)

    # --- CROSS-DAY DEDUP ---
    if not no_history_dedup:
        seen_urls = load_seen_urls([subject_slug])
        pre_dedup = [
            a for a in analyzed
            if a.get("decision") == "KEEP" and a.get("score", 0) >= min_score
        ]
        kept = [
            a for a in pre_dedup
            if a.get("source_url", a.get("url", "")) not in seen_urls
        ]
        history_suppressed = len(pre_dedup) - len(kept)
        if history_suppressed:
            logger.info(
                "[%s] Cross-day dedup: %d already seen in last 7 days",
                subject_slug, history_suppressed,
            )
        run_data["history_dedup_suppressed"] = history_suppressed
    else:
        kept = [
            a
            for a in analyzed
            if a.get("decision") == "KEEP" and a.get("score", 0) >= min_score
        ]
    killed = [a for a in analyzed if a not in kept]
    kept.sort(key=lambda a: a.get("score", 0), reverse=True)

    run_data["articles_kept"] = len(kept)
    run_data["articles_killed"] = len(killed)

    # Noise distribution logging
    noise_counts = {}
    for a in analyzed:
        flag = a.get("noise_flag", "NONE")
        noise_counts[flag] = noise_counts.get(flag, 0) + 1
    run_data["noise_distribution"] = noise_counts
    noise_killed = {k: v for k, v in noise_counts.items() if k != "NONE"}
    if noise_killed:
        logger.info("[%s] Noise filtered: %s", subject_slug,
                     ", ".join(f"{k}={v}" for k, v in noise_killed.items()))

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
                "profit_potential": a.get("profit_potential", 0),
                "timing": a.get("timing", 0),
                "actionability": a.get("actionability", 0),
                "confidence": a.get("confidence", 0),
                "score": a.get("score", 0),
                "city": a.get("city", ""),
                "state": a.get("state", ""),
                "location_details": a.get("location_details", ""),
                "stage": a.get("stage", ""),
                "initiator": a.get("initiator", ""),
                "timeline": a.get("timeline", ""),
                "reasoning": a.get("reasoning", ""),
                "next_steps": a.get("next_steps", ""),
                "full_text": a.get("full_text", ""),
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


def run_cross_signal(date: str | None = None, dry_run: bool = False) -> None:
    """Run cross-subject signal detection and generate report."""
    from jinja2 import Environment, FileSystemLoader

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    logger.info("Running cross-signal detection for %s", date)

    infra_opps = load_latest_opportunities("infrastructure", date)
    rezone_opps = load_latest_opportunities("rezoning", date)

    if not infra_opps:
        logger.info("No infrastructure opportunities for %s — skipping cross-signal", date)
        print(f"No infrastructure opportunities for {date}")
        return
    if not rezone_opps:
        logger.info("No rezoning opportunities for %s — skipping cross-signal", date)
        print(f"No rezoning opportunities for {date}")
        return

    cross_signals = detect_cross_signals(infra_opps, rezone_opps)

    if dry_run:
        print(f"\nCross-signal detection ({date}):")
        print(f"  Infrastructure opportunities: {len(infra_opps)}")
        print(f"  Rezoning opportunities: {len(rezone_opps)}")
        print(f"  Cross-signals found: {len(cross_signals)}")
        for cs in cross_signals:
            print(f"\n  Score {cs['cross_signal_score']}: {cs['city']}, {cs['state']}")
            print(f"    Infra: {cs['infrastructure']['headline'][:70]}")
            print(f"    Rezone: {cs['rezoning']['headline'][:70]}")
        return

    # Save cross-signals JSON
    if cross_signals:
        json_path = save_cross_signals(cross_signals, date)
        print(f"Cross-signals JSON: {json_path}")

    # Generate HTML report
    template_path = PROJECT_ROOT / "templates" / "cross_signal_report.html"
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)), autoescape=True
    )
    template = env.get_template(template_path.name)

    html = template.render(
        date=date,
        cross_signals=cross_signals,
    )

    report_dir = PROJECT_ROOT / "reports" / "cross-signals"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{date}.html"
    report_path.write_text(html, encoding="utf-8")
    logger.info("Cross-signal report: %s", report_path)
    print(f"Cross-signal report: {report_path}")

    # Send via Telegram
    bot = _get_telegram_bot("Cross-Signal", "")
    if bot:
        try:
            stats = {
                "date": date,
                "total_scanned": len(infra_opps) + len(rezone_opps),
                "kept_count": len(cross_signals),
                "killed_count": 0,
            }
            if cross_signals:
                bot.send_summary(
                    [{"headline": cs["cross_signal_narrative"][:100],
                      "score": cs["cross_signal_score"],
                      "city": cs["city"], "state": cs["state"]}
                     for cs in cross_signals],
                    stats,
                )
                bot.send_report(report_path)
            else:
                bot.send_no_results(stats)
        except Exception as e:
            logger.error("Cross-signal Telegram delivery failed: %s", e)

    print(f"\nCross-signal complete: {len(cross_signals)} signals found")


def run_weekly_summary() -> None:
    """Aggregate last 7 days of run logs and send a Telegram summary."""
    from datetime import timedelta

    today = datetime.now().date()
    subjects = list_subjects()
    if not subjects:
        logger.error("No subjects found")
        return

    # Aggregate per-subject stats
    subject_stats = {}
    total_tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    for s in subjects:
        slug = s["slug"]
        log_dir = PROJECT_ROOT / "logs" / slug
        stats = {"fetched": 0, "kept": 0, "killed": 0, "runs": 0}

        for day_offset in range(7):
            date = today - timedelta(days=day_offset)
            date_prefix = date.isoformat()
            if not log_dir.exists():
                continue
            for log_file in sorted(log_dir.glob(f"{date_prefix}_*.json")):
                try:
                    data = json.loads(log_file.read_text(encoding="utf-8"))
                    if data.get("dry_run"):
                        continue
                    stats["fetched"] += data.get("articles_fetched", 0)
                    stats["kept"] += data.get("articles_kept", 0)
                    stats["killed"] += data.get("articles_killed", 0)
                    stats["runs"] += 1
                    tu = data.get("token_usage", {})
                    for k in total_tokens:
                        total_tokens[k] += tu.get(k, 0)
                except (json.JSONDecodeError, OSError):
                    continue

        subject_stats[slug] = stats

    # Stage 2 GO/MAYBE/KILL counts
    stage2_dir = PROJECT_ROOT.parent / "deal-research" / "logs"
    go_count = maybe_count = kill_count = 0
    if stage2_dir.exists():
        for day_offset in range(7):
            date = today - timedelta(days=day_offset)
            log_file = stage2_dir / f"{date.isoformat()}.json"
            if not log_file.exists():
                continue
            try:
                data = json.loads(log_file.read_text(encoding="utf-8"))
                for r in data.get("results", []):
                    rec = r.get("research", {}).get("recommendation", "")
                    if rec == "GO":
                        go_count += 1
                    elif rec == "MAYBE":
                        maybe_count += 1
                    elif rec == "KILL":
                        kill_count += 1
            except (json.JSONDecodeError, OSError):
                continue

    # Cost estimate (claude-sonnet-4 pricing)
    input_cost = total_tokens["input_tokens"] / 1_000_000 * 3.0
    output_cost = total_tokens["output_tokens"] / 1_000_000 * 15.0
    cache_cost = total_tokens["cache_read_input_tokens"] / 1_000_000 * 0.30
    total_cost = input_cost + output_cost + cache_cost

    # Build message
    lines = ["Weekly Summary (last 7 days)", ""]
    total_fetched = total_kept = total_killed = 0
    for slug, stats in subject_stats.items():
        total_fetched += stats["fetched"]
        total_kept += stats["kept"]
        total_killed += stats["killed"]
        keep_rate = (
            f"{stats['kept'] / stats['fetched'] * 100:.0f}%"
            if stats["fetched"] > 0 else "N/A"
        )
        lines.append(f"{slug}: {stats['fetched']} fetched, {stats['kept']} kept, "
                      f"{stats['killed']} killed ({keep_rate} keep rate)")

    overall_rate = (
        f"{total_kept / total_fetched * 100:.0f}%"
        if total_fetched > 0 else "N/A"
    )
    lines.append("")
    lines.append(f"Total: {total_fetched} fetched, {total_kept} kept, "
                  f"{total_killed} killed")
    lines.append(f"Average keep rate: {overall_rate}")
    lines.append("")
    lines.append(f"Tokens: {total_tokens['input_tokens']:,} in / "
                  f"{total_tokens['output_tokens']:,} out / "
                  f"{total_tokens['cache_read_input_tokens']:,} cache-read")
    lines.append(f"Estimated cost: ${total_cost:.2f}")

    if go_count or maybe_count or kill_count:
        lines.append("")
        lines.append(f"Stage 2: {go_count} GO / {maybe_count} MAYBE / {kill_count} KILL")

    text = "\n".join(lines)
    logger.info("Weekly summary:\n%s", text)

    bot = _get_telegram_bot("Weekly Summary", "")
    if bot:
        try:
            bot._send_message(text)
            logger.info("Weekly summary sent to Telegram")
        except Exception as e:
            logger.error("Failed to send weekly summary: %s", e)
    else:
        print(text)


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
        help="Use Messages Batch API (50%% cheaper, async processing)",
    )
    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip full-text article fetching (faster runs)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N articles from RSS (for testing)",
    )
    parser.add_argument(
        "--skip-dedup",
        action="store_true",
        help="Skip embedding-based deduplication",
    )
    parser.add_argument(
        "--dedup-threshold",
        type=float,
        default=0.80,
        help="Cosine similarity threshold for dedup clustering (default: 0.80)",
    )
    parser.add_argument(
        "--no-history-dedup",
        action="store_true",
        help="Skip cross-day deduplication (for testing)",
    )
    parser.add_argument(
        "--weekly-summary",
        action="store_true",
        help="Aggregate last 7 days of logs and send Telegram summary",
    )
    parser.add_argument(
        "--cross-signal",
        action="store_true",
        help="Run cross-subject signal detection (infrastructure + rezoning overlap)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date for cross-signal detection (YYYY-MM-DD, default: today)",
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

    # --- Weekly summary ---
    if args.weekly_summary:
        run_weekly_summary()
        return

    # --- Cross-signal detection ---
    if args.cross_signal:
        run_cross_signal(date=args.date, dry_run=args.dry_run)
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
                    skip_enrichment=args.skip_enrichment,
                    limit=args.limit,
                    skip_dedup=args.skip_dedup,
                    dedup_threshold=args.dedup_threshold,
                    no_history_dedup=args.no_history_dedup,
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
            skip_enrichment=args.skip_enrichment,
            limit=args.limit,
            skip_dedup=args.skip_dedup,
            dedup_threshold=args.dedup_threshold,
            no_history_dedup=args.no_history_dedup,
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
