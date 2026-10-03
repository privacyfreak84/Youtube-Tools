# ytt — design

Status: **draft for review. No code is written against this yet.**
Replaces the current set of scripts (`auto_compile.py`, `make_compilations.py`, `stitch_videos.py`,
`make_transitions.py`, `yt_toolkit.py`) with one tool. The tested rendering engine is kept and wrapped.

## 1. Why

The scripts grew one at a time, so the user has to know how they happen to be built:

- `auto_compile` runs `make_compilations` as a subprocess, which runs `stitch_videos` as another. Flags such as
  `--redo`, `--setup`, `--dry-run`, `--reverse` exist at more than one layer.
- `auto_compile` alone has 30 flags. Several are synonyms (`--top` = `--pick new:N`, `--include-leftover` =
  `--leftover short`, `-n` = `--pick comps:N`).
- State is spread over five JSON files next to the scripts. Two of them are settings files and a hack keeps
  "download folder" and "clips folder" in agreement.
- One-line mode, `--again` and interactive mode handle saved state differently. The stale-date bug came from that.
- The interface speaks in internal words: pick, comps, adopt, ledger, leftover.

## 2. Three rules (everything else follows from these)

1. **Organise around what the user is trying to do**, not around how the old scripts implemented it.
2. **One canonical implementation per operation.** Guided menu, command line and scripts are three ways of filling in
   the same request. They are never three implementations.
3. **Internal concepts do not leak into the interface** just because they exist in the code.

Master test for any new feature: *where does it belong in the user's mental model?* If it has no home, the model is
wrong; do not bolt a flag onto the nearest command.

## 3. What ytt is

One tool for working with YouTube content from a local workspace. It has **two peer areas** on a shared
foundation. Neither is subordinate to the other.

```
   RESEARCH  (find & analyse)                 COLLECT & COMPILE  (get & make)
   channels · outliers · table                fetch · make · remake · library · style
   live · tags · niche · clip
            \                                         /
             \_______  shared foundation  ___________/
              sources · yt-dlp access · filters · output formatting
```

Research is useful on its own: it works on any channel, needs no library and produces no compilations.
The two areas touch at one optional point: a research result can be handed to `fetch` or `make`
("make a compilation from these"). Research never requires it, and compiling never requires research.

## 4. What the user thinks ytt manages

| Thing | Meaning |
|---|---|
| **Workspace** | The folder where everything lives. |
| **Source** | A YouTube channel (a handle or link). |
| **Clip** | A video that has entered the local library. |
| **Compilation** | A finished video made from clips, with a recorded recipe. |
| **Style** | A named description of how a compilation looks. |

Research results are **not** entities. They are views: printed as a table, or exported (`--json`, `--csv`).

Not part of the user's vocabulary: ledger, archive, adopt, pick, comps, leftover, redo mode, setup mode.

## 5. The one pipeline

Every command that *changes something* (`fetch`, `make`, `remake`, `library remove`, `style edit`) goes through:

```
REQUEST → RESOLVE → PLAN → CONFIRM → EXECUTE → RECORD
```

- **Request**: what was asked, as one structured object, whichever interface produced it.
- **Resolve**: look things up (channel list, what is already in the library) and fill in defaults.
- **Plan**: an explicit, printable description of what will happen. `--dry-run` stops here, with no special code.
- **Confirm**: shown unless `--yes`.
- **Execute**: download, render, delete.
- **Record**: write the result to the workspace database.

Read-only commands (`research ...`, `library` views, `doctor`) skip plan and confirm: request → run → render.

Example plan screen:

```
 Plan
   Source        @NestleCrunch · shorts · 2025-01-01 → 2025-12-31
   Selection     most viewed · first 45
   Library       29 already here · 16 to download
   Compilations  3 × 15 clips · style "default"
   Output        ~/Videos/ytt/compilations/
 Proceed? [Y/n]
```

## 6. Three ways in, one engine

| Level | How | Behaviour |
|---|---|---|
| Guided | `ytt` | Keyboard-driven menus (arrow keys, space, enter). Builds a request, then the normal plan screen. |
| Explicit | `ytt make @chan --clips 45 -n 3` | Same request from flags. Asks only for confirmation. |
| Script | `ytt make ... --yes` (and `--json` on list/research commands) | Never prompts. |

Interactive mode is a *request builder* and nothing more. If a question exists in the menu, the same setting
exists as a flag, and the other way round.

## 7. Commands

```
ytt                       guided front door
ytt make [@chan]          fetch what is missing, then compile
ytt fetch [@chan]         only get clips into the library
ytt remake ID             new compilation from an existing one's recipe
ytt library [clips|compilations|sources|stats]
ytt style [list|show|edit|save|delete]
ytt research <channels|outliers|table|live|tags|niche|clip> ...
ytt tools <stitch|transitions> ...      expert utilities, standalone
ytt workspace [show|init]
ytt config
ytt doctor
```

Every top-level command is a distinct user goal. A new command needs a goal that none of these covers.

- `make` with no channel uses clips already in the library.
- `make --like ID` repeats the *request* that produced compilation ID on fresh clips (today's `--again`).
- `fetch` is `make` without the compile step (today's `--no-compile`).
- `research` keeps today's `yt_toolkit` tools as its subcommands, unchanged in what they do.
- `tools stitch` is the standalone joiner for any list of video files.

## 8. Selection, in one place

Selection answers "which videos?" It stays one coherent group and the existing, tested grammar is ported as is:

```
--type shorts|videos     --from DATE  --to DATE
--min-views N            --min-length M  --max-length M
--sort popular|unpopular|oldest|latest|longest|shortest|title|random
--take 60 | 25-70 | last:20 | every:5 | random:30 | 1-10,25 | new:60 | comps:5
-n N                     shorthand for --take comps:N (N full compilations)
```

Positions count within the filtered, sorted list, so "25-70" means the same videos every time. Videos already in
the library are skipped without any "adopt" or "archive" step: the database knows every video id. Likely
re-uploads (same title and length) are flagged and skipped unless `--keep-duplicates`.

## 9. Style

A style is a named, reusable description of how a compilation is *rendered*. Only things the engine can do today:

- transition: kind (or random / a stinger folder), length, mode `hold` (default) or `overlap`
- stinger options: key colour, tolerance, sound on/off
- intro / outro
- quality (height cap, preset) and fit (stretch / pad / crop)

Not in a style: what goes in (source, selection, clip count, order). That is the *make* request.
Ideas such as blurred backgrounds or audio normalising are future style fields; they are listed here only so they
have a home when they arrive.

Today's "compilation setup" answers, which are silently remembered, become the workspace's **default style**.
Same convenience, but visible (`ytt style show`) and only changed by an explicit `ytt style edit`.

## 10. Compilations are recorded recipes

Each compilation row stores:

- the ordered list of clips (by clip id)
- a **snapshot** of the resolved style used (a copy, not a reference, so editing a style later never rewrites history)
- the make request that produced it, and its parent compilation if it came from `remake`
- output file, creation time, tool version

`ytt remake ID` takes a recorded compilation and applies changes. It needs no rediscovery. Changes are expressed as
consequences, not mechanisms:

| Change | Choices |
|---|---|
| Style | any saved style |
| Order | as before / reversed (today's `--reverse-each`) |
| Output | make a new compilation / replace the old output (today's `--replace`) |

If the clip files were deleted, they are downloaded again under their original names (today's behaviour).
A compilation whose clips cannot all be recovered is skipped and the rest go ahead.

## 11. Library

The library answers "what do I have?":

```
 Library   1,842 clips · 1,119 used · 723 unused · 87 compilations
 Storage   clips 18.4 GB · compilations 7.2 GB
```

Clips have a status: ready, used, unused, missing. "Unused" replaces "leftover".
When the library has fewer clips than a full compilation needs, one setting decides:
`--if-short keep|short|fetch` (today's `--leftover keep|short|topup`); the guided menu asks.
Removing a clip that compilations reference warns first and says they could no longer be remade.

## 12. Workspace and state

```
~/Videos/ytt/
  ytt.db            one SQLite file: sources, videos, clips, compilations, compilation_clips, styles, runs
  workspace.toml    paths, default style, workers, cookies, delete-used-clips
  clips/  compilations/  cache/  logs/
```

- One source of truth. No archive file, ledger file, or second settings file.
- Every object has an id (`compilation 42`, `clip 183`). Commands refer to ids, not file names.
- `--workspace PATH` or `YTT_WORKSPACE` selects another workspace. Nothing more is needed for now.

## 13. Configuration, and what is a flag

Precedence, lowest to highest: **built-in defaults → workspace.toml (incl. default style) → named style (`--style`)
→ command flags.** The environment only selects the workspace.

- Running a command **never rewrites defaults**. Nothing is remembered implicitly.
- Smart defaults are fine (all dates, most viewed, 15 clips per compilation). Hidden memory is not.
- A setting belongs in: a **flag** if it describes this one request; a **style** if it describes how things look;
  **workspace.toml** if it is about this machine or workspace (folders, workers, cookies).

Where today's 30 flags go:

| Today | Tomorrow |
|---|---|
| `--type --from --to --min-views --min/max-minutes --sort --pick` | selection flags (section 8) |
| `--top`, `-n/--compilations` | `--take new:N`, `-n` |
| `--max-height`, setup answers (transitions, intro/outro, quality) | style |
| `--reverse`, `--reverse-each` | clip order choice on `make` / `remake` |
| `--redo`, `--replace`, `--setup` | `remake`, `remake --replace`, `style edit` |
| `--again` | `make --like ID` |
| `--leftover`, `--include-leftover` | `--if-short` |
| `--no-compile` | `fetch` |
| `--dest`, `--check-folder` | workspace paths and watched folders (config) |
| `--workers`, `--cookies`, `--cookies-from-browser`, `--delete-after` | config, with a flag override |
| `--redownload`, `--keep-duplicates` | `fetch --refetch`, `--keep-duplicates` |
| `--dry-run`, `--yes` | global, every action command |

Target: `make` has roughly a dozen flags.

## 14. Research

Research keeps what `yt_toolkit` does and gets a consistent skin (same table style, same `--json`/`--csv`,
same channel resolution as the rest of the tool). The single bridge is optional and explicit, for example
`ytt research outliers @chan --make` ending in "Make a compilation from these? [y/N]". It is a convenience, not the
reason research exists.

## 15. Expert utilities

`ytt tools stitch` and `ytt tools transitions` stay available standalone. They are not part of the normal flow;
`make` uses the same engine through a function call, not a subprocess.

## 16. Output and help

- Human output: clean tables and progress (`rich`). Machine output: `--json` on list/research/library commands
  (not on every command: only where scripts benefit).
- `ytt --help`, `ytt make --help` etc. explain one level each. No 1,400-line wall of flags.
- `ytt doctor` checks Python, ffmpeg, ffprobe, yt-dlp (and its age), workspace permissions, database,
  style config, network and YouTube reachability.

## 17. Architecture and the dependency rule

```
 ui (guided menus, argparse, rendering)         ← the only layer that prints or prompts
   │
 operations (fetch, make, remake, research, ...) ← plain functions: Request → Plan → Result
   │
 ├─ workspace (database, config, styles)
 ├─ sources   (yt-dlp: listing, filtering, download)
 └─ engine    (ffmpeg: stitch, transitions)         ← the existing tested code, wrapped
```

Rule: nothing below `ui` calls `print`, `input` or imports the menu libraries. A small test enforces it.
Operations return plan and result objects; the ui renders them. This is what lets the menu, flags and
scripts share one implementation.

## 18. Deliberately not part of this

No plugin system, no workflow language, no daemon, no GUI, no multi-user, no non-YouTube sources, no aliases or
extension mechanism. Also deferred until a real need appears: switching between saved workspaces
(`--workspace` is enough), a global config file, engine versioning, `--json` everywhere.

## 19. Migration

- A one-time import brings in the old ledger, downloaded-videos archive, compilation settings and existing clip
  folders. The existing detection of clips on disk is reused for this import only.
- The 85 existing tests are the safety net. They are ported as the new layers appear; none are dropped without
  an equivalent.
- Old scripts stay until the new commands reach parity, then are removed in one commit.

## 20. Build order (bottom-up, small commits, pushed)

1. Freeze: tag the current state; tests green. *(done: 85 passing)*
2. Workspace: package skeleton, database, config, import of old state.
3. Engine wrapper + `build`, `remake`, `library` working headless end to end.
4. Sources + `fetch` (download layer behind an interface; faked in tests, as today).
5. `make` (the full pipeline) and `style`.
6. Guided menu on top of the same operations.
7. `research`, `tools`, `doctor`; consistent output.
8. Docs; delete the old scripts.

Each step ends in something that runs. The guided UI comes last because it is the one layer that can be
rebuilt freely once the operations underneath are right.

## 21. Known limits

- Downloads can only be verified against real YouTube on the user's machine; the sandbox used for development
  cannot reach it. The download layer stays small and is covered by fakes.
- Some old behaviours were found by reading code and tests, not documented (for example exactly how
  `--reverse` and `--reverse-each` combine with `--take`). Each is ported together with its existing test.

## 22. Open questions

- Style editing: prompts, or open the TOML in `$EDITOR`? (Proposal: prompts in the menu, `style edit` opens the file.)
- Should `make --like` remember which clips it already used within a series, or only rely on "unused"? (Proposal: rely on "unused".)
- Final name. (`ytt` for now.)
