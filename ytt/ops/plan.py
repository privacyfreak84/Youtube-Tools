"""The Plan: a real object describing what an action will do, built before anything happens (DESIGN.md section 5).
Errors block execution. Warnings are shown and need a yes. Notes are information only."""
from dataclasses import asdict, dataclass, field

COMPLETED, PARTIAL, FAILED, CANCELLED = "completed", "partial", "failed", "cancelled"


@dataclass
class Action:
    kind: str                      # render, download, delete, ...
    text: str                      # one line a person can read
    data: dict = field(default_factory=dict)


@dataclass
class Plan:
    title: str
    actions: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors

    def to_dict(self):
        return asdict(self)


@dataclass
class Item:
    """The outcome of one unit of work inside a run (one render, one download)."""
    what: str
    status: str                    # completed | failed | cancelled | skipped
    detail: str = ""


@dataclass
class RunResult:
    run_id: int
    status: str                    # completed | partial | failed | cancelled
    items: list = field(default_factory=list)
    made: list = field(default_factory=list)       # names of compilations that now exist because of this run

    def summary_status(self):
        done = sum(1 for i in self.items if i.status == "completed")
        if any(i.status == "cancelled" for i in self.items):
            return CANCELLED
        if done == len(self.items):
            return COMPLETED
        return PARTIAL if done else FAILED
