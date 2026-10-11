# ytt

Research YouTube, download clips from a channel, and join them into compilations with transitions. One command, `ytt`, with a guided menu and plain commands that do the same things.

## Install

You need Python 3.11 or newer and [ffmpeg and ffprobe](https://ffmpeg.org/) (4.3 or newer) on your `PATH`.

```bash
git clone https://github.com/privacyfreak84/Youtube-Tools.git
cd Youtube-Tools
pipx install .              # adds the `ytt` command
pipx install ".[stingers]"  # the same, plus Pillow for `ytt style stingers`
ytt doctor                  # checks that everything ytt needs is there and works
```

No pipx? `pip install .` inside a virtual environment does the same. Without installing anything, `pip install yt-dlp rich questionary` and then `python -m ytt` from the checkout works too.

**Keep yt-dlp up to date.** YouTube changes often and an old yt-dlp is the usual reason downloads stop working. `ytt doctor` tells you when yours is old. To update it: `pipx runpip ytt install -U yt-dlp` (pipx) or `pip install -U yt-dlp`.

## Start here

```bash
ytt
```

With no arguments, `ytt` opens the guided menu: make compilations, get clips only, remake a finished compilation, look at the library, styles, settings, research, join videos, check that everything works. Every choice shows the exact command it is about to run, so the menu also teaches the commands.

## Where things live

Compilations and clips go in a **workspace**, a folder (default `~/Videos/ytt`; change it with `--workspace PATH` or `$YTT_WORKSPACE`) holding:

| | |
|---|---|
| `clips/` | the clips you downloaded or added |
| `compilations/` | the finished videos |
| `ytt.db` | the records: which clip went into which compilation, what was fetched, what each run did |
| `workspace.toml` | your settings (`ytt workspace show`, `ytt workspace set NAME VALUE`) |
| `cache/` | small leftovers, such as the last research result |

`ytt research` and `ytt stitch` need no workspace and never touch one.

## Commands

| Command | What it does |
|---|---|
| `ytt make [CHANNEL]` | Fetch clips from a channel (or use the ones in the library) and make compilations. `-n 3` makes three; `--like last` makes another batch just like the last one; `--dry-run` shows the plan and does nothing. |
| `ytt fetch CHANNEL` | Only download clips into the library. Pick them by date, views, length, sort order, `--clips N`, `--range 25-70` or `--take last:20`. Videos the library already has are never downloaded again. |
| `ytt remake TARGETS` | Render finished compilations again from the same clips: `last`, `all`, a number, a name or a list. Change the look with `--style NAME`, or play them backwards with `--order reversed`. |
| `ytt style` | Saved styles: transitions, intro, outro, quality. A compilation keeps its own copy of the style it was made with, so changing a style never changes old compilations. `ytt style stingers` draws a folder of transition videos. |
| `ytt library` | See what you have: `stats`, `clips`, `compilations`, `sources`, `runs`, and `forget` (take compilations out of the records so their clips can be used again). |
| `ytt workspace` | `init`, `show`, `set`, and `import` (see below). |
| `ytt stitch VIDEO ...` | Join any videos into one file, with transitions. Every clip plays in full: the transition gets its own time over the held last and first frame and never cuts into the footage. Handles per-junction overrides (`--custom "2=circleopen:1.5, 4=cut"`), your own transition videos with chroma-keying, and `--list-transitions`. |
| `ytt research TOOL` | Look things up on YouTube (below). |
| `ytt doctor` | Checks Python, ffmpeg, yt-dlp, the workspace and the connection, and says what to do about each problem. |

Examples:

```bash
ytt make @SomeChannel --type shorts --sort oldest -n 2 --dry-run
ytt make @SomeChannel --type shorts --sort oldest -n 2 --yes
ytt remake last --style punchy
ytt stitch clips/*.mp4 -t circleopen -d 0.6 -o joined.mp4
```

`make`, `fetch` and `remake` first show a plan and ask to go ahead; `--yes` skips the question and `--dry-run` skips the work.

## Research

Seven tools. They only read from YouTube. Each prints a table, or gives `--json`, `--ids` or `--csv FILE` (which saves the rows as well as printing the table).

| Tool | What it answers |
|---|---|
| `ytt research channels` | Which channels are there for this search, or on another channel's Channels tab? |
| `ytt research outliers CHANNEL...` | Which videos do far better than their own channel's usual? |
| `ytt research table CHANNEL` | A channel's videos as a table: views, date, length. |
| `ytt research live CHANNEL...` | Which streams are live right now? |
| `ytt research tags` | Which tags and title words come up most across videos? |
| `ytt research niche` | Find a niche's channels, then outliers across all of them. |
| `ytt research clip` | Fresh outliers from a niche: recent videos that beat their channel's usual, best first. |

Two things worth knowing. The "channel median" an outlier is measured against is the median of the videos that were looked at, so `--limit 30` (the newest 30) gives a different baseline than the whole channel. And the channel page only says things like "1 month ago": such dates are shown with a `~` and are rough; `--resolve-dates` looks up the exact day (`clip` always does, because freshness is its whole point).

In the guided menu, a research result that is a list of videos ends with "Make a compilation from these videos?".

## Coming from the old scripts

ytt replaces `auto_compile.py`, `make_compilations.py`, `stitch_videos.py`, `make_transitions.py` and `yt_toolkit.py`. They are no longer in the repo, but they are not lost: git keeps them at the tag `old-scripts` (`git show old-scripts:auto_compile.py`, or `git checkout old-scripts` to run them as they were).

To bring your old downloads, ledger and settings across, run this once with the folder the old scripts ran in:

```bash
ytt workspace import OLD_FOLDER --dry-run   # shows what would be imported, changes nothing
ytt workspace import OLD_FOLDER
```

Nothing in the old folder is changed, and it is safe to run more than once. The guided menu offers the same on first run.

What works differently from the old scripts:

- `--csv` always means "also save the rows". In `research tags`, the old CSV input is `--from-csv`, and `--videos FILE` reads a list of ids.
- `research live` prints a table; the old one-file-per-channel output is behind `--out-dir`.
- `stitch` no longer asks anything: `--custom` and `--order` replace the interactive `--pick-transitions` and `--reorder`, it never asks for confirmation, and it will not overwrite a file without `--overwrite`.
- `style stingers` is the old `make_transitions.py`; `--ss` is now `--smoothing`, and a `--font` that cannot be loaded is an error rather than a silent fallback.
- ytt does not carry your answers from one run to the next on its own. The old tool did (once even a date range, which silently cut a channel down to one video). Settings are explicit (`ytt workspace set`), and `make --like last` repeats a make on purpose.
- There is no "look beyond the date range?" question. If the range holds fewer new videos than you asked for, the plan says only that many were found because the dates narrowed the list; if none match, it stops with an error.

## How the code is laid out

`ytt/ui` talks to you (commands, menu); `ytt/ops` decides what to do (plans, then runs); `ytt/engine` runs ffmpeg; `ytt/sources` talks to YouTube through yt-dlp; `ytt/workspace` holds the records and settings. A test keeps them apart: engine, sources and workspace use none of the others, and only `ui` ever prints or asks. [`DESIGN.md`](DESIGN.md) has the reasoning and the history of the rework; [`PROJECT_STATUS.md`](PROJECT_STATUS.md) is the running log.

## Tests

```bash
python -m unittest discover -s tests
```

About 850 tests, a couple of minutes, fully offline: YouTube is faked, and ffmpeg really renders small test videos. Needs ffmpeg and ffprobe on your `PATH`, plus `pip install yt-dlp rich questionary`. The tests never touch your workspace. The state files the old scripts wrote are frozen in `tests/fixtures/legacy_state/` so the import keeps being tested against the real format.

## License

See [`LICENSE`](LICENSE).
