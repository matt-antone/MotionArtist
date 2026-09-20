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
| `--start` / `--end` | recommended | trim the span to inspect, seconds or `m:ss`. Without `--end`, the span is `frames / fps` seconds of real time from `--start`. With both, that span is time-stretched onto the frame count (the sheet reports the speed factor). Neither end is taken literally unless `--margin 0` — see below. |
| `--playback` | default `loop` | `loop` (samples exclude `end`, so the last→first cut is one natural step), `one-shot`, `final-hold`. For `loop`, frame 0 and the last frame should both land on their feet — a loop seam an artist has to hold mid-air has no stable pose to draw. `extract` warns when either boundary frame comes back airborne; pick a different `--start`/`--search` window when it does. |
| `--name` | optional | slug for the output dir and title |
| `--margin` | default `0.5` s | slack around **both** `--start` and `--end`. The span you type is a guess; the move's own cut rarely lands on that exact second. Both ends are probed within the margin for the real one — matching poses for a `loop`, the stillest ending otherwise — and the winner is time-stretched onto the frame count, so the source span may come back a little shorter or longer than asked. `--margin 0` uses the span exactly as typed. Ignored with `--search`, which picks its own span. |
| `--search` | optional | "find the best loop": slide a `frames / fps`-second window over `--start..--end` (whole video when `--end` is omitted), score each start by loop-closure distance vs motion energy, and use the tightest seam among the livelier half. Prints the top candidates. |
| `--window` | optional | source seconds to take, when that differs from `frames / fps` — what `--search` looks for, and the span length when `--end` is omitted. Use it when a scene cut or lost tracking leaves less usable footage than the playback length, or to run a fast move slower. The winner is stretched onto the frame count. |
| `--stabilize` | optional | centre the hips horizontally in every frame. Use when the camera pans, tilts or zooms, or the performer travels; otherwise the seam check and the strip show camera motion as body motion. With a moving camera there is no fixed floor, so airborne is never called: the lower sole counts as planted. |
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
   It prints a `snap:` line first — the span it moved to inside `--margin`, its seam and energy
   score, and the runners-up — then writes `work/<name>/motion.json`, `work/<name>/thumbs/`, and
   one line per frame:
   index, source time, role (`key` = hold/extreme, `pilot` = fastest transition, `inbetween`),
   pace, and the generated pose cue. Frame 0 is always a key. Check `missing` is empty and the
   `seam` verdict; if a pose is missing or the view is wrong, re-run with a cleaner span. Watch the
   reported source speed too: snapping to a shorter or longer cut stretches it onto the same frame
   count, so a big margin on a short span can hand the artist noticeable slow motion. Widen the
   margin when the seam stays poor, drop it to `0` when the span is already exact (a beat grid, a
   cut you measured). A re-run rebuilds `motion.json` from the video, so it also prints an `arc:`
   line saying what happened to the hand-written arc and frame notes: carried over when the new
   capture lands on the same span, fps and frame count, `DROPPED` when any of those moved — because
   the same frame index then holds a different pose. Re-author after a `DROPPED`; there is nothing
   to salvage.
2. Author the performance arc (re-running step 1 will not eat it — see the `arc:` line).
   Read the printed table (not the JSON — it is large), look at a few
   thumbs if needed, and fill `arc` in `motion.json` with 1–3 short paragraphs in motion-director
   vocabulary: anticipation, action, weight change, follow-through, recovery, holds, and the loop
   seam or terminal hold. Add a per-frame `note` only where the generated cue misses intent
   (e.g. "fists pump on the beat", "this is the hit pose"). Cues say nothing about stance or foot
   spacing — the skeleton draws it — so a note is the only place to insist on it when a frame
   depends on it ("feet wider than the shoulders here"). Use `character-left` / `character-right`
   only; never screen sides for anatomy. Do not edit `cue`, `role`, `depth` or `pts`.
   Depth is extractor output too: `pts` is `[x, y, z]` per landmark (z negative toward the camera,
   hips at zero, same units as x) and includes the wrist-plane landmarks (`indexL/R`, `pinkyL/R`)
   the hand box uses; each frame carries `depth` (`near_side` plus a `near`/`far`/`level` verdict
   per limb), and the cue names which leg is behind whenever the legs overlap.
3. Render:
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" render work/dance/motion.json
   ```
   The sheet always holds exactly `--frames` cells, and the player loops them forever — a seam is
   reviewed by watching, never by drawing the cycle twice. Add `--pingpong` to walk those same cells
   out and back (0..N-1 then N-2..1): the return leg is the out leg reversed, so the seam is always
   clean and the artist still draws only the frames asked for.
   The sheet is rendered from `motion-artist/templates/sheet.html` — markup, CSS and player in one
   file, with `{{PLACEHOLDER}}`s the script fills. Change how a sheet looks by editing that template;
   pass `--template FILE` to render into a different one.
   Output `work/dance/dance-motion.html`: masthead (frames, fps, lap, playback, view, key and pilot
   indices), a stage with the big stick figure — pelvis and rib cage drawn as boxes in their own
   colour, the heavy face being the side turned toward the camera, so hip movement and the wind-up
   between hips and shoulders read at a glance, a brow and nose line for head facing, a box per
   hand, each foot in two boxes hinged at the ball, and every limb inked by the side it belongs to
   (character-left one colour, character-right another) — the source frame and the cue, a transport
   (play/scrub, rate, mirror), the frame strip, the arc, a fixed "for the artist agent" brief, and
   the full frame-note table. A `<script type="application/json" id="motion">` block carries the
   data for machine readers.
4. Show it: open the HTML in the browser, or publish it as an Artifact when the user wants a link.
5. Export — always finish here. The capture is not done until it is bundled:
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" export work/dance/motion.json
   ```
   Writes `exports/dance-16f-4fps-motion-source.zip` — always `exports/` at the repo root, the
   name carrying the frame count and fps, never inside
   the capture dir: `motion.json`, the sheet HTML, `thumbs/` and a
   generated `manifest.json` (fps, frame count, playback, view, seam, source, and a SHA-256 per
   file), all under a `<name>/` folder. Prints the bundle path and the zip's own SHA-256 — that
   pair is what the motion-director job input references. It warns when `arc` is still empty or
   frames are missing a pose; fix those and re-export rather than handing off a warned bundle.

## Hand-off to KaraokeParty-Graphics

Copy `exports/<name>-<frames>f-<fps>fps-motion-source.zip` into that repo's ignored
`work/<character>/motion-source/` and reference it by path and by the SHA-256 the export printed,
as the authorized motion source in the motion-director job input. The bundle's `manifest.json`
carries a SHA-256 per file, so an unzipped copy can be verified file by file. Do not commit
captures there; the repo excludes motion captures by policy. The sheet's
"view" is the *filmed* view — the manifest's `view` still governs the rendered character.

## Notes

- Dependencies: `yt-dlp`, `python3` with `opencv-python` and `mediapipe<1` (the 1.x wheel crashes in
  the Metal helper on macOS). Model is cached at `~/.cache/motion-artist/`.
- `selftest` runs the pose-description, span-picking, arc carry-over and figure-geometry checks:
  `python3 "$SKILL/scripts/motion_artist.py" selftest`. Run it after touching any of them.
- Every frame is scaled about the hips so torso length matches the clip median: camera zoom or distance never changes skeleton size.
- Cues are heuristic: elbow and knee angles, girdle twist and sole pitch from the world landmarks,
  wrist and hip heights from the image landmarks. They are guidance for the artist, not measurements.
- The bundle's `schema` stays `motion-artist/2`. That version means the landmarks carry a third
  float, `z`, which is still the layout; extra joints in `pts` and reworded `features` are content
  inside it, and CAG reads `pts` joint by joint, so a bundle keeps loading. Bump it only when the
  *shape* changes — and add the new value to `SCHEMAS` in that repo's `cag/motion.py` first, or
  `read_bundle` refuses every bundle you ship.
- The pelvis and rib cage are boxes, not bars. A bar seen from an angle is just a shorter bar, so a
  turn reads as nothing; a box turns visibly. Both are sized off the spine rather than off their own
  width, so a turn that narrows the girdle cannot also shrink the box and cancel what it is drawn to
  show. The cue names the same thing in words ("hips turned character-left against the shoulders")
  whenever the two girdles differ by 10° or more.
- The dashed line under the figure is the floor, drawn at the **max** sole depth in the whole clip
  (`compute_box`), so nothing ever crosses it. It is **not** `floor_y` from the JSON: that one is an
  85th *percentile* of the lower sole, so by construction about one frame in seven sits below it —
  drawing the line there would cut through those feet. `floor_y` is a classification threshold, not
  a ground plane: it is what `airborne` and planted compare the sole against (the sole, not the
  ankle — an ankle rides well above the floor whenever the dancer is on the balls of her feet, which
  is not airborne). Do not point the drawn line back at it, and if a downstream renderer draws its
  own floor, it must use max-of-soles too or it will show feet below the line.
- The sheet is dark only. It carries one `:root` and no `prefers-color-scheme` block, so it looks
  the same whatever the reader's OS is set to.
- The template opens with `<meta charset="utf-8">`. Every cue carries en-dashes, degree signs and
  em-dashes, and a server that sends no charset will mojibake the whole sheet without it.
- Limbs are inked by side, so character-left and character-right never have to be worked out from
  the pose; only the spine, head and girdles stay neutral. That replaced the hollow rings that used
  to mark the character-right wrist and ankle, which were saying the same thing a second time. This
  is the sheet's own figure — CAG draws its generator-facing skeleton separately in `cag/skeleton.py`
  and still rings those joints, as its prompts describe.
- Each foot is two boxes hinged at the ball, because that hinge is the footwork: on the ball the
  sole pitches up while the toes stay down, and one rigid foot box cannot show it. Nothing is
  tracked past the ball, so the toe plate is **inferred** — it flattens toward the floor once the
  heel lifts and otherwise carries on the line of the sole. The cue names the same thing in words
  ("on the balls of both feet, heels lifted") for planted feet only; a lifted foot already says so.
- Sole pitch is measured in 3D. In image x alone a foot pointed at the camera foreshortens to a
  near-vertical sole, which reads as a dramatic heel lift that is not happening — the same trap the
  stance word fell into.
- Hands are one box each. MediaPipe's finger landmarks are the least reliable thing it returns (on
  the shuffle clip the index-to-pinky span resolved on 1 hand in 64), so they are used only when
  they resolve to a believable hand width, and otherwise the hand is **guessed** to carry on the
  line of the forearm — what an artist would assume anyway. The cue says nothing about the hand
  beyond the arm height and elbow it already carries; there is no honest measurement to report.
- Cues deliberately carry **no stance or foot-spacing word**. Ankle spread measured in the image
  cannot tell a wide stance from a fore-aft step seen at an angle, so the word flipped between
  neighbouring frames the dancer never moved between, and a generator reading it drew a strobe.
  The skeleton already shows foot spacing. Do not add the word back.
