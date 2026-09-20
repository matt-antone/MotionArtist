# MotionArtist

A Claude Code skill that turns a video of a person moving into a **motion sheet** for animation
agents: skeletal stick-figure frames at a declared fps and frame count, each with a pose
instruction written in the vocabulary used by
[KaraokeParty-Graphics](https://github.com/matt-antone/KaraokeParty-Graphics) motion and
keyframe roles (character-left / character-right, keys, pilots, loop seam). The sheet is a
motion source: it describes motion only, never character scale, identity, view or prop hand.

Demo sheets:

- 16 frames at 4 fps from a dance short: https://claude.ai/code/artifact/cb0e7b85-66a2-4ce2-a1c9-c7dac23abd09
- best 8-frame, 2 fps loop found inside a 13 s range: https://claude.ai/code/artifact/44e63959-59d7-4931-8d3f-ff3abda6d2b3

```
motion-artist/
  SKILL.md                 # the skill (what Claude does, step by step)
  scripts/motion_artist.py # extract (video → motion.json + thumbs), render (→ HTML), export (→ bundle)
AGENTS.md                  # how to work in this repo: pipeline, conventions, verification
```

## Install

```bash
pip install "mediapipe<1" opencv-python   # plus yt-dlp on PATH
ln -s "$PWD/motion-artist" ~/.claude/skills/motion-artist   # or copy into a repo's .claude/skills or .agents/skills
```

`mediapipe<1` is deliberate: the 1.x macOS wheel aborts in its Metal helper. The pose model is
downloaded once to `~/.cache/motion-artist/`.

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
python3 motion-artist/scripts/motion_artist.py export work/dance/motion.json
```

| Flag | Meaning |
| --- | --- |
| `--fps`, `--frames` | rate and total frame count of the target animation (required) |
| `--start`, `--end` | trim the span to inspect, seconds or `m:ss`. Without `--end`, the span is `frames / fps` seconds of real time. With both, the span is time-stretched onto the frame count. |
| `--search` | find the best loop: slide a `frames / fps`-second window over `--start..--end` (whole video if `--end` is omitted), score each start by loop-closure pose distance against motion energy, pick the tightest seam among the livelier half |
| `--window` | source seconds the search looks for, when that differs from `frames / fps` (a scene cut leaves a short usable span, or a fast move should play slower); the winner is stretched onto the frame count |
| `--stabilize` | centre the hips horizontally in every frame; use for a moving camera or a travelling performer (airborne is then never called, since there is no fixed floor). Body scale is always normalised per frame from pixels-per-metre, so camera zoom never changes the skeleton's size |
| `--playback` | `loop` (default), `one-shot`, `final-hold` |
| `--exaggerate` | amplify each landmark's deviation from the clip-mean pose; default `1.25`, `1.0` = as filmed |
| `--name`, `--out` | output slug and directory (default `work/<name>/`) |
| `render --repeat N` | play the cycle N times back to back in the sheet |
| `render --pingpong` | play the cycle out and back; the return leg reverses the out leg, so the seam is clean and only the out leg needs drawing |
| `export --out`, `--sheet` | bundle path (default `exports/<name>-<frames>f-<fps>fps-motion-source.zip`) and the sheet HTML to include (default `<name>-motion.html` beside the json) |

`extract` prints one line per frame (index, source time, key / pilot / in-between, pace, pose cue)
and writes `motion.json` plus `thumbs/`. Between `extract` and `render`, fill `arc` and any
per-frame `note` in `motion.json`; the sheet renders them. `selftest` checks the pose heuristics
and the export manifest.

The rendered sheet has the stick figure beside the source frame, play / scrub / rate / mirror
controls, the frame strip (keys pink, pilots blue), a facing line on the head, the performance
arc, a brief for the artist agent, the frame-note table, and the data as embedded JSON.

`export` is the last step: it zips `motion.json`, the sheet and `thumbs/` with a generated
`manifest.json` (fps, frame count, playback, view, seam, source, and a SHA-256 per file), and
prints the bundle path and the zip's SHA-256 — the pair a KaraokeParty-Graphics motion-director
job input references. It warns if `arc` is empty or any frame has no pose.

Outputs live under `work/` (git-ignored, as are downloaded videos).
