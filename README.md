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
work/<creator>-<title>/       # one downloaded video, its split frames, meta.json and clips.json — git-ignored
exports/<genre>/<genre>-NN/   # finished bundles, numbered within their genre — git-ignored, never committed
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

Open `http://localhost:8765`, pick a video you have opened before — listed as `Creator - Title` —
or paste a YouTube URL for a new one, and name the genre it belongs to. It downloads the video into
`work/<creator>-<title>/`, splits every source frame into `work/<creator>-<title>/frames/`, and serves a
frame-by-frame viewer. Step to the frame you want, mark in and out, drag either mark along the
frame track to adjust it, add it to the list.

Work is keyed by the video, so a clip can never be read against the wrong source. The directory is
named for the video to be recognisable, but its YouTube id is what decides whether two URLs are the
same video: a name already held by a different id becomes `<creator>-<title>-2`, so two uploads
sharing a creator and a title do not become one directory holding two videos. Exports are keyed by
genre, and the two counters are separate. A clip is numbered *under its video* — `clip-01`,
`clip-02`, its place in that video's list, which names the directory it is captured into. A motion is
numbered *across its genre* — `hiphop-01`, `hiphop-02` — and that is the bundle's name.

Position cannot stand in for the motion number: two videos open on `hiphop` produce 01, 02 in one and
03 in the other. So nothing writes a motion number while you are marking — it does not exist yet.
`extract --genre hiphop` allocates it when the clip is first cut, past everything already exported
into `exports/<genre>/` and everything already claimed by another video, and writes it back into the
clip. A re-cut reads it back rather than taking a second number, so the re-export replaces the bundle.

The list is saved to `work/<creator>-<title>/clips.json`, where each clip carries the exact frame
numbers, the seconds they correspond to, and the directory it is captured into. That file is the
handoff: `extract --start S --end S` takes the seconds straight from it. The tool runs no part of the
pipeline — it only settles which frames the pipeline is pointed at.

## Landscape footage

A thumb is the whole frame resized to 200px wide, so a 16:9 source leaves the performer about a
third the height a portrait source gives. That is fixable only in the pixels, before tracing:

```bash
python3 motion-artist/scripts/portrait_crop.py work/britney-spears-toxic/source-P4QeqpsY8v8.mp4 --start 12 --end 20
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

To pick the boundaries by eye instead of by description, mark them first:

```bash
python3 motion-artist/scripts/clipper.py
```

That opens a browser at `localhost:8765`, splits the video into frames, and lets you drag an in and
an out mark per move and choose its playback and capture fps. Videos are listed by
`Creator - Title` and keyed by that same name, with the YouTube id beside it in `meta.json`, so
re-pasting a URL you already opened reopens those frames rather than downloading a second copy —
there is no set name to get one letter wrong.
It writes `work/<creator>-<title>/clips.json` — frame numbers, the seconds `--start`/`--end` want,
the `--playback` you chose, `--fps`/`--frames`, and the `clip-NN` directory each is captured into —
and runs nothing else. The motion number is not in there: `extract --genre` allocates it on the
first cut and writes it back.

The frame count is derived, never typed: `frames = fps x window`, where the window is the span your
marks already fixed. That is the order that makes the capture play at the speed it was danced, and
each clip carries the resulting `speed_factor` so a window that does not land on a whole frame says
so while you can still drag a mark to fix it.

Play runs the footage, every source frame between the marks at the rate it was filmed, because
judging whether a move is the right move means watching the motion. The capture is read rather than
watched: the line beside the marks gives the derived frame count, the seconds it will run, and
whether the window lands on a whole frame at that fps. Picking `ping-pong` walks those same
source frames out and back, so the return leg you watch is the motion reversed.

`ping-pong` sits in the playback list but is not a `--playback` value — it is `extract --pingpong`,
so a clip that picks it is written as `"playback": "loop", "pingpong": true` and that clip's
`extract` takes both. It adds no cells and reaches the manifest, but CAG does not read it yet, so
the straight seam is still what a consumer plays — which is why `seam_ratio` keeps measuring it.

Or by hand:

```bash
python3 motion-artist/scripts/motion_artist.py extract "https://www.youtube.com/shorts/…" \
  --fps 4 --frames 16 --start 0:16 --end 0:20 --genre hiphop --out work/britney-spears-toxic/clip-01
python3 motion-artist/scripts/motion_artist.py render work/britney-spears-toxic/clip-01/motion.json
python3 motion-artist/scripts/motion_artist.py pose-grid work/britney-spears-toxic/clip-01/motion.json
python3 motion-artist/scripts/motion_artist.py export work/britney-spears-toxic/clip-01/motion.json
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
| `--name`, `--genre`, `--out` | the capture's name and directory. `--genre hiphop` allocates the name as `<genre>-NN` against `exports/<genre>/`, records it in the video's `clips.json`, and needs `--out` (the clip's `capture` directory). `--name` names it by hand instead; passing both is refused. `--out` defaults to `work/<name>/`. |
| `render --template FILE` | render into a different sheet template (default `motion-artist/templates/sheet.html`) |
| `render --pingpong` | walk the same cells out and back; the return leg reverses the out leg, so the seam is clean and the sheet still holds only the requested frames |
| `pose-grid --cols` | pose cards per row; default `4` |
| `pose-grid --no-labels` | drop the frame-number band, and say so in the sidecar |
| `export --out`, `--sheet` | bundle path (default `exports/<genre>/<genre>-NN/`, an uncompressed directory, resolved against the `work/` the capture sits under) and the sheet HTML to include (default `<name>-motion.html` beside the json) |

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
job input references. It warns if `arc` is empty or any frame has no pose. A re-cut keeps its
`<genre>-NN` and replaces that bundle in place, so no stale twin is left behind to be referenced —
but the SHA-256 changes, so re-reference it rather than assuming the old digest still holds.

Downloaded video, split frames, captures and `clips.json` all live under `work/<creator>-<title>/`,
git-ignored. Finished bundles land in `exports/<genre>/`, also git-ignored: they are build output,
large and re-digested on every re-cut, and the hand-off was always the path and the SHA-256 the
exporter prints rather than a committed file.

`clips.json` is ignored with them, so **the frames you marked in the clipper live only in your
working copy**. Nothing else records them — a fresh clone starts with no marks, and a discarded
worktree takes its videos with it. Keep a copy outside the repo if a video's boundaries were
expensive to find.
