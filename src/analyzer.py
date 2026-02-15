"""Claude API article analyzer — subject-agnostic."""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import anthropic

from src.utils import retry_with_backoff

logger = logging.getLogger(__name__)

# Fields required in each analyzer result
_REQUIRED_FIELDS = {"decision", "headline"}

# Default values for optional fields shared across all subjects
_BASE_OPTIONAL_DEFAULTS = {
    "classification": "",
    "score": 0,
    "city": "",
    "state": "",
    "location_details": "",
    "initiator": "",
    "stage": "",
    "timeline": "",
    "reasoning": "",
    "source_url": "",
    "next_steps": "",
}


def _empty_usage() -> dict:
    """Return a zeroed-out usage dict."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _accumulate_usage(totals: dict, usage) -> None:
    """Add token counts from an API response usage object into *totals*."""
    totals["input_tokens"] += getattr(usage, "input_tokens", 0)
    totals["output_tokens"] += getattr(usage, "output_tokens", 0)
    totals["cache_creation_input_tokens"] += getattr(
        usage, "cache_creation_input_tokens", 0
    )
    totals["cache_read_input_tokens"] += getattr(
        usage, "cache_read_input_tokens", 0
    )


def analyze_articles(
    articles: list[dict],
    system_prompt: str,
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 16384,
    batch_size: int = 25,
    extra_fields: list[dict] | None = None,
    subject_slug: str = "",
    use_batch_api: bool = False,
) -> tuple[list[dict], dict]:
    """Analyze articles through Claude API with a subject-specific system prompt.

    Args:
        articles: Raw articles from fetcher.
        system_prompt: The filter system prompt for this subject.
        model: Claude model ID.
        max_tokens: Max response tokens.
        batch_size: Articles per API call.
        extra_fields: List of {field, default} dicts for subject-specific fields.
        subject_slug: Subject slug for log directory namespacing.
        use_batch_api: Use the Messages Batch API (50% cheaper, async).

    Returns (results, usage) — a list of analyzed article dicts and a dict of
    total token usage across all batches.
    """
    if not articles:
        logger.info("No articles to analyze")
        return [], _empty_usage()

    # Build optional defaults: base + subject-specific extra fields
    optional_defaults = dict(_BASE_OPTIONAL_DEFAULTS)
    if extra_fields:
        for field_spec in extra_fields:
            optional_defaults[field_spec["field"]] = field_spec.get("default", "")

    client = anthropic.Anthropic()
    all_results = []
    total_usage = _empty_usage()

    batches = [articles[i : i + batch_size] for i in range(0, len(articles), batch_size)]
    logger.info("Analyzing %d articles in %d batch(es)", len(articles), len(batches))

    if use_batch_api:
        results, usage = _analyze_via_batch_api(
            client, system_prompt, batches, model, max_tokens,
            optional_defaults, subject_slug,
        )
        all_results.extend(results)
        for key in total_usage:
            total_usage[key] += usage[key]
    else:
        for batch_idx, batch in enumerate(batches):
            try:
                result, usage = _analyze_batch(
                    client, system_prompt, batch, model, max_tokens,
                    optional_defaults,
                )
                all_results.extend(result)
                _accumulate_usage(total_usage, usage)
                logger.info(
                    "Batch %d usage: %d in / %d out / %d cache-create / %d cache-read",
                    batch_idx,
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "output_tokens", 0),
                    getattr(usage, "cache_creation_input_tokens", 0),
                    getattr(usage, "cache_read_input_tokens", 0),
                )
            except Exception as e:
                logger.error("Batch %d analysis failed after retries: %s", batch_idx, e)
                _save_failed_batch(batch, subject_slug)

    logger.info(
        "Total token usage: %d in / %d out / %d cache-create / %d cache-read",
        total_usage["input_tokens"],
        total_usage["output_tokens"],
        total_usage["cache_creation_input_tokens"],
        total_usage["cache_read_input_tokens"],
    )

    return all_results, total_usage


def _build_user_message(articles: list[dict]) -> str:
    """Build the user message for a batch of articles."""
    articles_payload = [
        {
            "title": a["title"],
            "snippet": a["snippet"],
            "url": a["url"],
            "published": a["published"],
            "source": a["source"],
        }
        for a in articles
    ]
    return (
        f"Analyze the following {len(articles_payload)} articles. "
        f"Return a JSON array with EXACTLY {len(articles_payload)} objects — "
        f"one KEEP or KILL decision per article. Do not skip any.\n\n"
        f"```json\n{json.dumps(articles_payload, separators=(',', ':'))}\n```"
    )


@retry_with_backoff(
    max_retries=2,
    base_delay=5.0,
    backoff_factor=2.0,
    exceptions=(anthropic.APIError,),
)
def _analyze_batch(
    client: anthropic.Anthropic,
    system_prompt: str,
    articles: list[dict],
    model: str,
    max_tokens: int,
    optional_defaults: dict,
) -> tuple[list[dict], object]:
    """Send one batch of articles to Claude for analysis.

    Returns (results, usage) where usage is the response.usage object.
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": _build_user_message(articles)}],
    )

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Response truncated (hit max_tokens=%d). "
            "Consider reducing batch_size or increasing max_tokens.",
            max_tokens,
        )

    response_text = response.content[0].text
    return _parse_response(response_text, articles, optional_defaults), response.usage


def _analyze_via_batch_api(
    client: anthropic.Anthropic,
    system_prompt: str,
    batches: list[list[dict]],
    model: str,
    max_tokens: int,
    optional_defaults: dict,
    subject_slug: str,
) -> tuple[list[dict], dict]:
    """Send all batches via the Messages Batch API (50% cheaper, async).

    Returns (results, usage_totals).
    """
    system_block = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]

    requests = []
    for batch_idx, batch in enumerate(batches):
        requests.append({
            "custom_id": f"batch-{batch_idx}",
            "params": {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_block,
                "messages": [{"role": "user", "content": _build_user_message(batch)}],
            },
        })

    logger.info("Submitting %d request(s) to Messages Batch API", len(requests))
    message_batch = client.messages.batches.create(requests=requests)
    batch_id = message_batch.id
    logger.info("Batch created: %s — polling for completion", batch_id)

    poll_interval = 10
    while True:
        message_batch = client.messages.batches.retrieve(batch_id)
        status = message_batch.processing_status
        logger.info(
            "Batch %s: %s (succeeded=%d, errored=%d, expired=%d, canceled=%d)",
            batch_id,
            status,
            message_batch.request_counts.succeeded,
            message_batch.request_counts.errored,
            message_batch.request_counts.expired,
            message_batch.request_counts.canceled,
        )
        if status == "ended":
            break
        time.sleep(poll_interval)

    # Collect results ordered by batch index
    result_map: dict[int, tuple[list[dict], dict]] = {}
    total_usage = _empty_usage()

    for entry in client.messages.batches.results(batch_id):
        batch_idx = int(entry.custom_id.split("-")[1])
        if entry.result.type == "succeeded":
            msg = entry.result.message
            usage = msg.usage
            _accumulate_usage(total_usage, usage)
            if msg.stop_reason == "max_tokens":
                logger.warning("Batch request %s truncated", entry.custom_id)
            response_text = msg.content[0].text
            parsed = _parse_response(
                response_text, batches[batch_idx], optional_defaults,
            )
            result_map[batch_idx] = parsed
        else:
            logger.error(
                "Batch request %s failed: %s", entry.custom_id, entry.result.type,
            )
            _save_failed_batch(batches[batch_idx], subject_slug)

    # Reassemble in order
    all_results = []
    for idx in sorted(result_map):
        all_results.extend(result_map[idx])

    return all_results, total_usage


def _parse_response(
    response_text: str, original_articles: list[dict], optional_defaults: dict
) -> list[dict]:
    """Parse Claude's JSON response with fallback extraction."""
    # Try direct parse
    try:
        data = json.loads(response_text)
        if isinstance(data, list):
            return _validate_results(data, optional_defaults)
        if isinstance(data, dict) and "articles" in data:
            return _validate_results(data["articles"], optional_defaults)
    except json.JSONDecodeError:
        pass

    # Fallback: extract JSON from markdown code fences or raw text
    patterns = [
        r"```json\s*\n(.*?)\n\s*```",
        r"```\s*\n(.*?)\n\s*```",
        r"(\[[\s\S]*\])",
    ]
    for pattern in patterns:
        match = re.search(pattern, response_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, list):
                    return _validate_results(data, optional_defaults)
                if isinstance(data, dict) and "articles" in data:
                    return _validate_results(data["articles"], optional_defaults)
            except json.JSONDecodeError:
                continue

    # Last resort: try to extract individual JSON objects from truncated response
    objects = _extract_partial_json_objects(response_text)
    if objects:
        logger.warning(
            "Extracted %d partial JSON objects from truncated response", len(objects)
        )
        return _validate_results(objects, optional_defaults)

    logger.error("Could not parse JSON from Claude response")
    logger.debug("Raw response (first 500 chars): %s", response_text[:500])
    _save_failed_batch(original_articles)
    return []


def _extract_partial_json_objects(text: str) -> list[dict]:
    """Extract complete JSON objects from a potentially truncated JSON array.

    When the response is truncated mid-array, individual objects up to the
    truncation point may still be valid.
    """
    objects = []
    # Find all {...} blocks that look like complete objects
    depth = 0
    start = None
    for i, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start : i + 1])
                    if isinstance(obj, dict) and "decision" in obj:
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    return objects


def _validate_results(results: list, optional_defaults: dict) -> list[dict]:
    """Validate and normalize analyzer results."""
    validated = []
    for r in results:
        if not isinstance(r, dict):
            continue

        # Accept either 'headline' or 'title' for the headline field
        if "title" in r and "headline" not in r:
            r["headline"] = r["title"]

        # Check required fields
        missing = _REQUIRED_FIELDS - set(r.keys())
        if missing:
            logger.warning("Skipping result missing required fields: %s", missing)
            continue

        # Apply defaults for optional fields
        for field, default in optional_defaults.items():
            r.setdefault(field, default)

        # Normalize decision
        r["decision"] = str(r["decision"]).upper()

        # Clamp score to 0-10
        try:
            r["score"] = max(0, min(10, int(r["score"])))
        except (ValueError, TypeError):
            r["score"] = 0

        # Ensure source_url is populated from original article if empty
        if not r.get("source_url"):
            r["source_url"] = r.get("url", "")

        validated.append(r)

    logger.info("Validated %d/%d results", len(validated), len(results))
    return validated


def _save_failed_batch(articles: list[dict], subject_slug: str = "") -> None:
    """Save failed articles to logs/failed/<subject>/ for later inspection."""
    if subject_slug:
        failed_dir = Path(__file__).parent.parent / "logs" / "failed" / subject_slug
    else:
        failed_dir = Path(__file__).parent.parent / "logs" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    filepath = failed_dir / filename
    filepath.write_text(json.dumps(articles, indent=2), encoding="utf-8")
    logger.info("Saved failed batch to %s", filepath)
