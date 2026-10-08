"""tags: which tags and title words come up most across a set of videos (the old TagExtractor). Tags are only on a
video's own page, so every video is one request."""
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field

from ytt.ops.research.common import ResearchError, parse_video_ids

STOPWORDS = frozenset("""the a an and or but of in on at to for with by from up about into over after is are was were be been
being this that these those it its as vs you your i my we our he she they them his her their not no so if than then how what
when why who which do does did will would can could should just out off all new part video official""".split())


@dataclass
class TagsRequest:
    videos: list                    # video ids or links
    top: int = 25
    min_word_len: int = 3


@dataclass
class TagsResult:
    tags: Counter = field(default_factory=Counter)
    words: Counter = field(default_factory=Counter)
    total: int = 0                  # videos that could be read
    with_tags: int = 0              # of those, how many had at least one tag
    notes: list = field(default_factory=list)

    def rows(self, top):
        """[{'kind', 'value', 'count'}]: the top tags, then the top title words."""
        return ([{"kind": "tag", "value": v, "count": n} for v, n in self.tags.most_common(top)]
                + [{"kind": "title_word", "value": v, "count": n} for v, n in self.words.most_common(top)])


def tokenize_title(title, min_len):
    words = re.findall(r"[a-zA-Z0-9']+", title.lower())
    return [w for w in words if len(w) >= min_len and w not in STOPWORDS and not w.isdigit()]


def urls_from_csv(text):
    """The links in the 'url' column of a CSV made by a research tool (rows with no link are skipped)."""
    reader = csv.DictReader(io.StringIO(text))
    if "url" not in (reader.fieldnames or []):
        raise ResearchError("no 'url' column found (expected a CSV saved by a research tool with --csv)")
    return [u for u in ((row.get("url") or "").strip() for row in reader) if u and u != "N/A"]


def analyze_tags(request, backend, on_progress=None):
    r = request
    try:
        ids = parse_video_ids("\n".join(r.videos))
    except ValueError as e:
        raise ResearchError(str(e).replace("line ", "video ", 1))
    if not ids:
        raise ResearchError("give video ids or links (or --videos FILE, --from-csv FILE)")
    if r.top < 1:
        raise ResearchError("--top must be 1 or more")
    if r.min_word_len < 1:
        raise ResearchError("--min-word-len must be 1 or more")
    result = TagsResult()
    for i, vid in enumerate(ids, 1):
        if on_progress:
            on_progress(f"[{i}/{len(ids)}] fetching tags: {vid}")
        info = backend.video_info(vid)
        if info is None:
            result.notes.append(f"could not read {vid}; skipped")
            continue
        result.total += 1
        if info.tags:
            result.with_tags += 1
            for tag in {t.strip().lower() for t in info.tags if t.strip()}:       # a tag counts once per video
                result.tags[tag] += 1
        for word in tokenize_title(info.title or "", r.min_word_len):
            result.words[word] += 1
    return result
