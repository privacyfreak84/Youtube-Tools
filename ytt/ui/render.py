"""Plain-text pieces the screens share: aligned tables and short number formats."""


def table(rows, headers):
    """Left-aligned columns with a dashed line under the headings. Nothing wraps."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths]), *[line(r) for r in cells]])


def count(n):
    """12345 -> '12.3K', 2500000 -> '2.5M', None -> 'N/A'."""
    if n is None:
        return "N/A"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def yes_no(value):
    return "N/A" if value is None else ("Yes" if value else "No")
