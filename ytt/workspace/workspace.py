"""A workspace: a folder holding ytt.db, workspace.toml and the media folders. The one place that knows about all three."""
import os
from contextlib import contextmanager
from pathlib import Path

from ytt.workspace import config as cfgmod
from ytt.workspace import db, paths, store
from ytt.workspace.errors import WorkspaceError


class Workspace:
    def __init__(self, root, conn, cfg):
        self.root = Path(root)
        self.conn = conn
        self.config = cfg

    # ---- creating and opening
    @classmethod
    def init(cls, root):
        """Create the workspace if it is not there; open it if it is. Safe to run again."""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for sub in paths.SUBFOLDERS:
            (root / sub).mkdir(exist_ok=True)
        config_path = root / paths.CONFIG_NAME
        if not config_path.exists():
            cfgmod.save(config_path, cfgmod.load(config_path))     # writes the defaults
        conn = db.connect(root / paths.DB_NAME)
        db.migrate(conn)
        return cls(root, conn, cfgmod.load(config_path))

    @classmethod
    def open(cls, root):
        root = Path(root)
        if not (root / paths.DB_NAME).exists():
            raise WorkspaceError(f"There is no ytt workspace at {root}. Create one with:  ytt workspace init")
        conn = db.connect(root / paths.DB_NAME)
        db.migrate(conn)
        return cls(root, conn, cfgmod.load(root / paths.CONFIG_NAME))

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- folders
    @property
    def clips_dir(self):
        return Path(self.config["clips_dir"]) if self.config["clips_dir"] else self.root / "clips"

    @property
    def compilations_dir(self):
        return Path(self.config["compilations_dir"]) if self.config["compilations_dir"] else self.root / "compilations"

    @property
    def cache_dir(self):
        return self.root / "cache"

    def to_stored(self, path):
        """How a file path is written to the database: relative to the workspace if inside it (so the whole
        folder can be moved), otherwise absolute."""
        p = Path(os.path.abspath(path))
        try:
            return p.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            try:
                return p.relative_to(self.root).as_posix()
            except ValueError:
                return str(p)

    def from_stored(self, text):
        p = Path(text)
        return p if p.is_absolute() else self.root / p

    # ---- settings
    def save_config(self):
        cfgmod.save(self.root / paths.CONFIG_NAME, self.config)

    # ---- data
    @contextmanager
    def transaction(self, dry_run=False):
        """Everything inside commits together, or not at all. With dry_run it always rolls back, so the
        real code can run without leaving a trace."""
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        if dry_run:
            self.conn.rollback()
        else:
            self.conn.commit()

    def counts(self):
        return store.counts(self.conn)
