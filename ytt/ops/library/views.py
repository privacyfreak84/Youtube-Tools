"""What is in the library: plain read-only views for the interface to show (DESIGN.md section 11).
Each returns lists/dicts of plain values; nothing here prints."""
import json
import os

from ytt.workspace import store


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def clips(ws, status=None, unused=False):
    out = []
    for r in store.list_clips(ws.conn):
        if status and r["status"] != status:
            continue
        if unused and (r["used"] or r["status"] != "ready"):
            continue
        out.append({"id": r["id"], "youtube_id": r["youtube_id"], "title": r["title"], "views": r["views"],
                    "duration": r["duration"], "status": r["status"], "used": bool(r["used"]),
                    "path": str(ws.from_stored(r["path"]))})
    return out


def compilations(ws):
    out = []
    for r in store.list_compilations(ws.conn):
        output = ws.from_stored(r["output_path"]) if r["output_path"] else None
        out.append({"name": r["name"], "clips": r["n_clips"], "made": r["made"],
                    "style_recorded": bool(r["style_snapshot"]), "imported": bool(r["imported"]),
                    "parent": r["parent_name"], "output": str(output) if output else None,
                    "output_exists": bool(output and output.is_file())})
    return out


def sources(ws):
    rows = ws.conn.execute("""SELECT s.handle,
                                     (SELECT COUNT(*) FROM videos v WHERE v.source_id = s.id) AS videos,
                                     (SELECT COUNT(*) FROM clips c JOIN videos v USING (youtube_id)
                                      WHERE v.source_id = s.id) AS clips
                              FROM sources s ORDER BY s.handle""").fetchall()
    return [{"handle": r["handle"], "videos": r["videos"], "clips": r["clips"]} for r in rows]


def runs(ws, limit=20):
    out = []
    for r in store.list_runs(ws.conn, limit):
        items = json.loads(r["items"]) if r["items"] else []
        out.append({"id": r["id"], "kind": r["kind"], "status": r["status"], "started": r["started"],
                    "finished": r["finished"], "items": items})
    return out


def stats(ws):
    c = clips(ws)
    ready = [x for x in c if x["status"] == "ready"]
    comps = compilations(ws)
    return {
        "clips": len(c),
        "ready": len(ready),
        "missing": sum(1 for x in c if x["status"] == "missing"),
        "failed": sum(1 for x in c if x["status"] == "failed"),
        "used": sum(1 for x in c if x["used"]),
        "unused": sum(1 for x in ready if not x["used"]),
        "compilations": len(comps),
        "compilations_missing_file": sum(1 for x in comps if x["output"] and not x["output_exists"]),
        "sources": len(sources(ws)),
        "videos": ws.counts()["videos"],
        "clips_bytes": sum(_size(x["path"]) for x in ready),
        "compilations_bytes": sum(_size(x["output"]) for x in comps if x["output"]),
    }
