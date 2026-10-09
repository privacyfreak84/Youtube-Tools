"""The checks the guided menu applies to typed answers. Each returns None when the answer is fine, or a short
message saying what is wrong (the question is then asked again)."""

from pathlib import Path

from ytt.sources import channel as chan


def whole_number(text):
    t = text.strip()
    return None if t.isdigit() and int(t) > 0 else "Type a whole number, 1 or more."


def number(text):
    try:
        return None if float(text) > 0 else "Type a number above 0."
    except ValueError:
        return "Type a number above 0."


def channel_problem(text):
    t = text.strip()
    if not t:
        return "Type the channel's name (like @Name) or a link."
    if any(c.isspace() for c in t):
        return "A channel name or link has no spaces."
    return None


def whole_number_or_nothing(text):
    return None if not text.strip() else whole_number(text)


def number_or_nothing(text):
    return None if not text.strip() else number(text)


def videos_file_problem(text):
    path = Path(text.strip()).expanduser()
    if not path.is_file():
        return "There is no file there."
    try:
        ids = chan.parse_video_ids(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return f"Can't read it: {e}"
    except ValueError as e:
        return str(e)
    return None if ids else "That file lists no videos."
