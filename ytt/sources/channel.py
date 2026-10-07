"""Turning what a person typed into something yt-dlp can read, and lists of video ids from text."""
import re

TABS = ("videos", "shorts")
ID = r"[A-Za-z0-9_-]{11}"
_IN_URL = re.compile(rf"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)({ID})(?![A-Za-z0-9_-])")


def base_channel_url(channel):
    """'@Name', 'Name' or a channel link (also one that ends in /videos or /shorts) -> the channel's own address."""
    channel = channel.strip().rstrip("/")
    url = channel if channel.startswith("http") else f"https://www.youtube.com/{channel}"
    for tab in (*TABS, "streams", "featured", "channels", "about"):
        if url.endswith(f"/{tab}"):
            return url[: -len(tab) - 1]
    return url


def video_id(text):
    """A YouTube id from a bare id or a video link, else None."""
    text = text.strip()
    if re.fullmatch(ID, text):
        return text
    m = _IN_URL.search(text)
    return m.group(1) if m else None


def parse_video_ids(text):
    """One id (or video link) per line; blank lines and lines starting with # are ignored.
    Returns the ids in order without repeats. Raises ValueError naming the first line that is not a video."""
    out, seen = [], set()
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        vid = video_id(line)
        if vid is None:
            raise ValueError(f"line {n} is not a YouTube video id or link: {line[:60]!r}")
        if vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out
