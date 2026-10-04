"""The workspace database: one SQLite file, versioned with PRAGMA user_version.
Add a schema change by appending a migration to MIGRATIONS; never edit one that has shipped."""
import sqlite3

from ytt.workspace.errors import WorkspaceError

MIGRATIONS = [
    # ---- version 1
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE sources (
        id      INTEGER PRIMARY KEY,
        handle  TEXT NOT NULL UNIQUE            -- as the user gave it: @Name or a link
    );

    -- A video on YouTube. The YouTube id is its one identity. Knowing a video says nothing about a local file.
    CREATE TABLE videos (
        youtube_id  TEXT PRIMARY KEY,
        source_id   INTEGER REFERENCES sources(id),
        title       TEXT,
        views       INTEGER,
        duration    REAL,
        published   TEXT,
        first_seen  TEXT NOT NULL
    );

    -- A local file of a video.
    CREATE TABLE clips (
        id          INTEGER PRIMARY KEY,
        youtube_id  TEXT NOT NULL UNIQUE REFERENCES videos(youtube_id),
        path        TEXT NOT NULL,              -- relative to the workspace if inside it, else absolute
        status      TEXT NOT NULL CHECK (status IN ('ready', 'missing', 'failed')),
        added       TEXT NOT NULL
    );

    -- Styles are stored as JSON so the database does not need to know every rendering option.
    CREATE TABLE styles (
        id       INTEGER PRIMARY KEY,
        name     TEXT NOT NULL UNIQUE,
        data     TEXT NOT NULL,
        created  TEXT NOT NULL
    );

    CREATE TABLE compilations (
        id              INTEGER PRIMARY KEY,
        name            TEXT NOT NULL UNIQUE,
        output_path     TEXT,
        made            TEXT,
        style_snapshot  TEXT,                   -- JSON copy of the style used; NULL when it was never recorded
        request         TEXT,                   -- JSON of the make request; NULL when never recorded
        parent_id       INTEGER REFERENCES compilations(id),
        imported        INTEGER NOT NULL DEFAULT 0
    );

    -- The ordered clips of a compilation. clip_id is NULL only for an imported file that could not be
    -- tied to a YouTube video; legacy_name then keeps what the old tool called it.
    CREATE TABLE compilation_clips (
        compilation_id  INTEGER NOT NULL REFERENCES compilations(id) ON DELETE CASCADE,
        position        INTEGER NOT NULL,
        clip_id         INTEGER REFERENCES clips(id),
        legacy_name     TEXT,
        PRIMARY KEY (compilation_id, position)
    );
    """,
    # ---- version 2: runs. Every action command records what actually happened (DESIGN.md section 13).
    """
    CREATE TABLE runs (
        id        INTEGER PRIMARY KEY,
        kind      TEXT NOT NULL,                -- remake, make, fetch, ...
        status    TEXT NOT NULL CHECK (status IN ('planned', 'running', 'completed', 'partial', 'failed', 'cancelled')),
        request   TEXT,                         -- JSON: what was asked
        plan      TEXT,                         -- JSON: what the plan said would happen
        items     TEXT,                         -- JSON: one outcome per download/render
        started   TEXT NOT NULL,
        finished  TEXT
    );
    """,
    # ---- version 3: how a clip got into the library. Only 'fetched' clips (ytt downloaded them itself) may ever be
    # deleted by ytt; 'found' clips were already on the disk and belong to the person; 'imported' is the default for
    # clips that came from the old tool without saying (treated like 'found').
    """
    ALTER TABLE clips ADD COLUMN origin TEXT NOT NULL DEFAULT 'imported' CHECK (origin IN ('fetched', 'found', 'imported'));
    """,
]
LATEST = len(MIGRATIONS)


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn):
    """Bring the database up to date. Refuses a database made by a newer ytt rather than guessing."""
    current = version(conn)
    if current > LATEST:
        raise WorkspaceError(f"This workspace was made by a newer ytt (database version {current}, this ytt "
                             f"understands up to {LATEST}). Update ytt.")
    for v in range(current, LATEST):
        conn.executescript(MIGRATIONS[v])
        conn.execute(f"PRAGMA user_version = {v + 1}")
    conn.commit()
    return version(conn)
