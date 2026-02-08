"""Resolve Google News RSS redirect URLs to actual article URLs."""

import base64
import logging
import re
from urllib.parse import urlparse

from googlenewsdecoder import gnewsdecoder

logger = logging.getLogger(__name__)

# Pattern to identify Google News RSS article URLs
_GNEWS_ARTICLE_PATTERN = re.compile(
    r"^https?://news\.google\.com/rss/articles/(.+)"
)


def resolve_url(url: str) -> str:
    """Resolve a Google News RSS URL to the actual article URL.

    Tries fast base64 protobuf decode first (works for older-format URLs),
    then falls back to googlenewsdecoder (uses Google's batchexecute API).
    Returns the original URL if both methods fail.
    """
    match = _GNEWS_ARTICLE_PATTERN.match(url)
    if not match:
        return url  # Not a Google News URL, return as-is

    encoded_payload = match.group(1)

    # Strip query string from the payload (e.g., ?oc=5)
    if "?" in encoded_payload:
        encoded_payload = encoded_payload.split("?", 1)[0]

    # Tier 1: Try fast base64 protobuf decode (zero network cost, older URLs)
    decoded_url = _decode_gnews_url(encoded_payload)
    if decoded_url:
        return decoded_url

    # Tier 2: Use googlenewsdecoder (newer encrypted URLs)
    resolved = _resolve_via_decoder(url)
    if resolved:
        return resolved

    logger.warning("Could not resolve Google News URL: %s", url[:80])
    return url  # Graceful fallback to original


def resolve_urls(articles: list[dict]) -> list[dict]:
    """Resolve Google News URLs for a batch of articles in-place.

    Updates the 'url' field with the resolved URL and stores the
    original Google News URL in 'original_google_url'.

    Returns the same list for chaining.
    """
    resolved_count = 0
    failed_count = 0

    for article in articles:
        original = article.get("url", "")
        if _GNEWS_ARTICLE_PATTERN.match(original):
            resolved = resolve_url(original)
            if resolved != original:
                article["original_google_url"] = original
                article["url"] = resolved
                resolved_count += 1
            else:
                failed_count += 1

    logger.info(
        "URL resolution: %d resolved, %d failed (kept original)",
        resolved_count,
        failed_count,
    )
    return articles


def _decode_gnews_url(encoded_payload: str) -> str | None:
    """Decode the base64url-encoded protobuf payload from a Google News URL.

    Works for older-format Google News URLs where the actual URL is
    directly embedded in the protobuf payload. Newer URLs use encryption
    and require the googlenewsdecoder fallback.
    """
    try:
        padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
        raw_bytes = base64.urlsafe_b64decode(padded)

        # Decode as latin-1 to preserve all byte values for regex matching
        decoded_str = raw_bytes.decode("latin-1")

        # Find URL pattern in decoded bytes
        url_match = re.search(r"https?://[^\s\x00-\x1f\"<>]+", decoded_str)
        if url_match:
            candidate = url_match.group(0)
            if "news.google.com" not in candidate:
                parsed = urlparse(candidate)
                if parsed.scheme and parsed.netloc:
                    return candidate
    except Exception as e:
        logger.debug("Base64 decode failed: %s", e)

    return None


def _resolve_via_decoder(url: str) -> str | None:
    """Resolve URL using googlenewsdecoder (Google's batchexecute API)."""
    try:
        result = gnewsdecoder(url)
        if result.get("status") and result.get("decoded_url"):
            decoded = result["decoded_url"]
            if "news.google.com" not in decoded:
                return decoded
    except Exception as e:
        logger.debug("gnewsdecoder failed for %s: %s", url[:60], e)

    return None
