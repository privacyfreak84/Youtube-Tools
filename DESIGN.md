# ytt — design

Status: **draft 2. Steps 1-3 of section 20 are built; the rest is not.**
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

## 7. Commands

```
ytt                       guided front door
ytt make [@chan]          fetch what is missing, then compile
ytt fetch [@chan]         only get clips into the library
ytt remake ID             render a recorded compilation again, with changes
ytt library [clips|compilations|sources|stats]
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

## 11. Library

```
 Library   1,842 clips · 1,119 used · 723 unused · 87 compilations
 Storage   clips 18.4 GB · compilations 7.2 GB
```

Two levels, kept apart: a **video** is known (id, title, views, date, channel); a **clip** is a local file with a
status of `ready`, `missing` or `failed`. "Used" and "unused" are derived from which compilations contain a clip.
"Unused" replaces "leftover". Download progress (queued, downloading) is part of a *run*, not a stored state.

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
- A failed download is replaced by the next video in line (today's behaviour) and reported.
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
   `remake` fetch missing clips again.
5. `make` (the full pipeline with validation and plans), `make --like`, and `style` commands.
6. The guided menu on top of the same operations (`rich` + `questionary`).
7. `research`, `stitch`, `doctor`, `style stingers`; consistent output.
8. Docs; delete the old scripts; port or replace their tests.

Each step ends in something that runs. The guided UI is last because it is the one layer that can be rebuilt freely
once the operations are right. The old scripts are frozen while this happens: bug fixes only if the new code can
not yet replace them.

## 21. Known limits

- Downloads can only be verified against real YouTube on the user's machine; the development sandbox cannot reach it.
  The download layer stays small and is covered by fakes.
- Some old behaviours were found by reading code and tests, not documentation (for example exactly how `--reverse`
  and `--reverse-each` combine with `--take`). Each is ported together with its existing test.

## 22. Open questions

- Confirm the `--clips` change in section 8 (60 *new* clips instead of "first 60 positions").
- Final name (`ytt` for now).
