"""Video files on disk: finding ones you already have, and downloading one clip so that a crash or Ctrl-C can never
leave a half-written file that looks finished (DESIGN.md section 13)."""
import os
import re
from pathlib import Path

from ytt.sources.errors import DownloadError, DownloadStopped

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}
PARTIAL_DIR = ".partial"          # downloads happen in here, next to where the file will end up
_STREAM_LEFTOVER = re.compile(r"\.f\d+\.")


def video_files(folder):
    """Every finished-looking video under the folder, subfolders included. Hidden files and folders (our own
    .partial among them) and per-stream leftovers of unfinished downloads are not counted."""
    folder = Path(folder)
    out = []
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        rel = p.relative_to(folder).parts
        if any(part.startswith(".") for part in rel) or ".part." in p.name or _STREAM_LEFTOVER.search(p.name):
            continue
        out.append(p)
    return out


def find_on_disk(folders, wanted_ids):
    """Video files in these folders whose name contains one of the wanted video ids -> {id: path}. Works for our
    names (<id>.mp4, the old STAMP_0001_<id>.mp4) and yt-dlp's usual 'Title [<id>].mp4'. An id is 11 characters,
    so a stray match inside some other name is practically impossible."""
    found = {}
    for folder in folders:
        if not Path(folder).is_dir():
            continue
        for path in video_files(folder):
            name = path.name
            for i in range(len(name) - 10):
                vid = name[i:i + 11]
                if vid in wanted_ids and vid not in found:
                    found[vid] = path
    return found


def finished_file(folder, stem):
    """The finished file of a download in this folder (ignores .part files and per-stream leftovers), or None."""
    good = [p for p in Path(folder).glob(glob_escape(stem) + ".*")
            if p.suffix.lower() in VIDEO_EXTS and not _STREAM_LEFTOVER.search(p.name) and p.stat().st_size > 0]
    return max(good, key=lambda p: p.stat().st_size) if good else None


def glob_escape(text):
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def clean_partials(folder, stem):
    """Remove everything a download of `stem` left in its work folder (the folder is ours alone)."""
    for p in Path(folder).glob(glob_escape(stem) + "*"):
        try:
            p.unlink()
        except OSError:
            pass


def _why(e):
    text = str(e).splitlines()[0][:150] if str(e) else ""
    return text or "download failed"


def download_clip(backend, video_id, folder, *, exact_path=None, max_height=1080, stop=None):
    """Download one video and put its file in `folder` under the name <id>.<ext> (or at exact_path, which is how a
    clip that is being fetched again keeps the name the library recorded). Returns (final path, info dict).

    The download runs in folder/.partial and the finished file is moved into place in one step, so what is in
    `folder` is always complete. Raises DownloadError when it fails and DownloadStopped when `stop` was set;
    whatever the download left behind is removed either way."""
    work = Path(folder) / PARTIAL_DIR
    work.mkdir(parents=True, exist_ok=True)
    stem = video_id
    try:
        info = backend.download(video_id, str(work / (stem + ".%(ext)s")), max_height, stop) or {}
        got = finished_file(work, stem)
        if got is None:
            raise DownloadError("it finished but no video file was found")
        final = Path(exact_path) if exact_path else Path(folder) / (video_id + got.suffix.lower())
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(got, final)
        return final, info
    except (DownloadStopped, DownloadError):
        raise
    except Exception as e:                      # (KeyboardInterrupt is not an Exception and passes through)
        raise DownloadError(_why(e)) from e
    finally:
        clean_partials(work, stem)
