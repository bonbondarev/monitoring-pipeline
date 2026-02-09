# Monitoring Pipeline

Multi-subject pipeline: Google News RSS → Anthropic API filtering → HTML report → Telegram delivery

## Project
Python 3.11+. Fetches news daily for configurable subjects (rezoning, infrastructure, etc.).
Purpose: find land acquisition opportunities before market prices them in.
Owner: LotPotential LLC

## Commands
- List subjects: `python src/main.py --list-subjects`
- Run one subject: `python src/main.py --subject rezoning`
- Run all subjects: `python src/main.py --all-subjects`
- Dry run (no API call): `python src/main.py --subject rezoning --dry-run`
- Custom date range: `python src/main.py --subject rezoning --days 7`
- Test Telegram: `python src/main.py --test-telegram`
- Lint: `ruff check src/`
- Stage 2 JSON output: `reports/<subject>/YYYY-MM-DD.json` (consumed by deal-research pipeline)

## Architecture
- src/main.py: Orchestrates full pipeline with --subject flag
- src/subject_loader.py: Loads subject config, prompt, template path from subjects/<name>/
- src/fetcher.py: Fetches Google News RSS by keyword, deduplicates, returns article list
- src/analyzer.py: Sends articles to Claude API with subject-specific system prompt, returns scored + classified JSON
- src/reporter.py: Converts JSON to formatted HTML report using dynamic template
- src/telegram_bot.py: Sends summary message + HTML file to Telegram with subject-specific header/emoji
- src/url_resolver.py: Resolves Google News redirect URLs
- src/utils.py: Retry decorator with exponential backoff
- subjects/<name>/subject.yaml: Keywords, target states, field mappings, display metadata
- subjects/<name>/prompt.md: Source of truth for all filtering logic — edit this to tune the filter
- config.yaml: Global settings only (model, retries, telegram)
- templates/default_report.html: Dynamic Jinja2 template shared by all subjects

## Adding a new subject
1. Copy subjects/_template/ to subjects/<new-name>/
2. Edit subject.yaml with keywords, states, field mappings
3. Edit prompt.md with filtering rules
4. Run: python src/main.py --subject <new-name> --dry-run
No Python code changes required.

## Non-negotiables
- NEVER commit .env or credentials
- All API calls: retry with exponential backoff
- Reports: self-contained HTML (inline CSS, no external deps)
- Log every run to logs/<subject>/ as JSON
- If Claude API fails, save raw articles to logs/failed/<subject>/
- The filter prompt in each subject's prompt.md is the ONLY place filtering logic lives
- Telegram messages must be concise — full details go in the HTML attachment
- Zero code duplication between subjects — ALL differences live in subjects/<name>/
- Adding a new subject requires ZERO Python code changes
