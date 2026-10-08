"""The checks the guided menu applies to typed answers. Each returns None when the answer is fine, or a short
message saying what is wrong (the question is then asked again)."""


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
