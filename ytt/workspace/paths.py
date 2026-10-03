"""Where the workspace lives. Precedence: explicit --workspace, then YTT_WORKSPACE, then ~/Videos/ytt."""
import os
from pathlib import Path

ENV_VAR = "YTT_WORKSPACE"
DB_NAME = "ytt.db"
CONFIG_NAME = "workspace.toml"
SUBFOLDERS = ("clips", "compilations", "cache", "logs")


def default_workspace():
    return Path.home() / "Videos" / "ytt"


def resolve(explicit=None, environ=None):
    environ = os.environ if environ is None else environ
    chosen = explicit or environ.get(ENV_VAR) or default_workspace()
    return Path(os.path.expanduser(str(chosen))).resolve()
