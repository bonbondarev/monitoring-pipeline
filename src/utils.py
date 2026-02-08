"""Shared utilities: retry decorator with exponential backoff."""

import functools
import logging
import time

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,),
):
    """Decorator: retry on specified exceptions with exponential backoff."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (backoff_factor**attempt)
                        logger.warning(
                            "Attempt %d/%d for %s failed: %s. Retrying in %.1fs",
                            attempt + 1,
                            max_retries,
                            func.__name__,
                            e,
                            delay,
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "All %d attempts for %s failed: %s",
                            max_retries,
                            func.__name__,
                            e,
                        )
            raise last_exception

        return wrapper

    return decorator
