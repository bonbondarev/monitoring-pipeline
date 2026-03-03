# Monitoring Pipeline

Stage 1 of the LotPotential land acquisition pipeline. Fetches articles from Google News RSS, filters them through the Claude API using subject-specific intelligence prompts, generates self-contained HTML reports, and delivers daily summaries via Telegram.

Kept opportunities are exported as JSON for the [deal-research](../deal-research/) Stage 2 pipeline.

## Subjects

Each subject is a self-contained folder under `subjects/` with its own keywords, prompt, and field mappings:

| Subject | Description | Target States |
|---------|-------------|---------------|
| `rezoning` | Government-initiated zoning changes (upzoning, TOD zones, corridor rezonings) | All 50 |
| `infrastructure` | Government infrastructure investment (water/sewer extensions, roads, utility districts) | All 50 |

To add a new subject, copy `subjects/_template/` — zero Python code changes required.

## Quick Start

```bash
# Clone and set up
git clone <repo-url> && cd monitoring-pipeline
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# List available subjects
python src/main.py --list-subjects

# Dry run (fetches articles, no API call)
python src/main.py --subject rezoning --dry-run

# Full run
python src/main.py --subject rezoning

# Run all subjects
python src/main.py --all-subjects
```

## Configuration

### API Keys (.env)

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-100...
```

### Global Settings (config.yaml)

Pipeline-wide settings shared across all subjects:

```yaml
model: "claude-sonnet-4-20250514"
max_retries: 3
retry_delay_seconds: 5
telegram_enabled: true
```

### Per-Subject Settings (subjects/<name>/subject.yaml)

Each subject can override global settings and defines its own keywords, states, and field mappings. See `subjects/_template/subject.yaml` for a fully annotated example.

## Usage

```bash
# Run a specific subject
python src/main.py --subject rezoning
python src/main.py --subject infrastructure

# Run all subjects sequentially
python src/main.py --all-subjects

# Dry run — fetch articles, print to console
python src/main.py --subject rezoning --dry-run

# Override lookback period
python src/main.py --subject rezoning --days 7

# Use Batch API (50% cheaper, async processing)
python src/main.py --subject rezoning --batch-api

# Run all subjects in dry-run mode
python src/main.py --all-subjects --dry-run

# Test Telegram delivery
python src/main.py --test-telegram

# Verbose logging
python src/main.py --subject rezoning --verbose

# List available subjects
python src/main.py --list-subjects
```

## Adding a New Subject

1. Copy the template: `cp -r subjects/_template subjects/my-subject`
2. Edit `subjects/my-subject/subject.yaml`:
   - Set name, slug, emoji, description
   - Add search keywords
   - Set target states
   - Define field mappings for the report change block
3. Edit `subjects/my-subject/prompt.md`:
   - Write the intelligence prompt (KEEP/KILL rules, scoring, classification)
   - Define the JSON output format matching your field mappings
4. Test: `python src/main.py --subject my-subject --dry-run`
5. Full run: `python src/main.py --subject my-subject`

No Python code changes required.

## Architecture

```
Google News RSS
      |
      v
  fetcher.py ──── Fetches RSS per keyword, deduplicates, date-filters
      |
      v
  analyzer.py ─── Sends batches of 25 articles to Claude API with subject's prompt.md
      |            Uses prompt caching + token tracking; optional --batch-api
      v
  reporter.py ─── Renders Jinja2 HTML template with subject metadata
      |
      v
telegram_bot.py ── Sends summary + HTML attachment via Telegram Bot API
```

Pipeline orchestrated by `src/main.py` with subject loaded by `src/subject_loader.py`.

Reports: `reports/<subject>/YYYY-MM-DD.html`
JSON opportunities (Stage 2 input): `reports/<subject>/YYYY-MM-DD.json`
Run logs: `logs/<subject>/YYYY-MM-DD_HHMMSS.json`
Failed batches: `logs/failed/<subject>/`

## API Cost Optimizations

- **Prompt caching**: System prompt uses `cache_control: ephemeral`. Batch 1 creates the cache, batches 2+ read from cache at 90% discount.
- **Token usage tracking**: Every run logs input, output, cache_creation, and cache_read token counts to the run log JSON.
- **Batch API**: `--batch-api` flag submits all batches as one async job via Messages Batch API for 50% cost reduction. Best for cron jobs where latency doesn't matter.
- **Readable JSON**: Article payloads use `indent=2` formatting. Do NOT switch to compact JSON — it degrades filtering quality.

## Project Structure

```
monitoring-pipeline/
├── src/
│   ├── main.py              # Pipeline orchestrator (--subject flag)
│   ├── subject_loader.py    # Loads subject config, prompt, template
│   ├── fetcher.py           # Google News RSS fetching
│   ├── analyzer.py          # Claude API filtering + caching + tracking
│   ├── reporter.py          # HTML report generation
│   ├── telegram_bot.py      # Telegram delivery
│   ├── url_resolver.py      # Google News URL resolution
│   └── utils.py             # Shared retry decorator
├── subjects/
│   ├── rezoning/            # Rezoning subject
│   │   ├── subject.yaml     # Keywords, states, field mappings
│   │   └── prompt.md        # Zoning intelligence prompt
│   ├── infrastructure/      # Infrastructure subject
│   │   ├── subject.yaml
│   │   └── prompt.md
│   └── _template/           # Copy to create a new subject
│       ├── subject.yaml     # Annotated template
│       └── prompt.md        # Skeleton prompt
├── templates/
│   └── default_report.html  # Dynamic Jinja2 template (all subjects)
├── config.yaml              # Global pipeline settings
├── .env                     # API keys (not committed)
├── reports/<subject>/       # Generated HTML reports + JSON opportunities
├── logs/<subject>/          # Run logs (JSON with token usage)
└── logs/failed/<subject>/   # Raw articles from failed API calls
```

## Scheduling

Cron jobs run daily, staggered by 30 minutes to avoid Google News rate limiting:

| Time (Eastern) | Subject |
|----------------|---------|
| 8:00 AM | infrastructure |
| 8:30 AM | rezoning |

Run `server-setup.sh` for automated VPS setup with cron jobs.

## Troubleshooting

**No articles fetched**: Google News may rate-limit. Try `--days 3` for a wider window, or reduce keywords in subject.yaml. Running subjects back-to-back manually can trigger 503 errors — the 30-minute cron gap prevents this.

**Claude API errors**: Check `ANTHROPIC_API_KEY`. Failed batches are saved to `logs/failed/<subject>/`.

**Telegram not delivering**: Run `--test-telegram` to verify. Check bot token and chat ID.

**Subject not found**: Run `--list-subjects` to see available subjects. Ensure `subject.yaml` exists in the subject folder.
