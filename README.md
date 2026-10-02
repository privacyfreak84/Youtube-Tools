# YouTube CLI Utilities

A collection of command-line tools for YouTube: research channels and trends, download videos or shorts from a channel, and stitch them into compilations with transitions.

## What's in the repo

| Script | What it does | Needs |
|---|---|---|
| [`auto_compile.py`](auto_compile.py) | **Master script.** Picks videos from a channel (by date, length, views, sort order and a flexible `--pick` syntax), downloads them, then builds compilations. Remembers your answers and what you've already downloaded. | `yt_toolkit.py`, `make_compilations.py`, `stitch_videos.py` |
| [`make_compilations.py`](make_compilations.py) | Turns a folder of clips into finished compilation videos, hands-off. Keeps a ledger of which clips went into which video, so runs resume where they stopped and nothing is made twice. | `stitch_videos.py` |
| [`stitch_videos.py`](stitch_videos.py) | The video joiner. Stitches many videos into one with ffmpeg `xfade` transitions, per-junction overrides, reordering, and "stinger" transition videos (including any video of your own, with chroma-keying). | ffmpeg 4.3+ |
| [`make_transitions.py`](make_transitions.py) | Generates stinger transitions (transparent `.mov` plus a green-screen `.mp4`) from a colour palette. Output goes to a folder that `stitch_videos.py` can use. | Pillow, ffmpeg |
| [`yt_toolkit.py`](yt_toolkit.py) | YouTube research toolkit: channel discovery, outlier detection, channel tables, live-stream extraction, tag extraction, niche report and clip finder. `auto_compile.py` imports `ChannelTable` and `build_cookies_opts` from it. | yt-dlp, optionally tabulate |

### How they fit together

```
yt_toolkit.py ──(channel listing)──▶ auto_compile.py ──▶ make_compilations.py ──▶ stitch_videos.py ──▶ finished .mp4
                                         (download                (batching,            (ffmpeg filter graph,
                                          via yt-dlp)              ledger)               transitions)

make_transitions.py ──▶ stingers/ folder ──▶ used by stitch_videos.py via --stinger-dir
```

**Keep these files together in one folder.** `auto_compile.py` needs `yt_toolkit.py` and `make_compilations.py`; `make_compilations.py` needs `stitch_videos.py`. `make_transitions.py` is only needed if you want custom stingers, and `yt_toolkit.py` also runs standalone.

## Requirements

- Python 3.8+
- [ffmpeg and ffprobe](https://ffmpeg.org/) on your `PATH` (ffmpeg 4.3+ for the `xfade` filter)
- `pip install yt-dlp tabulate` (research and download tools)
- `pip install pillow` (only for `make_transitions.py`)

## Quick start

```bash
# Everything in one go: asks a few questions once, then downloads and compiles
python auto_compile.py

# Or non-interactively
python auto_compile.py @Channel --sort oldest --pick 60
python auto_compile.py @Channel --sort popular --pick last:20
python auto_compile.py @Channel --from 2024-01-01 --to 2024-06-30 --sort popular --pick new:60
```

Run `python auto_compile.py --help` or read the header of the script for the full list of `--sort` and `--pick` options.

### Mass-producing compilations

```bash
python auto_compile.py @Channel --sort oldest --compilations 5   # just enough new videos for 5 full compilations
python auto_compile.py --again --yes                             # next batch, same settings, no questions
python auto_compile.py --again --yes --compilations 10           # ...or a bigger one this time
```

- **Plan by compilations, not videos.** `--compilations N` (or `--pick comps:N`, or the first option in the questions) works out how many videos you need from the compilation size you set up, and counts clips already waiting in the download folder, so nothing is downloaded that you don't need.
- **Never downloads the same video twice.** Besides its own history it looks for video ids in the file names in your download folder (including yt-dlp's usual `Title [id].mp4`) and in any `--check-folder FOLDER` you give it (remembered). Those count as downloaded. Videos that look like re-uploads (same title and same length as one you have) are skipped too; `--keep-duplicates` turns that off. The look-alike check is a guess, so it always tells you how many it skipped.
- **Leftovers are handled for you.** If clips are left that can't fill a whole compilation, it asks: keep them for next time, make a shorter compilation now, or download just enough more to fill one. For unattended runs use `--leftover keep|short|topup` (default with `--yes` is keep). The "download more" option only appears when the list can actually supply them.
- **Faster downloads.** `--workers N` downloads N videos at once (default 3). Use `--workers 1` if YouTube starts refusing.
- **`--again`** repeats your last run with the next batch. Unlike a fresh run, it keeps the last run's date range.
- **A summary at the end** lists the compilations made, how many clips are waiting, and the command for the next batch.
- `--delete-after` now also removes older leftover clips once they have been used (only files this script downloaded).

In interactive mode your answers are remembered as the defaults for next time, **except the date range**, which always starts blank so an old range can't quietly shrink the list. Pass `--from` / `--to` to prefill it; at the prompt, Enter keeps what is shown and `-` means no limit.

### Compiling clips you already have

```bash
python make_compilations.py                 # first run: asks setup questions, then how many to make
python make_compilations.py 1               # make one compilation (a good first test)
python make_compilations.py all             # make as many as your clips allow
python make_compilations.py --list          # show what has been made
python make_compilations.py --dry-run       # show the plan without rendering
python make_compilations.py --forget 3      # put compilation 3's clips back in the pool
python make_compilations.py --include-leftover   # also make a shorter final video from the remainder
```

By default each compilation uses a set number of clips (15 unless you change it in setup), so a small folder produces nothing until you add clips or pass `--include-leftover`.

### Joining clips directly

```bash
python stitch_videos.py clips/ -o out.mp4                       # whole folder, 1s fade between clips
python stitch_videos.py a.mp4 b.mp4 c.mp4 -t wipeleft -d 0.8    # explicit files and transition
python stitch_videos.py clips/ --custom "2=circleopen:1.5, 4=cut"   # override specific junctions
python stitch_videos.py clips/ --stinger-dir stingers -t stinger    # random stinger per junction
python stitch_videos.py clips/ --dry-run                        # preview the plan and ffmpeg command
python stitch_videos.py --list-transitions
```

`stitch_videos.py` can also use any video as a transition with `@file[|option=value]` (chroma key, cut point, audio on/off). See the script's header or `--help`.

### Generating stinger transitions

```bash
python make_transitions.py                         # 5 transitions, 1080x1920, into ./stingers
python make_transitions.py --size 1920x1080 -o stingers_wide
python make_transitions.py --only burst --text "CRUNCH!"
```

### Research toolkit

```bash
python yt_toolkit.py                                  # interactive menu
python yt_toolkit.py channels --search "lofi hip hop"
python yt_toolkit.py outliers "@Channel1" "@Channel2" --type shorts
python yt_toolkit.py table "@SomeChannel" --sort popular
python yt_toolkit.py live "@LofiGirl"
python yt_toolkit.py tags --csv outliers.csv --top 30
python yt_toolkit.py niche --search "lofi hip hop" --top 20
python yt_toolkit.py clip --search "reddit stories" --max-age-days 3
python yt_toolkit.py <tool> --help                    # flags for one tool
```

## State files

The scripts write small JSON files next to themselves so runs can resume: `auto_settings.json`, `fetch_archive.json`, `compile_settings.json`, `compile_ledger.json` and `compile_cache.json`. Delete one to reset that piece of state. They are listed in `.gitignore`, as are the `fetched/`, `compilations/` and `stingers/` folders.

## Tests

`auto_compile.py` has an offline test suite: the channel and the downloads are faked, but ffmpeg really renders the compilations.

```bash
python -m unittest discover -s tests -v
```

Needs ffmpeg and ffprobe on your `PATH` and `pip install yt-dlp tabulate`. The tests run in a scratch copy of the scripts, so they never touch your settings or downloads.

## Status

See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for the running status log.

## License

See [`LICENSE`](LICENSE).
