"""Article enrichment — fetch full article text before Claude filtering."""

import logging
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; NewsBot/1.0)"
_FETCH_TIMEOUT = 10
_DELAY_BETWEEN_REQUESTS = 1
_MAX_TEXT_LENGTH = 2000
_PAYWALL_KEYWORDS = {
    "subscribe", "subscription", "sign in to read", "premium content",
    "members only", "paywall", "register to continue", "log in to read",
}


def enrich_articles(articles: list[dict]) -> list[dict]:
    """Fetch full article text for each article.

    Adds `full_text` and `fetch_status` fields to each article dict.
    Never raises — all errors are caught and logged. Returns the same
    list with enrichment fields added.

    Args:
        articles: List of raw RSS article dicts.

    Returns:
        The same list with `full_text` and `fetch_status` added to each.
    """
    start_time = time.time()
    stats: dict[str, int] = {"success": 0, "timeout": 0, "error": 0, "paywall": 0}

    for i, article in enumerate(articles):
        url = article.get("url", "")
        if not url:
            article["full_text"] = None
            article["fetch_status"] = "error"
            stats["error"] += 1
            continue

        try:
            full_text, status = _fetch_article_text(url)
            article["full_text"] = full_text
            article["fetch_status"] = status
            stats[status] += 1
        except Exception as e:
            logger.debug("Enrichment failed for %s: %s", url[:80], e)
            article["full_text"] = None
            article["fetch_status"] = "error"
            stats["error"] += 1

        # Rate limiting: 1-second delay between requests
        if i < len(articles) - 1:
            time.sleep(_DELAY_BETWEEN_REQUESTS)

    elapsed = round(time.time() - start_time, 1)
    logger.info(
        "Enrichment complete in %.1fs: %d total, %d success, %d timeout, "
        "%d error, %d paywall",
        elapsed,
        len(articles),
        stats["success"],
        stats["timeout"],
        stats["error"],
        stats["paywall"],
    )

    return articles


def get_enrichment_stats(articles: list[dict]) -> dict[str, Any]:
    """Compute enrichment stats from articles that have been enriched."""
    stats: dict[str, int] = {"success": 0, "timeout": 0, "error": 0, "paywall": 0}
    for a in articles:
        status = a.get("fetch_status", "")
        if status in stats:
            stats[status] += 1
    return {
        "total": len(articles),
        **stats,
    }


def _fetch_article_text(url: str) -> tuple[str | None, str]:
    """Fetch and extract article text from a URL.

    Returns (text, status) where status is one of:
    "success", "timeout", "error", "paywall".
    """
    try:
        resp = requests.get(
            url,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            allow_redirects=True,
        )
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException:
        return None, "error"

    # Check for paywall HTTP codes
    if resp.status_code in (402, 403):
        return None, "paywall"

    if resp.status_code != 200:
        return None, "error"

    text = _extract_text(resp.text)

    # Detect paywall: very short body with subscription keywords
    if text and len(text) < 200:
        text_lower = text.lower()
        if any(kw in text_lower for kw in _PAYWALL_KEYWORDS):
            return None, "paywall"

    if not text:
        return None, "error"

    # Truncate to max length
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH]

    return text, "success"


def _extract_text(html: str) -> str | None:
    """Extract main article text from HTML.

    Strategy: find <article> tag first, fall back to largest block of <p> tags.
    Strips scripts, styles, nav, footer, ads.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Remove unwanted elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "aside",
                              "header", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Remove common ad/social divs
    for div in soup.find_all(["div", "section"], class_=lambda c: c and any(
        kw in str(c).lower() for kw in
        ["ad-", "ads-", "advert", "social", "share", "comment", "related",
         "sidebar", "newsletter", "popup", "modal", "cookie"]
    )):
        div.decompose()

    # Strategy 1: Find <article> tag
    article_tag = soup.find("article")
    if article_tag:
        paragraphs = article_tag.find_all("p")
        if paragraphs:
            text = "\n".join(p.get_text(strip=True) for p in paragraphs)
            if len(text) >= 100:
                return text

    # Strategy 2: Largest block of <p> tags
    paragraphs = soup.find_all("p")
    if paragraphs:
        text = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
        if text:
            return text

    return None
