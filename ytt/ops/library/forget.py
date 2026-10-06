"""library forget: take compilations out of the library's records so their clips can be used again. This is the old
`make_compilations --forget`, which was needed when you deleted a compilation video you were not happy with and wanted
its clips back in the pool. The video file of the compilation is never touched here. A plan, then a run."""
from dataclasses import dataclass, field

from ytt.ops.compile import remake as remake_mod
from ytt.ops.errors import OpError
from ytt.ops.plan import COMPLETED, Action, Item, Plan, RunResult
from ytt.workspace import store


@dataclass
class ForgetPlan:
    plan: Plan
    rows: list = field(default_factory=list)       # the compilations to forget
    available: int = 0                             # clips that can be used again afterwards


def plan_forget(ws, targets):
    """targets: 'last', 'all', a number, a name or a list ('1,3'), like remake. Nothing is changed."""
    conn = ws.conn
    rows = remake_mod.resolve_targets(conn, targets)
    ids = {r["id"] for r in rows}
    plan = Plan(title="Forget " + ", ".join(r["name"] for r in rows))
    fp = ForgetPlan(plan, rows)
    freed = set()
    for row in rows:
        clips = [c for c in store.compilation_clips(conn, row["id"]) if c["clip_id"] is not None]
        back = 0
        for c in clips:
            others = conn.execute("SELECT 1 FROM compilation_clips WHERE clip_id = ? AND compilation_id NOT IN (%s)"
                                  % ",".join("?" * len(ids)), (c["clip_id"], *ids)).fetchone()
            if others is None and c["status"] == "ready" and ws.from_stored(c["path"]).is_file() \
                    and c["clip_id"] not in freed:
                freed.add(c["clip_id"])
                back += 1
        n = row["n_clips"]
        plan.actions.append(Action("forget", f"forget {row['name']}: {n} clip{'s' if n != 1 else ''}, "
                                             f"{back} can be used again", {"compilation": row["name"], "clips": n}))
        if row["output_path"] and ws.from_stored(row["output_path"]).exists():
            plan.notes.append(f"{row['name']}: the video file {ws.from_stored(row['output_path'])} stays where it is; "
                              f"delete it yourself if you do not want it.")
        children = [r for r in store.list_compilations(conn) if r["parent_id"] == row["id"] and r["id"] not in ids]
        if children:
            plan.notes.append(f"{', '.join(c['name'] for c in children)} {'was' if len(children) == 1 else 'were'} remade "
                              f"from {row['name']}; they stay, but lose that link (and keep their clips used).")
    fp.available = len(freed)
    plan.notes.append("Only the library's record is removed. Clips whose files are gone, or that another compilation "
                      "still uses, are not available again.")
    return fp


def run_forget(ws, fp):
    if not fp.plan.ok:
        raise OpError("The plan has errors; nothing was done.")
    conn = ws.conn
    run_id = store.create_run(conn, "forget", {"targets": [r["name"] for r in fp.rows]}, fp.plan.to_dict())
    items = []
    for row in fp.rows:
        conn.execute("UPDATE compilations SET parent_id = NULL WHERE parent_id = ?", (row["id"],))
        conn.execute("DELETE FROM compilations WHERE id = ?", (row["id"],))      # its clip list goes with it (cascade)
        items.append(Item(row["name"], COMPLETED, "forgotten"))
    store.finish_run(conn, run_id, COMPLETED, [{"what": i.what, "status": i.status, "detail": i.detail} for i in items])
    conn.commit()
    return RunResult(run_id, COMPLETED, items, [], {"forgotten": len(fp.rows), "available": fp.available})
