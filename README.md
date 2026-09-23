# MotionArtist

A Claude Code skill that turns a video of a person moving into a **motion sheet** for animation
agents: the traced video frames as the pose reference a generator is conditioned on, and a pose instruction
per frame at a declared fps and frame count, written in the vocabulary used by
[KaraokeParty-Graphics](https://github.com/matt-antone/KaraokeParty-Graphics) motion and
keyframe roles (character-left / character-right, keys, pilots, loop seam). The sheet is a
motion source: it describes motion only, never character scale, identity, view or prop hand.

Demo sheets:

- 16 frames at 4 fps from a dance short: https://claude.ai/code/artifact/cb0e7b85-66a2-4ce2-a1c9-c7dac23abd09
- best 8-frame, 2 fps loop found inside a 13 s range: https://claude.ai/code/artifact/44e63959-59d7-4931-8d3f-ff3abda6d2b3

```
motion-artist/
  SKILL.md                    # the skill (what Claude does, step by step)
  scripts/motion_artist.py    # extract (video → motion.json + thumbs), render (→ HTML), pose-grid (→ pose grid), export (→ bundle)
  scripts/clipper.py          # browser tool: step a video frame by frame and mark clip in/out points
  scripts/portrait_crop.py    # crop a landscape source to a 9:16 window around the performer, before extract
  templates/sheet.html        # the motion sheet's markup, CSS and player; render() fills its placeholders
exports/<set>/                # finished bundles, one zip per capture — committed
AGENTS.md                     # how to work in this repo: pipeline, conventions, verification
```

## Install

```bash
pip install "mediapipe<1" opencv-python   # plus yt-dlp and ffmpeg/ffprobe on PATH
ln -s "$PWD/motion-artist" ~/.claude/skills/motion-artist   # or copy into a repo's .claude/skills or .agents/skills
```

`mediapipe<1` is deliberate: the 1.x macOS wheel aborts in its Metal helper. The pose model is
downloaded once to `~/.cache/motion-artist/`.

## Marking clips

A source video holds several moves, and choosing where each one starts and ends by scrubbing a
YouTube player is guesswork — a frame either side is a visibly different pose. `clipper` is a
local web tool for settling that:

```bash
python3 motion-artist/scripts/clipper.py            # --port 8765 by default
```

Open `http://localhost:8765`, name an animation set, paste a video URL. It downloads the video
into `work/<set>/`, splits every source frame into `work/<set>/frames/`, and serves a
frame-by-frame viewer. Step to the frame you want, mark in and out, drag either mark along the
frame track to adjust it, name the clip, add it to the list.

The list is saved to `exports/<set>/clips.json`, where each clip carries the exact frame numbers
and the seconds they correspond to. That file is the handoff: `extract --start S --end S` takes
the seconds straight from it. The tool runs no part of the pipeline — it only settles which frames
the pipeline is pointed at.

## Landscape footage

A thumb is the whole frame resized to 200px wide, so a 16:9 source leaves the performer about a
third the height a portrait source gives. That is fixable only in the pixels, before tracing:

```bash
python3 motion-artist/scripts/portrait_crop.py work/<set>/video.mp4 --start 12 --end 20
```

It traces the performer across `--start..--end` to find where they sit in frame, then crops the
**whole** video to a 9:16 window around them (`--aspect`, `--margin` to tune). Cropping the whole
video is the point: every timestamp keeps its original meaning, so marks in `clips.json` and
`extract --start/--end` stay valid and the manifest still records the true second. Tracing then
runs on the cropped frames, so `pts`, the thumbs and the figure share one coordinate space and
nothing downstream has to be told about the crop. A different move from the same video may sit
elsewhere in frame and need its own crop.

## Use

In Claude Code:

```
/motion-artist https://www.youtube.com/watch?v=… from 1:14 to 1:27, find the best loop for an 8 frame 2 fps loop
```

Or by hand:

```bash
python3 motion-artist/scripts/motion_artist.py extract "https://www.youtube.com/shorts/…" \
  --fps 4 --frames 16 --start 0:16 --end 0:20 --name dance
python3 motion-artist/scripts/motion_artist.py render work/dance/motion.json
python3 motion-artist/scripts/motion_artist.py pose-grid work/dance/motion.json
python3 motion-artist/scripts/motion_artist.py export work/dance/motion.json
```

| Flag | Meaning |
| --- | --- |
| `--fps`, `--frames` | rate and total frame count of the target animation (required) |
| `--start`, `--end` | trim the span to inspect, seconds or `m:ss`. Without `--end`, the span is `frames / fps` seconds of real time. With both, the span is time-stretched onto the frame count. |
| `--margin` | slack in seconds around `--start`/`--end`: probe both ends for the move's own cut (loop: matching poses; one-shot: the stillest ending) and stretch the winner onto the frame count. Default `0.5`; `0` uses the span exactly as given |
| `--url` | origin URL, when `source` is a local copy of it — the manifest is the only place a bundle's provenance lives |
| `--pingpong` | the capture plays out and back (`0..N-1..1`); recorded in the manifest and used by `render`. `seam`/`seam_ratio` still measure the straight loop, since a consumer that ignores the flag plays that jump |
| `--performer` | `female` / `male`, the filmed body carried into the manifest; omit when it should not be stated. Not the character the render must draw |
| `--search` | find the best loop: slide a `frames / fps`-second window over `--start..--end` (whole video if `--end` is omitted), score each start by loop-closure pose distance against motion energy, pick the tightest seam among the livelier half |
| `--window` | source seconds the search looks for, when that differs from `frames / fps` (a scene cut leaves a short usable span, or a fast move should play slower); the winner is stretched onto the frame count |
| `--stabilize` | centre the hips horizontally in every frame; use for a moving camera or a travelling performer (airborne is then never called, since there is no fixed floor). Body scale is always normalised per frame from pixels-per-metre, so camera zoom never changes the traced pose's size |
| `--playback` | `loop` (default), `one-shot`, `final-hold`. Only `loop` carries meaning downstream; the others differ only in how the span's end is chosen here |
| `--exaggerate` | amplify each landmark's deviation from the clip-mean pose; default `1.25`, `1.0` = as filmed |
| `--name`, `--out` | output slug and directory (default `work/<name>/`) |
| `render --template FILE` | render into a different sheet template (default `motion-artist/templates/sheet.html`) |
| `render --pingpong` | walk the same cells out and back; the return leg reverses the out leg, so the seam is clean and the sheet still holds only the requested frames |
| `pose-grid --cols` | pose cards per row; default `4` |
| `pose-grid --no-labels` | drop the frame-number band, and say so in the sidecar |
| `export --out`, `--sheet` | bundle path (default `exports/<set>/<set>-<index>-<frames>f-<fps>fps-motion-source.zip`) and the sheet HTML to include (default `<name>-motion.html` beside the json) |

`extract` prints one line per frame (index, source time, key / pilot / in-between, pace, pose cue)
and writes `motion.json` plus `thumbs/`. Landmarks carry depth — `pts` is `[x, y, z]`, z negative
toward the camera with the hips at zero — and every frame adds a `depth` block naming the near
side and each limb's `near` / `far` / `level`, so a 2D consumer can sort bones instead of guessing. Between `extract` and `render`, fill `arc` and any
per-frame `note` in `motion.json`; the sheet renders them. `selftest` checks the pose heuristics
and the export manifest.

The rendered sheet has the traced frame and its cue, play / scrub / rate / mirror controls, the
frame strip (keys pink, pilots blue), the pose grid, the performance arc, a brief for the
artist agent, the frame-note table, and the data as embedded JSON.

`pose-grid` builds the pose grid you hand a generator in one call: the traced frames as pose cards,
four across and twelve per image (the tool's own grid, not the character generator's batch size),
so a longer motion chunks across several. Photographs rather than
drawings because they carry the movement at the size it was really danced — measured against the
trace, drawn cards come back at 0.43–0.70 of it and photographs at 0.9–1.2. It writes a
`<name>-pose-grid.json` sidecar declaring the geometry in PNG pixels so a consumer slices by stated
numbers rather than measuring the image, and needs one traced frame per motion frame.

The character generator does **not** read this: it builds its own pose grid from the loose traced
frames, against its own batch size. The grid here is for handing a generator the whole set directly.

`export` is the last step: it zips the motion sheet, the HTML, `thumbs/` and any pose grid images with a
generated
`manifest.json` (fps, frame count, playback, view and the per-frame `view_frames` spread that
average hides, seam and the `seam_ratio` behind the verdict, `stabilized`, `exaggerate`,
`performer`, source, the pose grid's geometry, and a SHA-256 per file), and
prints the bundle path and the zip's SHA-256 — the pair a KaraokeParty-Graphics motion-director
job input references. It warns if `arc` is empty or any frame has no pose. Two cuts of one
move get two names, never one overwritten file, so a copy into a consuming repo removes the bundle
it supersedes in the same step.

Captures and downloaded video live under `work/`, git-ignored. Finished bundles land in
`exports/<set>/` and are committed, beside the `clips.json` clipper writes for that set.
