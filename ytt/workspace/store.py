"""Reading and writing the workspace database. Plain functions on a connection; none of them commits
(the caller's transaction decides), so a dry run can use exactly the same code and roll back."""
import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- sources
def add_source(conn, handle):
    conn.execute("INSERT OR IGNORE INTO sources (handle) VALUES (?)", (handle,))
    return conn.execute("SELECT id FROM sources WHERE handle = ?", (handle,)).fetchone()["id"]


def list_sources(conn):
    return [r["handle"] for r in conn.execute("SELECT handle FROM sources ORDER BY handle")]


# ---- videos
def upsert_video(conn, youtube_id, *, title=None, views=None, duration=None, published=None, source_id=None):
    """Add a video, or fill in what is missing on an existing one. Known values are never replaced by None."""
    conn.execute(
        """INSERT INTO videos (youtube_id, source_id, title, views, duration, published, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(youtube_id) DO UPDATE SET
               source_id = COALESCE(excluded.source_id, source_id),
               title     = COALESCE(excluded.title, title),
               views     = COALESCE(excluded.views, views),
               duration  = COALESCE(excluded.duration, duration),
               published = COALESCE(excluded.published, published)""",
        (youtube_id, source_id, title, views, duration, published, now()))


def get_video(conn, youtube_id):
    return conn.execute("SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)).fetchone()


# ---- clips
def upsert_clip(conn, youtube_id, path, status):
    """Record the local file of a video (the video must already be known). Returns (clip id, created?)."""
    row = conn.execute("SELECT id FROM clips WHERE youtube_id = ?", (youtube_id,)).fetchone()
    if row:
        conn.execute("UPDATE clips SET path = ?, status = ? WHERE id = ?", (path, status, row["id"]))
        return row["id"], False
    cur = conn.execute("INSERT INTO clips (youtube_id, path, status, added) VALUES (?, ?, ?, ?)",
                       (youtube_id, path, status, now()))
    return cur.lastrowid, True


def get_clip(conn, youtube_id):
    return conn.execute("SELECT * FROM clips WHERE youtube_id = ?", (youtube_id,)).fetchone()


def list_clips(conn):
    return conn.execute("""SELECT c.*, v.title, v.views, v.duration,
                                  EXISTS (SELECT 1 FROM compilation_clips cc WHERE cc.clip_id = c.id) AS used
                           FROM clips c JOIN videos v USING (youtube_id) ORDER BY c.id""").fetchall()


# ---- styles
def save_style(conn, name, data):
    conn.execute("""INSERT INTO styles (name, data, created) VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET data = excluded.data""",
                 (name, json.dumps(data, sort_keys=True), now()))


def get_style(conn, name):
    row = conn.execute("SELECT data FROM styles WHERE name = ?", (name,)).fetchone()
    return json.loads(row["data"]) if row else None


def list_styles(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM styles ORDER BY name")]


# ---- compilations
def add_compilation(conn, name, *, clips, output_path=None, made=None, style_snapshot=None, request=None,
                    parent_id=None, imported=False):
    """clips: ordered list of (clip_id or None, legacy_name or None). Returns the new compilation id."""
    cur = conn.execute(
        """INSERT INTO compilations (name, output_path, made, style_snapshot, request, parent_id, imported)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, output_path, made,
         json.dumps(style_snapshot, sort_keys=True) if style_snapshot is not None else None,
         json.dumps(request, sort_keys=True) if request is not None else None,
         parent_id, 1 if imported else 0))
    cid = cur.lastrowid
    conn.executemany("INSERT INTO compilation_clips (compilation_id, position, clip_id, legacy_name) VALUES (?, ?, ?, ?)",
                     [(cid, i, clip_id, legacy) for i, (clip_id, legacy) in enumerate(clips, start=1)])
    return cid


def get_compilation(conn, name):
    return conn.execute("SELECT * FROM compilations WHERE name = ?", (name,)).fetchone()


def compilation_clips(conn, compilation_id):
    return conn.execute("""SELECT cc.position, cc.clip_id, cc.legacy_name, c.youtube_id, c.path, c.status
                           FROM compilation_clips cc LEFT JOIN clips c ON c.id = cc.clip_id
                           WHERE cc.compilation_id = ? ORDER BY cc.position""", (compilation_id,)).fetchall()


def counts(conn):
    def n(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return {"sources": n("sources"), "videos": n("videos"), "clips": n("clips"),
            "compilations": n("compilations"), "styles": n("styles"),
            "missing_clips": conn.execute("SELECT COUNT(*) FROM clips WHERE status != 'ready'").fetchone()[0]}


def update_compilation(conn, compilation_id, *, output_path=None, made=None, style_snapshot=None, request=None, clips=None):
    """Change a compilation in place (used by `remake --replace`). Fields left as None are not touched.
    clips, if given, replaces the ordered clip list."""
    sets, args = [], []
    for column, value in (("output_path", output_path), ("made", made)):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(value)
    for column, value in (("style_snapshot", style_snapshot), ("request", request)):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(json.dumps(value, sort_keys=True))
    if sets:
        conn.execute(f"UPDATE compilations SET {', '.join(sets)} WHERE id = ?", (*args, compilation_id))
    if clips is not None:
        conn.execute("DELETE FROM compilation_clips WHERE compilation_id = ?", (compilation_id,))
        conn.executemany("INSERT INTO compilation_clips (compilation_id, position, clip_id, legacy_name) VALUES (?, ?, ?, ?)",
                         [(compilation_id, i, clip_id, legacy) for i, (clip_id, legacy) in enumerate(clips, start=1)])


def list_compilations(conn):
    return conn.execute("""SELECT c.*, (SELECT COUNT(*) FROM compilation_clips cc WHERE cc.compilation_id = c.id) AS n_clips,
                                  p.name AS parent_name
                           FROM compilations c LEFT JOIN compilations p ON p.id = c.parent_id ORDER BY c.id""").fetchall()


def compilation_names(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM compilations")]


# ---- runs
def create_run(conn, kind, request=None, plan=None, status="running"):
    cur = conn.execute("INSERT INTO runs (kind, status, request, plan, started) VALUES (?, ?, ?, ?, ?)",
                       (kind, status, json.dumps(request, sort_keys=True) if request is not None else None,
                        json.dumps(plan, sort_keys=True) if plan is not None else None, now()))
    return cur.lastrowid


def finish_run(conn, run_id, status, items):
    conn.execute("UPDATE runs SET status = ?, items = ?, finished = ? WHERE id = ?",
                 (status, json.dumps(items), now(), run_id))


def get_run(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_runs(conn, limit=20):
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
