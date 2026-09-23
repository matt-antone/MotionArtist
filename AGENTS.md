# Working in this repo

All agents must use caveman skill all the time.

This repo is one skill and three scripts. `motion-artist/SKILL.md` tells an agent how to turn a video
of a person moving into a motion source; `motion-artist/scripts/motion_artist.py` does the work,
`motion-artist/scripts/clipper.py` is the browser tool the user marks clip boundaries in before the
pipeline runs, and `motion-artist/scripts/portrait_crop.py` fixes landscape footage before it is
extracted. Everything else is docs and ignored scratch.

```
motion-artist/SKILL.md                 the skill an agent loads
motion-artist/scripts/motion_artist.py extract | render | pose-grid | export | selftest  (single file, ~1030 lines)
motion-artist/scripts/clipper.py       the clip marker: a local web tool that splits a video into
                                       frames and records which ones the user wants (see Marking clips)
motion-artist/scripts/portrait_crop.py crop a landscape source to a 9:16 window around the performer
                                       before extract, so the thumbs carry a full-size figure
motion-artist/templates/sheet.html     the motion sheet: markup, CSS and player, with {{PLACEHOLDER}}s
                                       render() fills. Edit the sheet's design here, not in the script
.claude/skills/motion-artist           symlink to motion-artist/, so the skill loads in this repo —
                                       git-ignored, so a fresh clone has to create it (see Setup)
work/                                  captures and downloaded video — git-ignored, never commit
exports/<set>/                         finished bundles, one zip per capture, and the set's
                                       clips.json — all git-ignored, never commit any of it
README.md                              the human-facing version of SKILL.md
```

## Setup

`python3` with `opencv-python` and `mediapipe<1`, plus `yt-dlp` on PATH. Pin mediapipe below 1.x:
the 1.x wheel crashes in the Metal helper on macOS. The pose model is cached at
`~/.cache/motion-artist/` on first run.

### Installing the skill

`.claude/` is git-ignored, so a fresh clone loads no skill and `/motion-artist` does nothing until
one of these runs. Both are symlinks — the skill stays a single source of truth and edits to
`motion-artist/` take effect with no reinstall.

To work on the skill in this repo:

```bash
mkdir -p .claude/skills && ln -s ../../motion-artist .claude/skills/motion-artist
```

To use it from any directory, link it into the user-level skills dir instead (absolute path — a
relative link breaks once it is outside this tree):

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/motion-artist" ~/.claude/skills/motion-artist
```

Either way the skill is named by `motion-artist/SKILL.md`, not by the link. Restart the session
after linking; skills are read at startup.

## Marking clips

Guessing `--start` and `--end` from a description costs re-cuts. When the user wants to point at
frames instead of describing them, run the clip marker and let them mark the boundaries:

```bash
python3 motion-artist/scripts/clipper.py
```

It opens `http://localhost:8765`. The user names an animation set, pastes a video URL, and the tool
downloads it into `work/<set>/` (reusing an existing download) and splits every source frame into
`work/<set>/frames/`. A name it has not seen is confirmed first, listing the sets that already
exist — a set name one letter off another is a second download and a second full frame split, and
nothing else in the flow would say so. Loading a set that already holds a **different** URL offers
to replace it, which deletes that video and its frames; the clips survive in `exports/`, but their
frame numbers were read off the video being replaced. They step through frames and mark in and out. Both marks sit on the frame track under the
viewer and can be dragged to adjust, with the frame following the mark as it moves, so a boundary
is settled by eye rather than re-marked. Loading a clip back into the player fills its name too, so
adding it again offers to replace it rather than writing a second clip — one name is one export
directory, and `clips.json` refuses two clips that would share it. They name each clip, pick how it
plays back and the fps the
clip is captured at, and the list is written to `exports/<set>/clips.json`:

```json
{"set": "shuffle-3", "url": "...", "source_fps": 29.97,
 "clips": [{"name": "side-step", "in_frame": 91, "out_frame": 150, "frames": 60,
            "start": 3.003, "end": 5.005, "playback": "loop", "pingpong": false,
            "capture_fps": 12, "capture_frames": 24, "speed_factor": 1.0,
            "export": "exports/shuffle-3/side-step"}]}
```

Run the pipeline per clip and take every one of those numbers as given: `--start`/`--end` from
`start`/`end`, `--playback` from `playback`, `--fps` from the clip's own `capture_fps`, `--frames`
from `capture_frames`. Do not re-search the span and do not pick a frame count. `capture_frames` is
already `capture_fps x window` for the window the marks fixed, which is the one order that makes
the capture play at the speed it was danced — see **Timing** in the skill for what picking a frame
count first costs.

`pingpong` is a separate flag because it is one: ping-pong is offered in the marker beside the
playbacks, but it is `extract --pingpong`, not a `--playback` value. A clip that chose it comes back
as `"playback": "loop", "pingpong": true`, so that clip's `extract` takes both `--playback loop` and
`--pingpong`. Never pass the word "ping-pong" to `--playback`; `extract` would reject it. `render`
then reads the flag out of the capture and does not need it repeated.

What the flag buys is worth carrying: it reaches `motion.json` and the manifest, and CAG reads it
from `motion.json` — folding `"playback": "loop"` with `"pingpong": true` into one word and writing
its proof as the bounce. A consumer that does not read it still jumps from the last frame to the
first, which is why `seam` and `seam_ratio` keep measuring that straight loop — a ping-pong clip
whose seam reads "needs blend" is still worth re-cutting.

`speed_factor` is that arithmetic checked: `1.0` means the marks land on a whole frame at this fps.
Anything else means they do not and the count was rounded, so the capture will play that much fast
or slow. The marker shows the same number live and its marks are draggable, so a clip that comes
back off 1.0 is worth handing back to be nudged rather than captured as it stands.

`frames` is how many **source** frames the clip spans, not the capture's count. The marker never
runs the pipeline and never writes a bundle.

## The pipeline

Five steps, in order. A capture is not finished until step 5.

1. `extract` — video → `work/<name>/motion.json` + `thumbs/`, and a printed frame table.
2. Author the arc — fill `arc`, and per-frame `note` only where the generated cue misses intent.
3. `render` — `motion.json` → the self-contained HTML motion sheet.
4. `pose-grid` — `thumbs/` → the pose grid: traced frames as pose cards, four across, twelve per
   image — this tool's own grid, not the consumer's batch size, which is 8 and which no longer
   reads the grid. Traced frames are the only pose reference; nothing in this repo draws a figure.
5. `export` — zip the motion sheet, the HTML, the traced frames, any pose grid images and a
   generated `manifest.json` with a SHA-256 per file.
   The printed bundle path and zip digest are what a KaraokeParty-Graphics job input references.

Every capture is named `<set>-<index>`. A video URL always arrives with a set name; one video is
one set, and each unique move cut from it takes the next index from 1. That string is the capture
directory, the bundle directory, the manifest `name` and the shipped `motion.json` `name`, so a
bundle cannot advertise one name and say another inside. Bundles land in `exports/<set>/`, one
directory per source video, as `<set>-<index>-<frames>f-<fps>fps-motion-source/` — an
uncompressed directory, not an archive.

An assigned name is an identifier in a way a descriptive label never was — "shuffle" is a genre,
and two different dances landed on it once and one silently overwrote the other. Provenance (url,
start second, span) rides in `manifest.json` rather than in the file name. Everything exported
under the older `<name>-<video id>-<start>s` scheme is deprecated and removed.
Hand off that zip as it is. Do not unpack it, and do not copy loose `motion.json`/`thumbs/` into a
consuming repo — the zip is the unit, and its `manifest.json` is what verifies it.
A re-cut of a move keeps its `<set>-<index>` and replaces the bundle in place, so no stale twin is
left to be referenced — but the SHA-256 changes, so re-reference it in any job input that names it.

**Delete `thumbs/` before re-cutting in place.** `extract` writes `f00..fNN` and overwrites, it does
not clean, so a re-cut to fewer frames leaves the tail of the longer one behind and `export` zips
the lot: one bundle here shipped 32 thumbs for a 20-frame capture. `export` warns when the count
disagrees with `frame_count` — treat that warning as a blocker, because a consumer that reads the
directory rather than the manifest silently uses the wrong frame count, and CAG drops the pose
reference entirely on a mismatch rather than degrading. `rm -rf <capture>/thumbs` first, every time.

**Landscape sources need `portrait_crop.py` before `extract`.** A thumb is the whole frame at 200px
wide, so 16:9 footage leaves the figure about 66px tall against roughly 250px from a portrait
source — measured at 66 and 262 on two clips through identical code. A higher-resolution download
does not help; the cap is applied after the fetch. Crop the video, not the thumbs, so the trace and
`pts` stay in one coordinate space. SKILL.md carries the full reasoning.

The manifest carries `view_frames`, the per-frame view counts, alongside the single majority
`view`. Screen on the counts: the majority value hides a set that is half three-quarter, or one
carrying rear frames inside a near-tie. A `back` frame is fatal downstream; a side frame usually is
not, and some moves turn in every window they have.

Read the printed table, not `motion.json` — the JSON is large and mostly landmarks. Never hand-edit
`cue`, `role`, `pts`, `depth` or `t`; they are extractor output. Re-run `extract` instead.

Since `motion-artist/2`, `pts` values are `[x, y, z]`: z comes from the MediaPipe world landmarks,
rescaled to the same units as x, negative toward the camera, hips at zero. Each frame also carries
`depth` = `{near_side, limbs}` (`near`/`far`/`level` per `legL`/`legR`/`armL`/`armR`), and the cue
says which leg is behind whenever the legs overlap in the image. A consumer that sorts bones by
mean z and draws the far ones first no longer has to guess.

## Vocabulary

**The term table lives in `motion-artist/SKILL.md`.** Read it before naming anything. It is there
rather than here because the skill ships standalone — `motion-artist/` is symlinked into a skills
directory without this file — and two copies of a glossary drift.

The rules that bind work in this repo:

- **Never write "sprite sheet", "spritesheet" or "skeleton"**, in code, filenames, manifest keys,
  comments, commit messages or conversation. All three are retired. "Sheet" unqualified and "grid"
  unqualified are banned too: between them they named eight different objects across this repo and
  the character generator's, which produced three wrong conclusions in one session and cost a
  feature that had been asked for twice.
- The pose images this repo makes are a **pose grid** of **pose cards** built from **traced frames**.
  The finished drawn character is the consumer's **frame sheet**, and we never produce one.
- `motion sheet` is the only allowed bare "sheet", and it means the contents of `motion.json`.
- **Agree a term before using it.** If something here has no name, name it in SKILL.md's table first,
  and tell the consumer's side, rather than reaching for "sheet" again.

## Verifying a change

- `python3 motion-artist/scripts/motion_artist.py selftest` — pose-description heuristics, timestamp
  parsing, manifest and digest. Fast, no video needed. Extend it when you add logic.
- `work/sample/` holds a real capture. Re-render or re-export it to check a change end to end
  without re-downloading anything.
- Changing geometry (scale, floor, stabilize, seam scoring)? Look at the rendered sheet. The
  heuristics are guidance for an artist, not measurements, and only the drawing shows a regression.

## Code conventions

Match what is there rather than introducing a second style.

- One file, standard library plus cv2 and mediapipe. Do not add a dependency for what a few lines do.
- Dense but plain: short helpers, compound lines where they read cleanly, no classes, no framework.
- A deliberate simplification with a known ceiling gets a `# ponytail:` comment naming the ceiling.
- Anatomy is `character-left` / `character-right`, never screen sides. The sheet's `view` is the
  *filmed* view and never governs the rendered character.
- A new flag needs three edits: the `argparse` line, the SKILL.md input table, and the README flag
  table. SKILL.md is the contract an agent reads — a flag missing from it does not exist.

## Git

- Branch off `main`; `main` is protected by habit, not by rule, so do not commit to it directly.
- Run `fallow` before committing.
- Commit subjects are imperative and describe the behaviour: "Pin the planted ankle to one floor
  line when stabilized", not "fix". Body explains why, wrapped at ~76 columns. Small, atomic commits.
- PRs target `main` and are merged with a merge commit.
- Never commit anything under `work/` or `exports/`, and no `.mp4`. Both trees are git-ignored;
  a bundle and the `clips.json` beside it are working-copy artefacts, never repo content.

## graft skill

This repo is indexed by graft. Use the graft skill for codebase context — `graft_find_code`, `graft_find_all`, `graft_trace_calls`, `graft_file_api`, `graft_repo_map` — before grepping or reading source files. Never commit graft caches.
