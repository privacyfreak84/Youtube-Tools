"""A style: how a compilation is rendered. Only what the engine can do today (DESIGN.md section 9).
Flat on purpose: no inheritance. Stored in the database as JSON; a compilation keeps a snapshot copy."""
from dataclasses import asdict, dataclass, fields

from ytt.workspace import store

QUALITIES = ("fast", "balanced", "best")
TRANSITION_MODES = ("hold", "overlap")        # hold: every clip plays in full (default); overlap: the old way
STINGER_KEYS = ("auto", "none", "green", "blue", "black", "white")
DEFAULT_NAME = "default"


class StyleError(ValueError):
    pass


@dataclass(frozen=True)
class Style:
    transition: str = "fade"               # any stitch transition, "random", "cut", or "stinger"
    transition_seconds: float = 1.0
    transition_mode: str = "hold"
    stinger_dir: str = ""
    stinger_key: str = "auto"              # auto | none | green | blue | black | white | RRGGBB
    stinger_sim: float = 0.12
    stinger_blend: float = 0.05
    stinger_despill: bool = False
    stinger_audio: bool = True
    intro: str = ""
    outro: str = ""
    quality: str = "balanced"
    max_height: int = 1080

    def __post_init__(self):
        if self.quality not in QUALITIES:
            raise StyleError(f"quality must be one of {', '.join(QUALITIES)} (got {self.quality!r})")
        if self.transition_mode not in TRANSITION_MODES:
            raise StyleError(f"transition_mode must be one of {', '.join(TRANSITION_MODES)} (got {self.transition_mode!r})")
        if not isinstance(self.transition_seconds, (int, float)) or isinstance(self.transition_seconds, bool) \
                or self.transition_seconds < 0:
            raise StyleError(f"transition_seconds must be a number of seconds, 0 or more (got {self.transition_seconds!r})")
        if not isinstance(self.max_height, int) or isinstance(self.max_height, bool) or self.max_height <= 0:
            raise StyleError(f"max_height must be a whole number of pixels above 0 (got {self.max_height!r})")
        key = self.stinger_key
        hexed = len(key) == 6 and all(c in "0123456789abcdefABCDEF" for c in key)
        if key not in STINGER_KEYS and not hexed:
            raise StyleError(f"stinger_key must be one of {', '.join(STINGER_KEYS)} or a colour like 00FF00 (got {key!r})")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        """Unknown keys are ignored (a newer ytt may have written them); invalid values raise StyleError."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def ensure_default(conn):
    """Make sure the style named 'default' exists. Never overwrites an existing one."""
    if store.get_style(conn, DEFAULT_NAME) is None:
        store.save_style(conn, DEFAULT_NAME, Style().to_dict())
        return True
    return False
