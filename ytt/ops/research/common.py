"""Small things every research tool shares. Research only reads from YouTube and never touches the library."""
from datetime import datetime, timezone

from ytt.sources.channel import base_channel_url, parse_video_ids   # re-exported: the one way channels and ids are read

__all__ = ["ResearchError", "base_channel_url", "parse_video_ids", "parse_channel_list", "utc_now", "days_between"]


class ResearchError(Exception):
    """A request that cannot be carried out (a bad option, nothing to work on). The message is for the person."""


def parse_channel_list(text):
    """One channel (@handle, name or link) per line; blank lines and lines starting with # are skipped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def utc_now():
    return datetime.now(timezone.utc)


def days_between(timestamp, now):
    """Whole days from a UTC timestamp (seconds) to `now`, or None when there is no timestamp."""
    if not timestamp:
        return None
    return (now - datetime.fromtimestamp(timestamp, tz=timezone.utc)).days
