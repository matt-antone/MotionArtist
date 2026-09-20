---
name: motion-artist
description: Turn a YouTube (or local) video of a person moving into a frame-by-frame motion source for animation artists — an HTML motion sheet of skeletal stick-figure frames with per-frame pose instructions at a declared fps and frame count, written in KaraokeParty-Graphics motion-director vocabulary (character-left/right, keys, pilots, loop seam). Use when the user says "motion artist", "turn this video into animation frames", "motion sheet", "reference this dance for the sprite", "pose sheet from video", or hands you a video link plus fps/frames.
metadata:
  short-description: Video → skeletal motion sheet for animation agents
---

# motion-artist

Video of a person moving → `motion.json` + a self-contained HTML motion sheet. The sheet is a
**motion source** for the KaraokeParty-Graphics `motion_director` / `animation_keyframe_artist`
roles: it controls motion only, never character scale, identity, view or prop hand.

## Inputs (ask for anything missing)

| Input | Required | Notes |
| --- | --- | --- |
| video | yes | YouTube URL (incl. Shorts) or local file |
| `--fps` | yes | playback rate of the target animation |
| `--frames` | yes | total frame count of the target animation |
| `--start` / `--end` | recommended | trim the span to inspect, seconds or `m:ss`. Without `--end`, the span is `frames / fps` seconds of real time from `--start`. With both, that span is time-stretched onto the frame count (the sheet reports the speed factor). |
| `--playback` | default `loop` | `loop` (samples exclude `end`, so the last→first cut is one natural step), `one-shot`, `final-hold` |
| `--name` | optional | slug for the output dir and title |
| `--search` | optional | "find the best loop": slide a `frames / fps`-second window over `--start..--end` (whole video when `--end` is omitted), score each start by loop-closure distance vs motion energy, and use the tightest seam among the livelier half. Prints the top candidates. |
| `--window` | optional | source seconds the search looks for, when that differs from `frames / fps`. Use it when a scene cut or lost tracking leaves less usable footage than the playback length, or to run a fast move slower. The winner is stretched onto the frame count. |
| `--stabilize` | optional | centre the hips horizontally in every frame. Use when the camera pans, tilts or zooms, or the performer travels; otherwise the seam check and the strip show camera motion as body motion. With a moving camera there is no fixed floor, so airborne is never called: the lower ankle counts as planted. |
| `--exaggerate` | default `1.25` | amplify each landmark's deviation from the clip-mean pose; `1.0` = as filmed |

If the user gives no trim and the video is longer than ~15 s, make a contact sheet first
(sample one frame per second with cv2, tile them, Read the image) and propose a span. If they
ask for "the best loop" inside a range, pass the range as `--start/--end` plus `--search`. When the
search reports no fully-tracked window, the range holds a scene cut or a shot with no visible body:
sample it to find the usable span, then search inside that span with `--window`.

## Steps

`$SKILL` below is this skill's directory (the folder holding this SKILL.md).

1. Run the extractor (first run downloads the video ≤720p and the MediaPipe model):
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" extract URL --fps 4 --frames 16 --start 0:16 --end 0:20 --name dance
   ```
   It writes `work/<name>/motion.json`, `work/<name>/thumbs/`, and prints one line per frame:
   index, source time, role (`key` = hold/extreme, `pilot` = fastest transition, `inbetween`),
   pace, and the generated pose cue. Frame 0 is always a key. Check `missing` is empty and the
   `seam` verdict; if a pose is missing or the view is wrong, re-run with a cleaner span.
2. Author the performance arc. Read the printed table (not the JSON — it is large), look at a few
   thumbs if needed, and fill `arc` in `motion.json` with 1–3 short paragraphs in motion-director
   vocabulary: anticipation, action, weight change, follow-through, recovery, holds, and the loop
   seam or terminal hold. Add a per-frame `note` only where the generated cue misses intent
   (e.g. "fists pump on the beat", "this is the hit pose"). Use `character-left` / `character-right`
   only; never screen sides for anatomy. Do not edit `cue`, `role` or `pts`.
3. Render:
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" render work/dance/motion.json
   ```
   Add `--repeat 2` to play the cycle twice back to back in the sheet (frame numbers run on), which
   is how a loop seam should be reviewed. Add `--pingpong` to play it out and back (0..N then N-1..1):
   the return leg is the same poses reversed, so the seam is always clean and the artist draws only
   the out leg.
   Output `work/dance/dance-motion.html`: masthead (frames, fps, lap, playback, view, key and pilot
   indices), a stage with the big stick figure, the source frame and the cue, a transport
   (play/scrub, rate, mirror), the frame strip, the arc, a fixed "for the artist agent" brief, and
   the full frame-note table. A `<script type="application/json" id="motion">` block carries the
   data for machine readers.
4. Show it: open the HTML in the browser, or publish it as an Artifact when the user wants a link.
5. Export — always finish here. The capture is not done until it is bundled:
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" export work/dance/motion.json
   ```
   Writes `work/dance/dance-motion-source.zip`: `motion.json`, the sheet HTML, `thumbs/` and a
   generated `manifest.json` (fps, frame count, playback, view, seam, source, and a SHA-256 per
   file), all under a `<name>/` folder. Prints the bundle path and the zip's own SHA-256 — that
   pair is what the motion-director job input references. It warns when `arc` is still empty or
   frames are missing a pose; fix those and re-export rather than handing off a warned bundle.

## Hand-off to KaraokeParty-Graphics

Copy the exported `<name>-motion-source.zip` into that repo's ignored
`work/<character>/motion-source/` and reference it by path and by the SHA-256 the export printed,
as the authorized motion source in the motion-director job input. The bundle's `manifest.json`
carries a SHA-256 per file, so an unzipped copy can be verified file by file. Do not commit
captures there; the repo excludes motion captures by policy. The sheet's
"view" is the *filmed* view — the manifest's `view` still governs the rendered character.

## Notes

- Dependencies: `yt-dlp`, `python3` with `opencv-python` and `mediapipe<1` (the 1.x wheel crashes in
  the Metal helper on macOS). Model is cached at `~/.cache/motion-artist/`.
- `selftest` runs the pose-description checks: `python3 "$SKILL/scripts/motion_artist.py" selftest`.
- Every frame is scaled about the hips so torso length matches the clip median: camera zoom or distance never changes skeleton size.
- Cues are heuristic (elbow/knee angles from world landmarks, heights from image landmarks). They
  are guidance for the artist, not measurements.
