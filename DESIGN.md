# ytt — design

Status: **draft 2. Steps 1-6 of section 20 are built; steps 7 and 8 are not.**
Replaces the current scripts (`auto_compile.py`, `make_compilations.py`, `stitch_videos.py`, `make_transitions.py`,
`yt_toolkit.py`) with one tool. The tested rendering engine is kept and wrapped.

## 1. Why

The scripts grew one at a time, so the user has to know how they happen to be built:

- `auto_compile` runs `make_compilations` as a subprocess, which runs `stitch_videos` as another. Flags such as
  `--redo`, `--setup`, `--dry-run`, `--reverse` exist at more than one layer.
- `auto_compile` alone has 30 flags. Several are synonyms (`--top` = `--pick new:N`, `--include-leftover` =
  `--leftover short`, `-n` = `--pick comps:N`).
- State is spread over five JSON files next to the scripts. Two are settings files, and a hack keeps
  "download folder" and "clips folder" in agreement.
- One-line mode, `--again` and interactive mode handle saved state differently. The stale-date bug came from that.
- The interface speaks in internal words: pick, comps, adopt, ledger, leftover.

## 2. Rules (everything else follows from these)

1. **Organise around what the user is trying to do**, not around how the old scripts implemented it.
2. **One canonical implementation per operation.** Guided menu, command line and scripts are three ways of filling in
   the same request, never three implementations.
3. **Internal concepts do not leak into the interface** just because they exist in the code. The database schema,
   class names, module names and old script boundaries never dictate the user's model.
4. **Every dependency between areas must be justified.** "It's convenient" is not a justification.

Master test for any new feature: *where does it belong in the user's mental model?* If it has no home, the model is
wrong; do not bolt a flag onto the nearest command.

## 3. What ytt is

One tool for working with YouTube content from a local workspace, with **two peer areas**:

```
   RESEARCH                                COLLECT & COMPILE
   produces information                    consumes information
   channels · outliers · table             fetch · make · remake · library · style
   live · tags · niche · clip              stitch
         \                                       /
          \____ foundation (small, listed) _____/
```

- **Research works with zero local state.** It needs no workspace and no library, and it never reads or writes
  library tables. It may use the workspace's cache folder if one exists.
- **Compiling works with zero research.** Nothing in `make` or `fetch` calls research code.
- They meet at one explicit point (section 14): a plain list of YouTube video ids.

**The foundation is only these things, and nothing else may be added to it without a written reason:**
YouTube identity and metadata (the video id, listing a channel, filtering), download access (yt-dlp), the workspace
(config, database, paths), and the request/plan/run framework. Research logic, compilation logic, selection
policies and rendering policies are *not* foundation. There is no `utils.py`.

## 4. What the user thinks ytt manages

| Thing | Meaning |
|---|---|
| **Workspace** | The folder where everything lives. |
| **Source** | A YouTube channel (handle or link). |
| **Clip** | A video you have downloaded into the library. |
| **Compilation** | A finished video made from clips, with a recorded recipe. |
| **Style** | A named description of how a compilation is rendered. |

A *video* is something on YouTube (research finds videos). A *clip* is the local file of one. The same video id
identifies both, so research and the library never get tangled: knowing a video exists says nothing about whether a
file does.

Research output is not an entity. It is a view: a table, or `--json` / `--csv`.

Not in the user's vocabulary: ledger, archive, adopt, pick, comps, leftover, redo mode, setup mode.

**Identity rule: the YouTube video id is the one canonical identity of a video and its clip.** Not the file name,
URL, title, position in a list, or download path; those are attributes.

## 5. The pipeline

Every command that *changes something* (`fetch`, `make`, `remake`, `library remove`, `style` edits) goes through:

```
REQUEST → RESOLVE → VALIDATE → PLAN → CONFIRM → EXECUTE → RECORD
```

- **Request**: what was asked, one structured object, whichever interface produced it.
- **Resolve**: look things up (channel list, what the library already has) and fill in defaults.
- **Validate**: find problems *before* anything happens (see below).
- **Plan**: an explicit description of what will happen.
- **Confirm**: shown unless `--yes`.
- **Execute**: download, render, delete, with the failure rules in section 13.
- **Record**: write what actually happened.

Read-only commands (`research ...`, `library` views, `doctor`) skip plan and confirm: request → run → render.

**The Plan is a real object** (a dataclass that can be turned into a dict/JSON), not text assembled for printing.
It has typed actions (download, render, delete, replace output), inputs, outputs, estimates, and three kinds of
message:

- **Errors** block execution (no ffmpeg; output folder not writable; nothing matched the selection).
- **Warnings** are shown and need an explicit yes (3 selected videos are known to be unavailable; this compilation
  reuses 3 clips already used elsewhere). `--yes` proceeds past warnings but never past errors.
- **Notes** are information only.

`--dry-run` is "build the plan, render it, exit". It has no code of its own. A finished run stores the plan it ran.

**Validate** covers what can be known without doing the work: ffmpeg/ffprobe present (and `xfade` when needed),
output folder writable and enough disk for the estimate, assets a style refers to exist (stinger folder), clip files
for a `remake` present or re-fetchable, the selection non-empty. It cannot know whether YouTube will serve a
download; that is handled by failure rules, not pretended away.

```
 Plan
   Source        @NestleCrunch · shorts · 2025-01-01 → 2025-12-31
   Selection     most viewed · 45 clips
   Library       29 already here · 16 to download
   Compilations  3 × 15 clips · style "default"
   Output        ~/Videos/ytt/compilations/
 Warnings
   2 selected videos are no longer available; replaced by the next in line
 Proceed? [Y/n]
```

## 6. Three ways in, one engine

| Level | How | Behaviour |
|---|---|---|
| Guided | `ytt` | Keyboard-driven menus (arrows, space, enter). Builds a request, then the normal plan screen. |
| Explicit | `ytt make @chan --clips 45 -n 3` | The same request from flags. Asks only to confirm. |
| Script | `ytt make ... --yes` (`--json` on list/research/library) | Never prompts. Non-zero exit if anything failed. |

Interactive mode is a *request builder* and nothing more. Every menu question has a matching flag, and the other
way round. The menu never shows list-position syntax; it asks plain questions ("How many clips?").

**The guided menu, as built (step 6).** `ytt` with no arguments at a terminal starts it; without a terminal (a pipe, a
script) it prints help, so a script never waits for an answer.

- *One way in.* The menu turns its answers into the command line the flags would have made, as a list of words, and
  runs that list through the same parser and the same command functions (`cli.run_argv`). So a menu answer and a flag
  cannot differ, and the plan screen, the "Proceed? [Y/n]", the progress and the exit codes are the command's own.
  Before running, the menu prints `Command line for this:  ytt make @Chan --clips 45 ...`, so the flags can be learned
  from it. Tests check the exact words for every flow and that the real parser turns them into the request the answers
  describe.
- *Entries.* Make compilations (from a channel, from the library, from a list of videos in a file, or `--like` an
  earlier make), Get clips only, Remake, Look at the library (summary, clips, compilations, sources, runs, forget),
  Styles (list, show, change or make new, delete), Workspace and settings (show, change a setting, bring in the old
  scripts' state with a preview first). With no workspace yet it offers to set one up, empty or with the old state.
- *Questions.* Only what has to be asked: where the clips come from, which channel, videos or shorts, which come first,
  how much (a number of compilations, a number of new clips, or one stretch of the list as two plain numbers). Then two
  tick-lists, "Narrow it down?" (dates, views, length) and "Change anything else?" (style, size, clip order,
  direction, what to do with leftovers, deleting used clips, retrying unreadable clips, look-alikes, refetch, picture
  quality); nothing ticked means the workspace's usual settings, and each question starts at the usual value. Last:
  "Ready. What next?": show the plan, show every detail of the plan and do nothing (`--dry-run`), or go back.
- *Not in the menu, on purpose:* `--take` (the advanced grammar), `--yes`, `--json`, `--workers` (a workspace setting,
  reachable under Workspace and settings), and changing the selection of a `--like` (type it as flags).
- *Keys.* Arrows and enter choose, space ticks, typing answers text (a wrong answer is refused with a reason and asked
  again), Ctrl-C goes back one step, and at the main menu quits. `questionary` is used only in `ytt/ui/prompts.py` and
  `rich` only for the header, so every command works without either installed; the menu says what to install if they
  are missing.

**Menu additions (step 7).** Four entries join the main menu: *Research YouTube* (a second menu with the seven tools,
each asking only what differs from the usual), *Join any videos into one file* (stitch), *Check that everything works*
(doctor, no questions) and, under Styles, *Make transition videos* (stingers). They follow the same rule as every other
flow: the answers become the command line the flags would have made, shown before it runs, and wrong answers are refused
with a reason. Research ends with "Also save the results to a CSV file?". The research tools that find videos
(outliers, niche, clip, table, live) always save their rows (to `cache/last-research.csv`, or to the file the person
asked for), and when the command worked and found videos the menu asks "Make a compilation from these N video(s)?":
the ids are written to `cache/research-ids.txt` and the normal make questions follow with `make --videos FILE`. That is
the handoff of section 14. Stitch and stingers have no confirmation of their own, so their last question is "Make it /
show what would be done and do nothing / go back".

## 7. Commands

```
ytt                       guided front door
ytt make [@chan]          fetch what is missing, then compile
ytt fetch [@chan]         only get clips into the library
ytt remake ID             render a recorded compilation again, with changes
ytt library [clips|compilations|sources|stats|runs|forget]
ytt style [list|show|edit|set|delete|stingers]
ytt research <channels|outliers|table|live|tags|niche|clip> ...
ytt stitch FILES... -o OUT      expert utility: join any videos (not part of the normal flow)
ytt workspace [show|init|set]   paths, defaults, workers, cookies
ytt doctor
```

Every top-level command is a distinct user goal; a new one needs a goal none of these covers. There is no `tools`
namespace and no separate `config`. The stinger generator (today's `make_transitions.py`) lives at
`ytt style stingers`, because stingers are style assets.

- `make` with no channel uses clips already in the library.
- `fetch` is `make` without the compile step (today's `--no-compile`).
- `research` keeps today's `yt_toolkit` tools, unchanged in what they do.

**Step 7, as built.**

- `ytt research channels|outliers|table|live|tags|niche|clip`. Every tool works with no workspace (cookie settings are
  read from the workspace's `workspace.toml` when there is one; `--cookies-from-browser` and `--cookies` override), only
  reads from YouTube, and ends the same way: a table by default, `--json` (`{"results", "ids", "notes"}`; `tags` adds the
  coverage numbers, `channels` has no ids), `--ids` (one id per line, for `make --videos -`; not on `channels` and `tags`),
  and `--csv FILE`, which saves the rows as well. Notes (a channel that was skipped, and why) go to stderr in brackets
  and into the JSON. Nothing found: a plain message on stderr, exit code 1, and valid empty JSON if `--json` was asked.
- *Changed from `yt_toolkit` on purpose:* `--csv` always means "also save the rows" (it used to be the input of `tags`;
  that is now `--from-csv`, and `tags --videos FILE|-` reads a list of ids); `tags` takes video ids as well as links
  and only YouTube videos; `live` prints a table and keeps the old one-JSON-file-per-channel output behind
  `--out-dir DIR`; a title in a table is shortened to fit but never in `--json` or `--csv`; a CSV column `id` is added
  at the end; `table` gives `upload_date` as an ISO date with a separate `approx` flag (not `2026-01-02~`) in
  `--json`/`--csv` while the table still shows the `~`; channel links to `/channels`, `/about` or `/live` now resolve
  to the channel everywhere.
- `ytt stitch FILES... -o OUT`: the engine behind a request, plan, run of its own; works with no workspace. It keeps
  every flag of `stitch_videos.py` except the two interactive ones, `--pick-transitions` and `--reorder` (`--custom` and
  `--order` say the same without asking). It refuses to replace a file without `--overwrite` and refuses an output that
  is one of its inputs, so it never asks for a confirmation.
- `ytt doctor [--offline]`: see section 17. `ytt style stingers`: see section 9.

## 8. Selection

"Which videos?" is one coherent group. The public vocabulary is deliberately plain:

```
--type shorts|videos        --from DATE  --to DATE
--min-views N               --min-length M  --max-length M
--sort popular|unpopular|oldest|latest|longest|shortest|title|random
--clips N                   N clips you don't have yet, in that order, skipping what the library has
-n N                        enough new clips for N full compilations
--range 25-70               exactly those positions in the filtered, sorted list (stable between runs)
```

The full existing grammar (`last:20`, `every:5`, `random:30`, `1-10,25`, ...) survives as one **advanced** flag,
`--take EXPR`, documented separately. The guided menu never exposes it. Videos already in the library are skipped
without any "adopt"/"archive" step, because the database knows every video id. Likely re-uploads (same title and
length) are flagged and skipped unless `--keep-duplicates`.

Behaviour change to confirm: today `--pick 60` means "the first 60 positions, then skip the ones you have", which can
download fewer than 60. `--clips 60` means 60 *new* clips (today's `new:60`); stable positions are `--range`.

Details pinned down by reading the code:

- `--clips N` means "download N more". Clips already waiting in the library are not counted against it. `-n N` is
  different on purpose: it counts the waiting clips, so it downloads only what N full compilations still need.
- Today `new:N` takes the first N videos you don't have and only *then* drops look-alike re-uploads, so it can come out
  slightly short. `--clips N` should fill up to N *after* those are dropped, taking the next in line.
- If the channel has fewer new videos than N, you get what exists and the plan says so.

A selection has two forms: a **query** (channel + filters above) or an explicit **list of videos** (section 14).

As built in `ytt fetch` (step 4):

- A query needs exactly one of `--clips`, `-n`, `--range`, `--take`; there is no hidden default count. `--sort`
  defaults to `popular`; `--type` to `videos`. A list (`--videos FILE`, `-` for stdin) takes none of the query options.
- `--clips` and `-n` (and `--take new:N` / `comps:N`) are *fill* selections: a failed download is replaced by the next
  video in line, so you still get N when the channel has them. `--range` and the other `--take` forms are *exact*: only
  those positions are tried, anything already in the library is skipped, and a failure is reported, not replaced.
  A list is exact too.
- Likely re-uploads are found before counting (same title ignoring case/punctuation, and same length to the second;
  only the first 70 characters of a title count so clips imported from the old tool still match). A video with no
  known length is never called a look-alike. Always reported as a guess.
- Exact upload dates are looked up only where the date range needs them (about log2(n) lookups per edge, not n).
- Videos found on disk (in the clips folder or a `watch_folders` entry, by the 11-character id in the file name) are
  added to the library as `found` clips instead of being downloaded again. Files in other folders are used where they
  are and are never moved or deleted.
- `--refetch` ignores what the library has, downloads again, and replaces the existing file in the clips folder.
- Quality cap: `--max-height`, else the default style's `max_height`. Parallel downloads: `--workers`, else the
  workspace setting. Cookies come from the workspace settings (`cookies_from_browser`, `cookies_file`), not flags.

## 9. Style

A style is a named, reusable description of how a compilation is *rendered*, limited to what the engine can do today:

- transition: kind (or random, or a stinger folder), length, mode `hold` (default) or `overlap`
- stinger options: key colour, tolerance, sound on/off
- intro / outro
- quality (height cap, preset) and fit (stretch / pad / crop)

Not in a style: what goes in (source, selection, clips per compilation, order); that is the `make` request.
Styles are **flat**: no inheritance in v1. Styles live in the database; the user never edits a file. `style edit` is
the guided editor, `style set NAME key value` is the scriptable form.

Blurred backgrounds, audio normalising etc. are future style fields, listed only so they have a home.

**Workspace defaults are not a style.** `workspace.toml` holds machine and workspace facts (folders, workers,
cookies, delete-used-clips), a pointer `default_style = "default"`, and optional defaults for make requests
(e.g. clips per compilation). Today's remembered setup answers are split accordingly. It is all visible with
`ytt workspace show` and `ytt style show`, and changed only on purpose.

**`style stingers`, as built (step 7).** `ytt/engine/stingers.py` is `make_transitions.py` as a library (Pillow draws,
ffmpeg encodes; Pillow is needed only to draw, so it is not a hard dependency: `pip install pillow`), and
`ytt/ops/compile/stingers.py` is its request, plan, run. It needs no workspace; the output folder defaults to `./stingers`.
Files are encoded to a `.part` name and renamed, so Ctrl-C or a failed encode leaves no broken file; replacing files that
are already there is a warning that needs a yes (`--yes`; without a terminal it refuses). `--font` that cannot be loaded is
now an error (it used to fall back silently), `--ss` is `--smoothing`, and the command prints the `ytt style set ... stinger-dir
... transition stinger` and `ytt stitch --stinger-dir ...` lines that use the folder. Every stinger is tested to be clear at
both ends and to cover the whole screen at the midpoint, where the cut is hidden.

## 10. Compilations and remake

A compilation is a **recorded, concrete object**. Its row stores everything needed to reproduce it, not references
to things that can change:

- the ordered list of clips (video ids), and the video metadata that mattered (title, length) at that time
- a **snapshot** of the resolved style and the rendering options used
- the make request that produced it, and its parent compilation if it came from `remake`
- the output file, creation time, tool version

**`remake ID` operates on that concrete compilation. It never re-runs the original selection query.** Same clips,
same order, unless you change them. Changes are expressed as consequences:

| Change | Choices |
|---|---|
| Style | any saved style |
| Order | as recorded / reversed (today's `--reverse-each`) |
| Output | make a new compilation / replace the old output (today's `--replace`) |

If clip files were deleted they are fetched again under their original names. A compilation whose clips cannot
all be recovered is skipped and the others go ahead (today's behaviour).

**`make --like ID`** is a different thing, with a narrow meaning: *run the same make request again* (same source,
filters, sort, count, style), resolved against the library as it is now, so clips already used are not taken again.
It is today's `--again`. It does not mean "something similar" in any looser sense.

**`make`, as built (step 5).** `ytt make [CHANNEL]` is a fetch and a compile in one run (one row in `runs`).

- *How many.* `-n N` makes at most N compilations; with a channel and no `--clips/--range/--take` it also fetches what
  is missing for them. With a channel and `--clips N` (or `--range`, `--take`, `--videos`) everything that fills a
  compilation is made, the clips already waiting included. With no channel, `-n N` or `--all` (as many as the library
  allows) is required; otherwise it asks "How many compilations?" and stops.
- *Size and order.* `--per N` or `--per-minutes M` (the workspace's size otherwise); `--order name|oldest|newest|random`
  (`name` = the order they were fetched in; random draws a seed that is recorded); `--reverse` plays the whole pool
  backwards and `--reverse-each` each compilation backwards. Both apply after ordering and before cutting, and have
  nothing to do with which clips were fetched. Resolved the old open question about `--reverse` with `--take`.
- *Leftover.* Clips that cannot fill a compilation: `--if-short keep` (default; they wait for the next make), `short`
  (a last, shorter compilation, never from one clip) or `fetch` (fetch just enough more to fill one; needs a channel).
  Reaching `-n N` leaves spare clips, not leftover.
- *Used clips.* `--delete-used-clips` / `--keep-used-clips` (else the workspace setting) deletes only clips ytt
  downloaded, only after the run, never when it was cancelled; the clip stays in the library as `missing`, so `remake`
  can fetch it again. Clips found on the disk or imported are never deleted.
- *Unreadable clips.* A clip ffprobe cannot read is marked `failed` and left out; `--retry-failed` (the old
  `--retry-bad`) tries it again and clears the mark if it works.
- *The plan is a projection when downloads are involved.* The compilations are cut again from the library after the
  downloads, so they can differ if a download fails and the next video takes its place. The plan says so. Without a
  terminal and without `--yes` it refuses; `--dry-run` lists every clip of every compilation.
- *Status.* `completed` when everything promised was done (fewer videos than asked for, said in the plan, is fine);
  `partial` when a download or a render fell short; `failed` when nothing was done; `cancelled` on Ctrl-C (exit 130),
  keeping every finished download and compilation.
- *`--like ID`* takes the recorded request (ID: number, name or `last`; for a remade compilation it follows the
  "remade from" links back to the make). Flags typed with it override; a typed `--clips/--range/--take/--videos`
  replaces the recorded selection; the random seed is drawn again. Imported compilations have no request and are refused.
- *`style`*: `list`, `show`, `set NAME KEY VALUE [KEY VALUE ...]` (`--new` or `--from NAME` creates; a typo in a name
  never creates a style), `edit NAME` (prompts, needs a terminal, creates a new style), `delete NAME` (the workspace's
  default cannot be deleted). Compilations keep their own snapshot, so a style change never touches finished work.
- *`library forget TARGETS`* is the old `--forget`: the compilations' records are removed so their clips can be used
  again (unless another compilation uses them or the file is gone). The video file is never touched.

## 11. Library

```
 Library   1,842 clips · 1,119 used · 723 unused · 87 compilations
 Storage   clips 18.4 GB · compilations 7.2 GB
```

Two levels, kept apart: a **video** is known (id, title, views, date, channel); a **clip** is a local file with a
status of `ready`, `missing` or `failed`. "Used" and "unused" are derived from which compilations contain a clip.
"Unused" replaces "leftover". Download progress (queued, downloading) is part of a *run*, not a stored state.

A clip also records how it arrived: `fetched` (ytt downloaded it), `found` (it was already on the disk) or `imported`
(it came from the old tool without saying). Only `fetched` clips may ever be deleted by ytt (delete-used-clips in
step 5); the person's own files are never touched.

When the library has fewer clips than a full compilation needs, one setting decides:
`--if-short keep|short|fetch` (today's `--leftover keep|short|topup`); the guided menu asks.
Removing a clip that compilations reference warns first and says they could no longer be remade.

## 12. Workspace and state

```
~/Videos/ytt/
  ytt.db            one SQLite file: sources, videos, clips, compilations, compilation_clips, styles, runs
  workspace.toml    paths, workers, cookies, default style name, make defaults
  clips/  compilations/  cache/  logs/
```

- One source of truth. No archive file, no ledger file, no second settings file.
- Every object has an id (`compilation 42`, `clip 183`, `run 918`); commands refer to ids, not file names.
- `--workspace PATH` or `YTT_WORKSPACE` selects another workspace.
- Precedence, lowest to highest: **built-in defaults → workspace.toml → named style → flags.** The environment only
  selects the workspace. Running a command never rewrites defaults. Smart defaults are fine; hidden memory is not.

A setting is a **flag** if it describes this request, a **style** if it describes how things look, **workspace.toml**
if it is about this machine or workspace.

## 13. Failure, cancellation and recovery

Every action command creates a **run** that records what actually happened:

```
 Run 918   status: partial
   downloaded  11 / 12
   rendered     2 / 3     failed: compilation 44 (ffmpeg exited 1)
```

Run states: `planned`, `running`, `completed`, `partial`, `failed`, `cancelled`. Each download and render inside it
has its own outcome. Rules, so that re-running is always safe:

- Downloads go to temporary names and are renamed only when complete; a render goes to a temporary file and is renamed
  only when ffmpeg finishes. A crash leaves no half-written file that looks finished.
- A clip or compilation is recorded only after its file is complete. Nothing is recorded in advance.
- A failed download is replaced by the next video in line (today's behaviour) and reported, for fill selections (see
  section 8). A fetch run is `completed` when the number asked for arrived, even if some downloads failed on the way
  (those stay listed as failed); `partial` when fewer arrived; `failed` when none did.
- Clips are recorded in the order they were chosen, even when parallel downloads finish out of order, so the clip
  order of a later `make` is the order the person picked. A kill that cannot be caught (power loss) can leave finished
  files that were not recorded yet; the next fetch finds them by id and adds them.
- `fetch` is idempotent: a second run downloads only what is still missing.
- **Ctrl-C** stops cleanly: running ffmpeg is terminated, its temporary file removed, everything already completed is
  kept and recorded, the run ends as `cancelled`, and the summary says what finished. Exit code 130.
- Scripts get a non-zero exit code whenever a run is anything but `completed`.

## 14. Research and the handoff to compiling

Research keeps what `yt_toolkit` does, in the same table style, with the same `--json`/`--csv` and the same channel
resolution as the rest of the tool.

The handoff is **a plain list of YouTube video ids**, nothing more. There is no saved "result set" entity and no id
for research output.

- Any research command can export that list (`--ids` writes one id per line; `--json` includes them).
- `make` and `fetch` accept it as the explicit-list form of selection: `--videos FILE` (or `-` for stdin).
- In the guided menu, a research result ends with "Make a compilation from these? [y/N]", passing the same list in
  memory.

**As built (step 7).** `ytt/ops/research/` has one module per tool (`channels`, `outliers`, `table`, `live`, `tags`,
`niche`, `clip`) and may import only `ytt.sources` (enforced by `tests/test_architecture.py`). `niche` and `clip` are the
discovery step of `channels` followed by the scan of `outliers`; `clip` always looks up the real date of every outlier and
drops the old ones. Each takes a request and a backend and returns data and notes; nothing in them prints. The command
line is `ytt/ui/research_cli.py`. The backend gained `channel_tab`, `video_info`, `search_channels`, `featured_channels`,
`list_url` and `live_status`; a failed read is a `SourceError` that the tool turns into a note, except `video_info`,
which never raises. Results are checked against a fake YouTube; none of this has run against real YouTube yet.

## 15. Architecture and the dependency rule

```
 ui          guided menus, argparse, rendering       the only layer that prints or prompts
  │
 ops         research/  library/  compile/  ...      plain functions: Request → Plan → Result
  │
 ├─ workspace   database, config, styles
 ├─ sources     yt-dlp: listing, filtering, download
 └─ engine      ffmpeg: stitch, transitions            the existing tested code, wrapped
```

Enforced by a test that reads the import graph, not by good intentions:

- Nothing below `ui` calls `print`/`input` or imports the menu libraries.
- `ops/research` may import `sources` only (plus the cache path from `workspace`), never `library`, `compile`
  or database tables.
- `ops/compile` and `ops/library` may import `sources`, `workspace` and `engine`, never `research`.
- `engine` imports nothing from the rest of the package.

## 16. Boundary table

| Concept | User-facing | Persistent | Owned by | May depend on |
|---|---|---|---|---|
| Video (id, metadata) | yes (research) | yes (`videos`) | sources | nothing |
| Source | yes | yes | sources | video |
| Clip | yes | yes | library | video |
| Compilation | yes | yes | compile | clips, style snapshot |
| Style | yes | yes | compile | engine capabilities |
| Research view | yes | no (exported on request) | research | sources |
| Request | via flags/menu | no | ops | defaults, workspace config |
| Plan | yes, transiently | stored with its run | ops | request, workspace state |
| Run | yes | yes | ops | plan |
| Renderer (ffmpeg) | no | no | engine | nothing |
| Old ledger / archive | **no** | **no** | old system, import only | nothing |

## 17. Output, help and `doctor`

- Human output: clean tables and progress (`rich`). Machine output: `--json` on list/research/library commands only.
- Help is one level per command (`ytt --help`, `ytt make --help`); no single wall of flags.
- `ytt doctor` checks Python, ffmpeg, ffprobe, yt-dlp (and its age), workspace permissions, database, styles,
  network and YouTube reachability, and can verify migrated state.

**`doctor`, as built (step 7).** `ytt/ops/doctor.py` returns a list of checks (ok / warn / fail, what it saw, how to fix
it); it only looks and changes nothing, and everything outside the program (programs, network, disk, the date) comes in
through a `Probes` object so tests use a pretend machine. It checks: Python 3.11 or newer; ffmpeg and ffprobe, and that
ffmpeg has the `libx264` and `aac` encoders (Fedora's stock `ffmpeg-free` has no `libx264`; rendering needs it); yt-dlp
installed and under 60 days old (a warning, since an old yt-dlp is the usual reason downloads stop working); the menu
libraries; the workspace (writable, `workspace.toml` readable, clip and compilation folders, 5 GB free, cookies file,
watch folders); the database, opened read-only (version, integrity); styles (each loads, intro/outro files and the stinger
folder exist, the default exists); whether every ready clip and compilation still has its file (how a migrated workspace is
verified); and the internet and YouTube. With no workspace it says so as a warning and carries on. Exit code 1 when
something failed, 0 when there are only warnings; `--offline` skips the network checks.

## 18. Deliberately not part of this

No plugin system, workflow language, daemon, GUI, multi-user mode, non-YouTube sources, aliases, extension
mechanism, style inheritance, saved research result sets, or per-plan save/load. Deferred until a real need appears:
switching between saved workspaces, a global config file, engine versioning, `--json` everywhere.
The core is written so another source *could* fit one day; the interface is not made generic for it.

## 19. Migration

A **one-time import**, not runtime compatibility. It reads the old ledger, downloaded-videos archive, compilation
settings and existing clip folders (reusing the existing on-disk detection for this purpose only), writes the new
state, and prints a report:

```
 Imported   sources 12 · videos 843 · clips 791 · compilations 57 · styles 1
 Warnings   3 · Unresolved records 2
```

After that the old files are ignored. The existing 85 tests are the safety net and are ported as the new layers
appear; none is dropped without an equivalent. The old scripts stay until the new commands reach parity, then are
removed in one commit.

## 20. Build order (bottom-up, small commits, pushed)

1. Freeze: current state tagged, tests green. **Done.**
2. Package skeleton, the import-graph test, workspace (database, config), the old-state import and its report. **Done**
   (`ytt workspace init|show|set|import`).
3. Engine wrapper (`ytt/engine/stitch.py`), then `remake` and `library` working headless end to end. **Done**
   (`ytt remake`, `ytt library`; runs table; plan objects with errors/warnings/notes).
4. Sources and `fetch`, with runs and the failure rules (download layer behind an interface, faked in tests). Also lets
   `remake` fetch missing clips again. **Done** (`ytt fetch`; `ytt/sources`; migration 3 adds clip origin; `remake`
   fetches gone clips again under their recorded names, skips a compilation whose clips cannot be fetched, and
   deletes the re-fetched clips again when delete-used-clips is on). Real YouTube downloads are untested.
5. `make` (the full pipeline with validation and plans), `make --like`, and `style` commands. **Done**
   (`ytt make`, `ytt style list|show|set|edit|delete`, `ytt library forget`; see "`make`, as built" in section 10).
   Real YouTube downloads and real-size libraries are untested.
6. The guided menu on top of the same operations (`rich` + `questionary`). **Done** (`ytt` at a terminal; see "The
   guided menu, as built" in section 6). Not yet used on a real machine with real downloads.
7. `research`, `stitch`, `doctor`, `style stingers`; consistent output. **Done** (`ytt research` with seven tools,
   `ytt stitch`, `ytt doctor`, `ytt style stingers`, and the menu entries and the research-to-make handoff; see
   sections 6, 7, 9, 14 and 17, "as built"). Consistent output means the same table style, `--json`/`--ids`/`--csv`,
   notes on stderr, plain messages and exit codes in every research tool. Nothing has run against real YouTube.
8. Docs; delete the old scripts; port or replace their tests.

Each step ends in something that runs. The guided UI is last because it is the one layer that can be rebuilt freely
once the operations are right. The old scripts are frozen while this happens: bug fixes only if the new code can
not yet replace them.

## 21. Known limits

- Downloads can only be verified against real YouTube on the user's machine; the development sandbox cannot reach it.
  The download layer stays small and is covered by fakes.
- `ytt/sources/ytdlp.py` is covered by a stand-in for yt-dlp and by one check against the real yt-dlp's format
  selection; what real YouTube returns (list fields, rough dates, 403s, rate limits) is unverified until run on the
  user's machine.
- Videos found in a `watch_folders` entry are now library clips and can be used in compilations (the old tool counted
  them as downloaded but never compiled them). They are never deleted. Kept as is in step 5.
- Some old behaviours were found by reading code and tests, not documentation. `--reverse`/`--reverse-each` are settled
  for `make` (section 10, "as built") and covered by `tests/test_groups.py`; the rest are ported with their tests.
- The menu is tested with scripted answers (exact command lines, validation, Ctrl-C, whole flows against the fake
  YouTube), with real key presses fed to the real prompts through a pipe, and by hand in a pseudo-terminal; not yet
  on the user's own terminal. `style edit` still asks its questions as plain typed lines (step 5), not with arrow keys.
  The research, stitch, doctor and stinger flows of step 7 are tested the same way.
- Research, `doctor`'s network checks and everything that reads YouTube are checked against a fake YouTube only; what
  real YouTube returns (flat-listing fields, rough dates, subscriber counts, live flags, blocks and rate limits) is
  unverified until run on the user's machine. `doctor` and `stitch` were also tried only in the sandbox, on tiny
  synthetic clips. Stingers were rendered at small sizes only; 1080x1920 at the default smoothing is slow (every frame is
  drawn at twice the size).
- The research tools that look at each video (`table --full`, `tags`, `live`, `--resolve-dates`) make one request per
  video and are as slow as that sounds on a big list.
- `make` was tested with compilations of 2 clips of about a second. Large libraries (the first plan probes every unused
  clip once, then `cache/probes.json` remembers) and real-length renders are untested.

## 22. Open questions

- Final name (`ytt` for now).
