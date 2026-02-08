"""Telegram Bot API delivery using raw requests (no SDK) — subject-agnostic."""

import logging
from pathlib import Path

import requests

from src.utils import retry_with_backoff

logger = logging.getLogger(__name__)


class TelegramDelivery:
    """Handles all Telegram message delivery via Bot API."""

    def __init__(self, token: str, chat_id: str,
                 subject_name: str = "Monitor", subject_emoji: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.subject_name = subject_name
        self.subject_emoji = subject_emoji

    def _header(self, date: str) -> str:
        """Build the message header line."""
        prefix = f"{self.subject_emoji} " if self.subject_emoji else ""
        return f"{prefix}{self.subject_name} \u2014 {date}"

    def send_summary(
        self, kept_articles: list[dict], stats: dict
    ) -> bool:
        """Send formatted summary text message with emoji score indicators.

        Args:
            kept_articles: List of KEEP articles sorted by score desc.
            stats: Dict with total_scanned, kept_count, killed_count,
                   high_priority_count keys.
        """
        total = stats.get("total_scanned", 0)
        kept_count = stats.get("kept_count", 0)
        killed = stats.get("killed_count", 0)
        high_priority = stats.get("high_priority_count", 0)

        lines = [
            self._header(stats.get("date", "today")),
            "",
            f"Scanned: {total} articles",
            f"Opportunities: {kept_count} ({high_priority} high priority)",
            f"Killed: {killed}",
            "",
        ]

        for article in kept_articles[:15]:
            score = article.get("score", 0)
            emoji = "\U0001f534" if score >= 8 else "\U0001f7e1"
            city = article.get("city", "")
            state = article.get("state", "")
            location = f"{city}, {state}" if city and state else city or state
            headline = article.get("headline", "")[:80]
            classification = article.get("classification", "")
            stage = article.get("stage", "")

            lines.append(f"{emoji} {score}/10 \u2014 {location}")
            lines.append(headline)
            lines.append(classification)
            lines.append(f"\u2192 {stage}")
            lines.append("")

        lines.append("---")
        lines.append("Full report attached \u2193")

        text = "\n".join(lines)
        return self._send_message(text)

    def send_no_results(self, stats: dict) -> bool:
        """Send 'no opportunities found' message.

        Args:
            stats: Dict with total_scanned and killed_count keys.
        """
        total = stats.get("total_scanned", 0)
        killed = stats.get("killed_count", 0)

        text = (
            f"{self._header(stats.get('date', 'today'))}\n"
            f"\n"
            f"Scanned: {total} articles\n"
            f"No opportunities found today.\n"
            f"\n"
            f"All {killed} articles were filtered out."
        )
        return self._send_message(text)

    @retry_with_backoff(
        max_retries=2,
        base_delay=3.0,
        exceptions=(requests.RequestException,),
    )
    def send_report(self, report_path: Path, caption: str = "") -> bool:
        """Send HTML report as a document attachment.

        Args:
            report_path: Path to the HTML report file.
            caption: Optional caption for the document.
        """
        url = f"{self.base_url}/sendDocument"
        default_caption = f"Daily {self.subject_name} Report"
        with open(report_path, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id": self.chat_id,
                    "caption": caption or default_caption,
                },
                files={"document": (report_path.name, f, "text/html")},
                timeout=30,
            )

        if resp.status_code != 200:
            logger.error(
                "Telegram sendDocument failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
            return False

        logger.info("Report sent to Telegram: %s", report_path.name)
        return True

    def send_test(self) -> bool:
        """Send a test message to verify Telegram setup."""
        return self._send_message(
            f"{self._header('Test message')}\n\n"
            "Telegram delivery is working correctly."
        )

    @retry_with_backoff(
        max_retries=2,
        base_delay=3.0,
        exceptions=(requests.RequestException,),
    )
    def _send_message(self, text: str) -> bool:
        """Send a text message via Telegram Bot API."""
        url = f"{self.base_url}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id": self.chat_id,
                "text": text,
            },
            timeout=15,
        )

        if resp.status_code != 200:
            logger.error(
                "Telegram sendMessage failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
            return False

        return True
