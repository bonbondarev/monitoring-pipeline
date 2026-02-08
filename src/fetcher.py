"""Google News RSS fetcher with retry, deduplication, and date filtering."""

import calendar
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import feedparser
import requests

from src.utils import retry_with_backoff

logger = logging.getLogger(__name__)

# Google News tracking params to strip during deduplication
_TRACKING_PARAMS = {
    "oc",
    "ved",
    "usg",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
}


def fetch_all_articles(
    keywords: list[str],
    lookback_days: int = 1,
    max_articles: int = 100,
) -> list[dict]:
    """Fetch and deduplicate articles across all keywords.

    Returns a list of article dicts sorted by published date descending,
    capped at max_articles. Google News redirect URLs are resolved to
    actual article URLs before deduplication.
    """
    from src.url_resolver import resolve_urls

    # Step 1: Collect all raw articles from all keywords
    raw_articles = []
    for keyword in keywords:
        try:
            articles = _fetch_keyword(keyword, lookback_days)
            raw_articles.extend(articles)
        except Exception as e:
            logger.error("Failed to fetch keyword '%s': %s", keyword, e)
            continue

    # Step 2: Resolve Google News redirect URLs to actual article URLs
    resolve_urls(raw_articles)

    # Step 3: Deduplicate on resolved URLs
    seen_urls: set[str] = set()
    deduplicated = []
    for article in raw_articles:
        normalized = _normalize_url(article["url"])
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            deduplicated.append(article)

    # Second pass: headline-based dedup (catches cases where URL resolution
    # fails and two different Google News URLs point to the same article)
    seen_titles: set[str] = set()
    title_deduped = []
    for article in deduplicated:
        title_key = article["title"].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            title_deduped.append(article)
        else:
            logger.debug("Title dedup removed: %s", article["title"][:80])
    deduplicated = title_deduped

    # Sort by published date descending, cap at max
    deduplicated.sort(key=lambda a: a["published"], reverse=True)
    result = deduplicated[:max_articles]
    logger.info(
        "Fetched %d unique articles from %d keywords (capped at %d)",
        len(result),
        len(keywords),
        max_articles,
    )
    return result


@retry_with_backoff(
    max_retries=3,
    base_delay=2.0,
    backoff_factor=2.0,
    exceptions=(requests.RequestException,),
)
def _fetch_keyword(keyword: str, lookback_days: int) -> list[dict]:
    """Fetch Google News RSS for a single keyword."""
    encoded = quote_plus(keyword)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={encoded}+when:{lookback_days}d&hl=en-US&gl=US&ceid=US:en"
    )

    logger.debug("Fetching RSS: %s", url)
    resp = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RezoningMonitor/1.0)"},
    )
    resp.raise_for_status()

    feed = feedparser.parse(resp.text)

    if feed.bozo and not feed.entries:
        logger.warning(
            "Malformed RSS for keyword '%s': %s", keyword, feed.bozo_exception
        )
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    articles = []

    for entry in feed.entries:
        published = _parse_date(entry)
        if published and published < cutoff:
            continue

        title = entry.get("title", "")
        articles.append(
            {
                "title": title,
                "snippet": entry.get("summary", entry.get("description", "")),
                "url": entry.get("link", ""),
                "published": published.isoformat() if published else "",
                "source": _extract_source(entry, title),
                "keyword": keyword,
            }
        )

    logger.debug("Keyword '%s': %d articles", keyword, len(articles))
    return articles


def _normalize_url(url: str) -> str:
    """Normalize URL for deduplication: strip tracking params, lowercase host."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if parsed.query:
        params = parse_qs(parsed.query)
        cleaned = {k: v for k, v in params.items() if k not in _TRACKING_PARAMS}
        query = urlencode(cleaned, doseq=True)
    else:
        query = ""

    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{hostname}{path}{'?' + query if query else ''}"


def _parse_date(entry) -> datetime | None:
    """Parse entry publication date to timezone-aware datetime."""
    if entry.get("published_parsed"):
        timestamp = calendar.timegm(entry.published_parsed)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    if entry.get("published"):
        try:
            return parsedate_to_datetime(entry.published)
        except (ValueError, TypeError):
            pass

    return None


def _extract_source(entry, title: str) -> str:
    """Extract news source name from entry or title.

    Google News RSS includes the source after the last ' - ' in the title.
    """
    source = entry.get("source", {})
    if isinstance(source, dict) and source.get("title"):
        return source["title"]

    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()

    return "Unknown"
