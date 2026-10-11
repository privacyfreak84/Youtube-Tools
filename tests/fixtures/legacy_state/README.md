# Old-tool state, captured from the real old scripts

These folders are what the old tool (`auto_compile.py`, `make_compilations.py`, `stitch_videos.py`) actually wrote
when it was run against a fake YouTube, frozen so that `ytt workspace import` keeps being tested against the
real format after the old scripts were deleted. The scripts themselves are still in git, at the tag `old-scripts`.

| folder | how the old tool was run |
|---|---|
| `two_compilations/` | `--pick comps:2`: 8 clips fetched, 2 compilations of 4 clips made, every file still on disk |
| `clips_deleted_after_use/` | `--pick new:6 --delete-after`: 6 clips fetched, 1 compilation made, its 4 clips deleted, 2 clips kept |

Each folder has the old tool's JSON files unchanged except that the machine's temporary folder in a path is written
`{ROOT}` (`tests/legacy_world.py` puts the scratch folder back), and `media.json`, the list of video files that were
in the folder. The videos are not stored; `tests/legacy_world.py` makes tiny stand-ins with the same names.

## Making them again (only if the old format is ever to be captured afresh)

```
git checkout old-scripts          # in a separate clone: the old scripts and tests/test_auto_compile.py
```
then run the old tool through `WorldTest` from `tests/test_auto_compile.py` (`run_ac(*base("--pick", "comps:2"))`),
copy the `*.json` files from its scratch folder (`WorldTest.tmp`), replace that folder's path by `{ROOT}`, and list the
`*.mp4` files it holds (relative paths) in `media.json`.
