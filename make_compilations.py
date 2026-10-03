#!/usr/bin/env python3
"""
make_compilations.py - turn a big pile of clips into finished compilation videos, hands-off.

Put this file in the same folder as stitch_videos.py (and make_transitions.py).

HOW TO USE
----------
  python make_compilations.py            first time: asks a few questions once, then asks how many to make
  python make_compilations.py 1          make 1 compilation now      (a good first test)
  python make_compilations.py 10         make 10
  python make_compilations.py all        make as many as your clips allow

It remembers your answers, and remembers which clips are already in a finished video, so
every run picks up exactly where the last one stopped. Stop it any time (Ctrl+C) and run it
again - nothing is lost, and nothing is made twice.

OTHER COMMANDS (all optional)
-----------------------------
  --setup            answer the questions again (change folder, clip count, transitions...)
  --dry-run          show what WOULD be made, without rendering anything
  --list             show the compilations made so far
  --forget 3         put compilation 3's clips back in the pool (e.g. you deleted that video)
  --redo last        remake a finished compilation from the SAME clips in the SAME order with your current
                     settings (try other transitions/quality). last | all | 3 | 1,3 ; the old video is kept
                     unless you add --replace. Combine with --setup to change the settings first.
  --retry-bad        try unreadable/corrupt clips again
  --include-leftover also make a final shorter video from clips that don't fill a whole one
  --reverse          play the whole sequence backwards (last clip first)
  --reverse-each     same clips in each compilation, but each one played backwards
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent          # settings, history and output live next to this script
STITCH = HERE / "stitch_videos.py"
SETTINGS_FILE = HERE / "compile_settings.json"
LEDGER_FILE = HERE / "compile_ledger.json"      # which clips went into which finished video
CACHE_FILE = HERE / "compile_cache.json"        # clip lengths, so big folders are only scanned once

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}
QUALITY = {"fast": ("veryfast", 21), "balanced": ("medium", 18), "best": ("slow", 16)}

DEFAULTS = {
    "clips_dir": "",
    "output_dir": str(HERE / "compilations"),
    "prefix": "compilation",
    "size_mode": "count",            # "count" = N clips per video, "minutes" = aim for N minutes per video
    "clips_per_video": 15,
    "minutes_per_video": 10,
    "order": "name",                 # name | oldest | newest | random
    "transition": "fade",            # any stitch_videos.py -t value: fade, random, cut, stinger, ...
    "transition_seconds": 1.0,
    "stinger_dir": "",
    "stinger_key": "auto",           # how transition VIDEOS are shown: auto | none | green | blue | black | white | RRGGBB
    "stinger_sim": 0.12,             # background-removal tolerance
    "stinger_blend": 0.05,           # background-removal edge softness
    "stinger_despill": False,        # remove the green/blue tint left on the edges
    "stinger_audio": True,           # mix in the transition videos' own sound
    "intro": "",
    "outro": "",
    "quality": "balanced",
    "seed": None,
}

sv = None   # stitch_videos module, imported in main() so a missing file gives a friendly message


# --------------------------------------------------------------------------- small helpers

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        backup = path.with_suffix(path.suffix + ".broken")
        try:
            shutil.copy(path, backup)
        except OSError:
            pass
        print(f"  note: {path.name} was unreadable; a copy was kept as {backup.name} and a fresh one started")
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)               # atomic: a crash can't leave a half-written history file


def clean_path(text):
    return os.path.expanduser(text.strip().strip('"').strip("'"))


def fmt_duration(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- questions

def ask_text(prompt, default=""):
    suffix = f" [{default}]" if default != "" else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans if ans else str(default)


def ask_int(prompt, default, lo=1, hi=100000):
    while True:
        ans = ask_text(prompt, default)
        try:
            val = int(ans)
            if lo <= val <= hi:
                return val
        except ValueError:
            pass
        print(f"   please type a whole number from {lo} to {hi}")


def ask_float(prompt, default, lo, hi):
    while True:
        ans = ask_text(prompt, default)
        try:
            val = float(ans)
            if lo <= val <= hi:
                return val
        except ValueError:
            pass
        print(f"   please type a number from {lo:g} to {hi:g}")


def ask_yes_no(prompt, default):
    return ask_text(f"{prompt} (y/n)", "y" if default else "n").lower().startswith("y")


def ask_choice(prompt, options, default_key):
    """options: list of (key, label). Returns the chosen key."""
    print(prompt)
    for i, (_, label) in enumerate(options, 1):
        print(f"   {i}) {label}")
    default_idx = [k for k, _ in options].index(default_key) + 1
    while True:
        ans = ask_text("   choose", default_idx)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        print(f"   please type a number from 1 to {len(options)}")


def ask_path(prompt, default="", kind="dir", optional=False):
    hint = " (Enter to skip)" if optional else ""
    while True:
        ans = clean_path(ask_text(prompt + hint, default))
        if not ans:
            if optional:
                return ""
            print("   this one is needed")
            continue
        p = Path(ans)
        if kind == "dir" and p.is_dir():
            return str(p.resolve())
        if kind == "file" and p.is_file():
            return str(p.resolve())
        print(f"   can't find that {'folder' if kind == 'dir' else 'file'}: {ans}")
        print("   (tip: you can drag the folder/file into this window to paste its path)")


# --------------------------------------------------------------------------- finding clips

def scan_clips(folder, skip_dir=None):
    """Every video under the folder (subfolders included), as (relative key, absolute path)."""
    folder = Path(folder)
    skip_dir = Path(skip_dir).resolve() if skip_dir else None
    found = []
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        if p.name.startswith(".") or ".part." in p.name:
            continue
        if skip_dir is not None and skip_dir in p.resolve().parents:
            continue
        found.append((p.relative_to(folder).as_posix(), p))
    return found


def used_keys(ledger):
    used = set(ledger["skipped"])
    for entry in ledger["compilations"].values():
        used.update(entry["clips"])
    return used


def order_pool(pool, s, n_used):
    """pool: list of (key, path) -> same list in the order clips should be used."""
    pool = sorted(pool, key=lambda kp: sv.natural_key(kp[1]))
    mode = s["order"]
    if mode == "oldest":
        pool.sort(key=lambda kp: kp[1].stat().st_mtime)
    elif mode == "newest":
        pool.sort(key=lambda kp: kp[1].stat().st_mtime, reverse=True)
    elif mode == "random":
        # seeded, so --dry-run and the real run agree on the same shuffle
        random.Random((s.get("seed") or 0) + n_used).shuffle(pool)
    return pool


def clip_info(path, cache):
    """Length, size and frame rate of a clip, remembered between runs. None if unreadable."""
    st = path.stat()
    c = cache.get(str(path))
    if c and c.get("size") == st.st_size and c.get("mtime") == int(st.st_mtime):
        return c
    try:
        clip = sv.probe(path)
    except SystemExit:
        return None
    c = {"size": st.st_size, "mtime": int(st.st_mtime), "duration": clip.duration,
         "w": clip.width, "h": clip.height, "fps": clip.fps}
    cache[str(path)] = c
    return c


def build_groups(pool, s, cache, max_videos, include_leftover):
    """Walk the ordered pool and cut it into compilations. Returns (groups, held_back, bad_clips)."""
    groups, cur, cur_secs, bad = [], [], 0.0, []
    by_count = s["size_mode"] == "count"
    target_n = s["clips_per_video"]
    target_secs = s["minutes_per_video"] * 60
    for key, path in pool:
        if max_videos is not None and len(groups) >= max_videos:
            break
        info = clip_info(path, cache)
        if info is None:
            bad.append(key)
            continue
        cur.append((key, path))
        cur_secs += info["duration"]
        full = len(cur) >= target_n if by_count else cur_secs >= target_secs
        if full and len(cur) >= 2:
            groups.append(cur)
            cur, cur_secs = [], 0.0
    held = cur
    if include_leftover and len(held) >= 2 and (max_videos is None or len(groups) < max_videos):
        groups.append(held)
        held = []
    return groups, held, bad


def next_number(ledger, out_dir, prefix):
    pat = re.compile(rf"^{re.escape(prefix)}_(\d+)")
    nums = [0]
    for name in ledger["compilations"]:
        m = pat.match(name)
        if m:
            nums.append(int(m.group(1)))
    if out_dir.is_dir():
        for f in out_dir.iterdir():
            m = pat.match(f.name)
            if m:
                nums.append(int(m.group(1)))
    return max(nums) + 1


# --------------------------------------------------------------------------- setup wizard

KEY_CHOICES = [("auto", "Detect it automatically from the video's first frame (works for green/blue screens only)"),
               ("none", "Remove nothing - the video plays as it is on top of the cut"),
               ("green", "Remove a green background"),
               ("blue", "Remove a blue background"),
               ("black", "Remove a black background"),
               ("white", "Remove a white background"),
               ("custom", "Remove another colour (you type it)")]


def ask_stinger_look(s):
    """How the transition videos are shown. Videos with real transparency (like the .mov files from
    make_transitions.py) need none of this; it is for videos on a plain coloured background."""
    cur = str(s["stinger_key"])
    named = [k for k, _ in KEY_CHOICES if k != "custom"]
    print("\n   How should the transition videos be shown? (ones with real transparency, like the .mov files\n"
          "   from make_transitions.py, ignore this; it is for videos on a plain coloured background)")
    pick = ask_choice("   Background of the transition videos:", KEY_CHOICES, cur if cur in named else "custom")
    if pick == "custom":
        while True:
            col = ask_text("   colour to remove, as 6 hex digits, e.g. FF00FF", cur if re.fullmatch(r"[0-9A-Fa-f]{6}", cur) else "")
            if re.fullmatch(r"#?[0-9A-Fa-f]{6}", col.strip()):
                s["stinger_key"] = col.strip().lstrip("#").upper()
                break
            print("   please type 6 hex digits like 00FF00")
    else:
        s["stinger_key"] = pick
    s["stinger_audio"] = ask_yes_no("   Play the transition videos' own sound?", s["stinger_audio"])
    if s["stinger_key"] != "none" and ask_yes_no("   Fine-tune the background removal (tolerance, edge softness, edge clean-up)?", False):
        s["stinger_sim"] = ask_float("   tolerance: how close to that colour still counts as background (0.01-0.50)",
                                     s["stinger_sim"], 0.01, 0.5)
        s["stinger_blend"] = ask_float("   edge softness (0.00-0.30)", s["stinger_blend"], 0.0, 0.3)
        s["stinger_despill"] = ask_yes_no("   Remove the green/blue tint left on edges?", s["stinger_despill"])


def wizard(s, ask_folder=True):
    print("\nLet's set this up once. Press Enter on any question to accept the [default].\n")
    q = [0]

    def nq():
        q[0] += 1
        return f"{q[0]}."

    while ask_folder:
        s["clips_dir"] = ask_path(f"{nq()} Folder with your videos", s["clips_dir"], "dir")
        n = len(scan_clips(s["clips_dir"]))
        if n >= 2:
            print(f"   found {plural(n, 'video')}\n")
            break
        q[0] -= 1
        print("   found fewer than 2 videos there - pick another folder\n")

    s["output_dir"] = str(Path(clean_path(ask_text(f"{nq()} Where should finished compilations go", s["output_dir"]))).resolve())
    print()

    s["size_mode"] = ask_choice(f"{nq()} How big should each compilation be?",
                                [("count", "A set number of clips"),
                                 ("minutes", "A rough length in minutes")], s["size_mode"])
    if s["size_mode"] == "count":
        s["clips_per_video"] = ask_int("   how many clips per compilation", s["clips_per_video"], 2, 200)
        if s["clips_per_video"] > 40:
            print("   note: very long compilations can render slowly and use a lot of memory; 10-30 clips is the comfortable range")
    else:
        s["minutes_per_video"] = ask_int("   about how many minutes per compilation", s["minutes_per_video"], 1, 600)
    print()

    s["order"] = ask_choice(f"{nq()} Which clips go first?",
                            [("name", "In number/name order (1, 2, 3 ... 1683 ...)"),
                             ("oldest", "Oldest first (by file date)"),
                             ("newest", "Newest first (by file date)"),
                             ("random", "Random")], s["order"])
    print()

    kind = ask_choice(f"{nq()} Transitions between clips?",
                      [("fade", "Smooth fade"),
                       ("random", "A mix of different transitions"),
                       ("cut", "Hard cuts (no transition)"),
                       ("stinger", "My own transition videos (from a folder)")],
                      s["transition"] if s["transition"] in ("fade", "random", "cut", "stinger") else "fade")
    s["transition"] = kind
    if kind == "stinger":
        s["stinger_dir"] = ask_path("   folder with your transition videos", s["stinger_dir"], "dir")
        ask_stinger_look(s)
    else:
        s["stinger_dir"] = ""
    print()

    s["intro"] = ask_path(f"{nq()} Intro video to put at the start of every compilation", s["intro"], "file", optional=True)
    s["outro"] = ask_path(f"{nq()} Outro video to put at the end of every compilation", s["outro"], "file", optional=True)
    print()

    s["quality"] = ask_choice(f"{nq()} Quality vs speed?",
                              [("fast", "Fast - quickest render, good for testing"),
                               ("balanced", "Balanced - recommended"),
                               ("best", "Best - slowest, highest quality")], s["quality"])

    if s["seed"] is None:
        s["seed"] = random.randrange(1 << 30)
    save_json(SETTINGS_FILE, s)
    print(f"\nSaved. From now on just run:  python {Path(__file__).name}\n"
          f"(to change any of this later:  python {Path(__file__).name} --setup)\n")


# --------------------------------------------------------------------------- rendering

def render(files, out_path, s, resolution_fps):
    """Run stitch_videos.py into a temporary file, and only rename it on success."""
    preset, crf = QUALITY[s["quality"]]
    part = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    cmd = [sys.executable, str(STITCH), *[str(f) for f in files], "-o", str(part), "--overwrite",
           "-t", s["transition"], "-d", str(s["transition_seconds"]),
           "--preset", preset, "--crf", str(crf)]
    if s["stinger_dir"]:
        cmd += ["--stinger-dir", s["stinger_dir"], "--key", str(s["stinger_key"]),
                "--key-similarity", str(s["stinger_sim"]), "--key-blend", str(s["stinger_blend"])]
        if s["stinger_despill"]:
            cmd.append("--despill")
        if not s["stinger_audio"]:
            cmd.append("--no-stinger-audio")
    if resolution_fps:
        cmd += ["--resolution", resolution_fps[0], "--fps", resolution_fps[1]]
    try:
        # stdout (the long plan listing) is hidden; ffmpeg's progress and any errors still show
        rc = subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode
    except KeyboardInterrupt:
        part.unlink(missing_ok=True)
        raise
    if rc == 0 and part.exists():
        os.replace(part, out_path)
        return True
    part.unlink(missing_ok=True)
    return False


def render_one(group, out_path, s, cache):
    """Render one compilation from [(key, path), ...]. The output size/frame rate is whatever most of the
    clips use (not the intro, not a random first clip). Returns True on success."""
    intro = [Path(s["intro"])] if s["intro"] else []
    outro = [Path(s["outro"])] if s["outro"] else []
    infos = [clip_info(p, cache) for _, p in group]
    size = Counter((i["w"], i["h"]) for i in infos).most_common(1)[0][0]
    fps = Counter(str(i["fps"]) for i in infos).most_common(1)[0][0]
    return render(intro + [p for _, p in group] + outro, out_path, s, (f"{size[0]}x{size[1]}", fps))


# --------------------------------------------------------------------------- commands

def cmd_list(ledger):
    comps = ledger["compilations"]
    if not comps:
        print("Nothing made yet.")
        return
    total = sum(len(e["clips"]) for e in comps.values())
    print(f"{plural(len(comps), 'compilation')} made, using {plural(total, 'clip')} in total:\n")
    for name, e in sorted(comps.items()):
        exists = "" if Path(e["file"]).exists() else "   (file no longer there)"
        print(f"  {name}   {plural(len(e['clips']), 'clip')}   {e['made']}{exists}")
        print(f"      {e['clips'][0]}  ...  {e['clips'][-1]}")
    if ledger["skipped"]:
        print(f"\n{plural(len(ledger['skipped']), 'unreadable clip')} skipped (use --retry-bad to try again)")


def cmd_forget(ledger, which):
    comps = ledger["compilations"]
    name = which if which in comps else next((n for n in comps if re.fullmatch(rf".*_0*{re.escape(which)}", n)), None)
    if name is None:
        sys.exit(f"No compilation matches '{which}'. Use --list to see them.")
    n = len(comps[name]["clips"])
    del comps[name]
    save_json(LEDGER_FILE, ledger)
    print(f"{name} forgotten: its {plural(n, 'clip')} are available again for the next run.")
    print("(the video file itself was not touched - delete it yourself if you don't want it)")


def _num_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def resolve_redo(ledger, spec):
    """'last' | 'all' | '3' | 'compilation_003' | '1,3' -> names of finished compilations, oldest first."""
    comps = ledger["compilations"]
    if not comps:
        sys.exit("Nothing has been made yet, so there is nothing to remake. (--list shows what exists)")
    ordered = sorted(comps, key=_num_key)
    chosen = []
    for part in [p.strip() for p in str(spec).split(",") if p.strip()]:
        if part.lower() == "all":
            found = ordered
        elif part.lower() == "last":
            found = [ordered[-1]]
        elif part in comps:
            found = [part]
        else:
            found = [n for n in ordered if re.fullmatch(rf".*_0*{re.escape(part)}", n)][:1]
        if not found:
            sys.exit(f"No compilation matches '{part}'. Use --list to see them.")
        chosen += [n for n in found if n not in chosen]
    return sorted(chosen, key=_num_key)


def cmd_redo(args, s, ledger, clips_dir, cache):
    """Remake finished compilations from the same clips in the same order, with the current settings."""
    names = resolve_redo(ledger, args.redo)
    out_dir = Path(s["output_dir"])
    plan = []
    for name in names:
        group = [(k, Path(clips_dir) / k) for k in ledger["compilations"][name]["clips"]]
        gone = [k for k, p in group if not p.is_file()]
        if gone:
            sys.exit(f"Can't remake {name}: {plural(len(gone), 'of its clip')} no longer in {clips_dir}:\n   "
                     + ", ".join(gone[:5]) + (" ..." if len(gone) > 5 else "")
                     + "\n   (python auto_compile.py --redo ... downloads deleted clips again for you)")
        if args.reverse or args.reverse_each:
            group = group[::-1]
        plan.append((name, group))

    print(f"Remaking {plural(len(plan), 'compilation')} from the same clips"
          + (" (played backwards)" if args.reverse or args.reverse_each else "")
          + (", replacing the old video(s)." if args.replace else ". The old video(s) are kept."))
    if args.dry_run:
        n0 = next_number(ledger, out_dir, s["prefix"])
        for i, (name, group) in enumerate(plan):
            target = name if args.replace else "%s_%03d" % (s["prefix"], n0 + i)
            print(f"  {name}  ({plural(len(group), 'clip')})  ->  {target}")
        print("\n(dry run - nothing was rendered or saved)")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    made, failed, started = [], [], time.time()
    try:
        for gi, (old, group) in enumerate(plan, 1):
            if args.replace:
                name = old
                out_path = Path(ledger["compilations"][old].get("file") or out_dir / f"{old}.mp4")
            else:
                name = f"{s['prefix']}_{next_number(ledger, out_dir, s['prefix']):03d}"
                out_path = out_dir / f"{name}.mp4"
            print(f"\n[{gi}/{len(plan)}] {name}  ({plural(len(group), 'clip')}, same as {old})")
            if render_one(group, out_path, s, cache):
                keys = [k for k, _ in group]
                ledger["compilations"][name] = {"clips": keys, "file": str(out_path),
                                                "made": datetime.now().strftime("%Y-%m-%d %H:%M")}
                save_json(LEDGER_FILE, ledger)
                out_path.with_suffix(".txt").write_text("\n".join(keys) + "\n", encoding="utf-8")
                made.append(name)
                print("   done.")
            else:
                failed.append(old)
                print(f"   FAILED: {old} was not remade (see the message above). The old one is untouched.")
    except KeyboardInterrupt:
        print("\n\nStopped. Everything finished so far is saved.")
    finally:
        save_json(CACHE_FILE, cache)
    print("\n" + "-" * 50)
    print(f"Remade {plural(len(made), 'compilation')} in {fmt_duration(time.time() - started)}.")
    if made:
        print(f"Find them in: {out_dir}")
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed)}.")


# --------------------------------------------------------------------------- main

def main():
    global sv
    ap = argparse.ArgumentParser(description="Make many compilations from a big folder of clips, hands-off.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("count", nargs="?", metavar="HOW_MANY",
                    help="a number, or 'all' - how many compilations to make now (default: asks)")
    ap.add_argument("--setup", action="store_true", help="answer the setup questions again")
    ap.add_argument("--dry-run", action="store_true", help="show the plan without rendering anything")
    ap.add_argument("--list", action="store_true", help="show compilations made so far")
    ap.add_argument("--forget", metavar="N", help="free the clips of compilation N so they can be used again")
    ap.add_argument("--redo", metavar="WHICH",
                    help="remake finished compilation(s) from the same clips in the same order with your current "
                         "settings: last, all, a number, or a list like 1,3")
    ap.add_argument("--replace", action="store_true", help="with --redo: overwrite the old video instead of keeping it")
    ap.add_argument("--retry-bad", action="store_true", help="try previously unreadable clips again")
    ap.add_argument("--include-leftover", action="store_true",
                    help="also make a final shorter video from clips that don't fill a whole one")
    ap.add_argument("--clips", metavar="FOLDER", help="use this folder for this run instead of your saved one")
    ap.add_argument("--order", choices=["name", "oldest", "newest", "random"],
                    help="use this clip order for this run only")
    ap.add_argument("--reverse", action="store_true",
                    help="flip the whole clip sequence (last clip first) for this run")
    ap.add_argument("--reverse-each", action="store_true",
                    help="keep the same clips in each compilation but play each one backwards")
    args = ap.parse_args()

    if not STITCH.is_file():
        sys.exit(f"Error: stitch_videos.py must be in the same folder as this script ({HERE})")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Error: ffmpeg/ffprobe not found on PATH")
    sys.path.insert(0, str(HERE))
    import stitch_videos
    sv = stitch_videos

    ledger = load_json(LEDGER_FILE, {})
    ledger.setdefault("compilations", {})
    ledger.setdefault("skipped", {})

    if args.replace and not args.redo:
        sys.exit("Error: --replace only works together with --redo")
    if args.list:
        return cmd_list(ledger)
    if args.forget:
        return cmd_forget(ledger, args.forget)
    if args.retry_bad:
        n = len(ledger["skipped"])
        ledger["skipped"] = {}
        save_json(LEDGER_FILE, ledger)
        print(f"{plural(n, 'clip')} will be tried again.")

    s = {**DEFAULTS, **load_json(SETTINGS_FILE, {})}
    if args.clips and not Path(clean_path(args.clips)).is_dir():
        sys.exit(f"Error: --clips folder not found: {args.clips}")
    folder_ok = bool(args.clips) or (bool(s["clips_dir"]) and Path(s["clips_dir"]).is_dir())
    if args.setup or not SETTINGS_FILE.exists() or not folder_ok:
        if SETTINGS_FILE.exists() and not args.setup and not folder_ok:
            print(f"The videos folder '{s['clips_dir']}' can't be found - let's set it again.")
        wizard(s, ask_folder=not args.clips)
    clips_dir = str(Path(clean_path(args.clips)).resolve()) if args.clips else s["clips_dir"]
    if args.order:
        s["order"] = args.order

    for label, key in (("intro", "intro"), ("outro", "outro")):
        if s[key] and not Path(s[key]).is_file():
            sys.exit(f"Error: your {label} video is missing: {s[key]}\n       (fix with --setup)")

    out_dir = Path(s["output_dir"])
    cache = load_json(CACHE_FILE, {})

    if args.redo:
        return cmd_redo(args, s, ledger, clips_dir, cache)

    # ---- what is new?
    all_clips = scan_clips(clips_dir, skip_dir=out_dir)
    used = used_keys(ledger)
    pool = [(k, p) for k, p in all_clips if k not in used]
    print(f"{plural(len(all_clips), 'video')} in {Path(clips_dir).name or clips_dir}, {len(pool)} not used yet.")
    if len(pool) < 2:
        print("Nothing new to make. Add more clips to the folder and run this again.")
        return

    # ---- how many to make this run?
    if args.count is None:
        if sys.stdin.isatty() and not args.dry_run:
            if s["size_mode"] == "count":
                est = len(pool) // s["clips_per_video"]
                print(f"That's enough for about {plural(est, 'compilation')}.")
            ans = ask_text("How many compilations do you want to make now? (a number, or 'all')", "1").lower()
            args.count = ans
        else:
            args.count = "1" if not args.dry_run else "all"
    if str(args.count).lower() == "all":
        max_videos = None
    else:
        try:
            max_videos = max(1, int(args.count))
        except ValueError:
            sys.exit("Error: HOW_MANY must be a number or 'all'")

    pool = order_pool(pool, s, len(used))
    if args.reverse:
        pool.reverse()
    print("Checking clips..." if max_videos is None or max_videos > 3 else "", end="", flush=True)
    groups, held, bad = build_groups(pool, s, cache, max_videos, args.include_leftover)
    if args.reverse_each:
        groups = [g[::-1] for g in groups]
    save_json(CACHE_FILE, cache)
    print("\r" + " " * 20 + "\r", end="")

    if bad:
        print(f"Skipping {plural(len(bad), 'unreadable clip')}: " + ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else ""))
    if not groups:
        print("Not enough clips to make a full compilation yet "
              f"({plural(len(held), 'clip')} waiting). Add more clips, or use --include-leftover.")
        return

    per = f"{s['clips_per_video']} clips" if s["size_mode"] == "count" else f"about {s['minutes_per_video']} min"
    shorter = len(groups[-1]) < len(groups[0]) if s["size_mode"] == "count" else False
    print(f"\nPlan: {plural(len(groups), 'compilation')} of {per} each"
          + (" (the last one is shorter)" if shorter else "")
          + (f", {plural(len(held), 'leftover clip')} wait for next time" if held and max_videos is None else "") + ".")

    if args.dry_run:
        n0 = next_number(ledger, out_dir, s["prefix"])
        for i, g in enumerate(groups):
            print(f"\n  {s['prefix']}_{n0 + i:03d}  ({plural(len(g), 'clip')})")
            for k, _ in g:
                print(f"     {k}")
        print("\n(dry run - nothing was rendered or saved)")
        return

    # ---- render
    out_dir.mkdir(parents=True, exist_ok=True)
    made, failed, started = [], [], time.time()
    try:
        for gi, group in enumerate(groups, 1):
            name = f"{s['prefix']}_{next_number(ledger, out_dir, s['prefix']):03d}"
            out_path = out_dir / f"{name}.mp4"
            print(f"\n[{gi}/{len(groups)}] {name}  ({plural(len(group), 'clip')})")
            if render_one(group, out_path, s, cache):
                keys = [k for k, _ in group]
                ledger["compilations"][name] = {"clips": keys, "file": str(out_path),
                                                "made": datetime.now().strftime("%Y-%m-%d %H:%M")}
                save_json(LEDGER_FILE, ledger)          # saved after EVERY video, so a crash loses nothing
                (out_dir / f"{name}.txt").write_text("\n".join(keys) + "\n", encoding="utf-8")
                made.append(name)
                left = len(groups) - gi
                if left:
                    per_video = (time.time() - started) / gi
                    print(f"   done. About {fmt_duration(per_video * left)} left for the remaining {left}.")
                else:
                    print("   done.")
            else:
                failed.append(name)
                print(f"   FAILED: {name} was not made (see the message above). Moving on.")
    except KeyboardInterrupt:
        print("\n\nStopped. Everything finished so far is saved - run this again to carry on.")
    finally:
        for key in bad:
            ledger["skipped"][key] = "unreadable"
        save_json(LEDGER_FILE, ledger)
        save_json(CACHE_FILE, cache)

    print("\n" + "-" * 50)
    print(f"Made {plural(len(made), 'compilation')} in {fmt_duration(time.time() - started)}.")
    if made:
        print(f"Find them in: {out_dir}")
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed)}. Their clips are still unused, so running again will retry them.")


if __name__ == "__main__":
    main()
