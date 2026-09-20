---
name: motion-artist
description: Turn a YouTube (or local) video of a person moving into a frame-by-frame motion source for animation artists — an HTML motion sheet with per-frame pose instructions at a declared fps and frame count, and a pose grid of the traced frames for conditioning a generator, written in KaraokeParty-Graphics motion-director vocabulary (character-left/right, keys, pilots, loop seam). Use when the user says "motion artist", "turn this video into animation frames", "motion sheet", "reference this dance for the sprite", "pose sheet from video", or hands you a video link plus fps/frames.
metadata:
  short-description: Video → motion sheet and pose grid for animation agents
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
| `--frames` | yes | total frame count of the target animation. No ceiling — one motion is one capture and one bundle, however long. Prefer a multiple of 4; see below. |
| `--start` / `--end` | recommended | trim the span to inspect, seconds or `m:ss`. Without `--end`, the span is `frames / fps` seconds of real time from `--start`. With both, that span is time-stretched onto the frame count (the sheet reports the speed factor). Neither end is taken literally unless `--margin 0` — see below. |
| `--playback` | default `loop` | `loop` (samples exclude `end`, so the last→first cut is one natural step), `one-shot`, `final-hold`. **Only `loop` reaches CAG as meaning.** Its sole test is `playback == "loop"`; everything else is just not-loop, and whether a set holds its last frame comes from CAG's own per-set plan, never from the bundle — so `final-hold` is decoration there today. It still changes what happens *here*: a non-loop span is snapped to the stillest ending rather than to matching poses. For `loop`, frame 0 and the last frame should both land on their feet — a loop seam an artist has to hold mid-air has no stable pose to draw. `extract` warns when either boundary frame comes back airborne; pick a different `--start`/`--search` window when it does. |
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

## Frame count: any length, but prefer a multiple of 12

One motion is **one capture and one bundle**, whatever its length. Do not split a long motion into
several — CAG has no concept of chaining bundles, so two bundles are two unrelated animations there,
each with its own frame sheet, its own proof and its own registration scale, and the loop relation
between them is lost.

The number 12 belongs to CAG's renderer, not to the bundle: it draws at most 12 figures per image
on a grid four wide, and chunks a longer motion across as many renders as it needs before
assembling one sheet. A 24-frame bundle renders as 12 + 12, a 32 as 12 + 12 + 8, a 14 as 12 + 2 —
all one animation out the other side. That ceiling is not reachable from here: `sheet()` chunks by
`SHEET_FRAMES` unconditionally and no spec field raises it, so nothing this skill ships can put more
than 12 figures in one render whatever `frame_count` says. (A scratch script calling `draw()`
directly can, and does tile one pose across the last row past 12 — but that path is below CAG.)

**Each chunk is a separate generation call, and every boundary is a chance for the character to
drift** — costume, face, proportions. The approved key art is attached to every render, which is
real mitigation and is also exactly what failed to stop a dancer's trainers arriving on a lifted
foot. Nothing measures identity across chunks and nothing warns. So prefer a count that divides by
12: it costs the fewest calls. 24 is two; 26 is three, one of them drawing two figures.

Failing that, prefer a multiple of 4, so the last row of the last chunk fills rather than sitting
part-empty. That is wasted cells, not a failure, and `extract` says so as a note. Nothing breaks
at 14.

What does **not** need managing from here is drawn size. The generator draws figures bigger in a
sparse render than a full one, so a 12 + 2 chunking draws its last two much larger — but
registration measures each chunk separately and normalises it out, deliberately, because one factor
across both put a size pop at the seam. Measured on 16 frames with the second chunk drawn at twice
the height of the first: registered cells came out 389px and 393px, a 4px spread, about 1%, and that
residual is rounding between two source sizes rather than drift.

## `thumbs/` is a contract output

`thumbs/fNN.jpg` — one per frame, in frame order, count matching `frame_count` — is **the pose
reference CAG draws from**, not a preview convenience. Its strip is built straight from them.

A/B'd on one clip at 8 frames, drawn pose cards against the raw traced frames: pose
fidelity was a wash (roll-to-roll spread wider than the gap between conditions), but amplitude — how
big the drawn movement is against the trace — was not. Skeleton-conditioned runs landed at 0.43–0.70
of the real movement and read as a timid sway; photo-conditioned runs sit at 0.9–1.2 and read as a
real dance. A pose can rank perfectly and still read flat if it is drawn at half size.

So **the photographs are the pose route, and the only one.** Skeletons are out. Nothing this skill
ships draws a figure at all: `pose-grid` tiles `thumbs/`, and the motion sheet's stage and frame strip
show the traced frames. The drawing code is deleted, not bypassed. CAG agrees from its side — it
takes the photographs when a bundle carries a complete set, and its own pose renderer is gone too.
Do not reintroduce a drawn pose route without a measurement that beats 0.9–1.2 on amplitude.

What this asks of a capture: a tight, consistent crop with the dancer fully in frame, every frame
tracked — a frame with no pose writes no thumb, which slides the whole strip out of step with the
frame indices, and `pose-grid` refuses to build on an incomplete set. `export` warns when the thumb
count and `frame_count` disagree.

Thumbs are written 200px wide at JPEG quality **88**. Both numbers are deliberate. 200px is the
width every amplitude measurement above was taken at, and CAG letterboxes each thumb onto a 384×512
card at native size, so it is demonstrably enough; 384 wide would fill the card exactly, but it is
not the measured condition, so it needs a measurement before it is worth taking. The quality came up
from 60 because JPEG artefacts at 60 sit on the limb edges, which is precisely what the generator is
reading off them. That one is reasoning about the failure mode, not a measured delta.

Known failure mode of the photo route: photographs bleed the **dancer's costume** into the character
(a brown boot came back as the dancer's white sneaker on a lifted foot, 2 figures in 16). It is fixed
on the CAG side with a positive costume sentence — nothing to do here. Worth carrying the general
lesson though: telling the model to *ignore* the clothing did not work. Negation is weak; naming the
right thing positively is what held. That applies to any prompt text this skill generates.

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
   spacing — the traced frame shows it — so a note is the only place to insist on it when a frame
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
   clean and the artist still draws only the frames asked for. It is a **playback flag on the sheet
   only** — it adds no cells, leaves `frame_count` alone, and does not reach `manifest.json`. CAG
   never learns of it, and reverses nothing on its own, so an out-and-back that exists only as this
   flag appears in neither the frame sheet nor the proof. If the delivered asset has to show it,
   trace the return leg as real frames.
   The sheet is rendered from `motion-artist/templates/sheet.html` — markup, CSS and player in one
   file, with `{{PLACEHOLDER}}`s the script fills. Change how a sheet looks by editing that template;
   pass `--template FILE` to render into a different one.
   Output `work/dance/dance-motion.html`: masthead (frames, fps, lap, playback, view, key and pilot
   indices), a stage holding the traced frame and its cue, a transport (play/scrub, rate, mirror),
   the frame strip, the pose grid, the arc, a fixed "for the artist agent" brief, and
   the full frame-note table. A `<script type="application/json" id="motion">` block carries the
   data for machine readers.
4. Build the pose grid — the one image you hand a generator the whole set in:
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" pose-grid work/dance/motion.json
   ```
   Tiles `thumbs/` — the footage, never drawn figures; see above for why — **four across and twelve
   to a sheet**, which is CAG's own render grid, so one sheet is exactly one of its generation calls
   and a longer motion chunks across several rather than growing one. Writes
   `dance-pose-grid.png` for a single image, or `dance-pose-grid-00.png`, `-01.png` … when it
   chunks, plus a `dance-pose-grid.json` sidecar declaring the geometry in PNG pixels (`cols`, `rows`,
   `tile_w/h`, `cell_w/h`, `label_h`, `per_sheet`, `sheets`) so a consumer slices by stated numbers
   instead of measuring the picture back out of it. `--no-labels` drops the frame-number band and
   says so in the sidecar; `--cols` overrides the four. It needs a complete `thumbs/` set and stops
   if one is missing rather than writing a sheet whose tiles are off by one.
5. Show it: open the HTML in the browser, or publish it as an Artifact when the user wants a link.
6. Export — always finish here. The capture is not done until it is bundled:
   ```bash
   python3 "$SKILL/scripts/motion_artist.py" export work/dance/motion.json
   ```
   One motion is one bundle, however many frames it has.
   Writes `exports/dance-b0ARQ5kM85Y-16.0s-24f-4fps-motion-source.zip` — always `exports/` at the
   repo root, never inside
   the capture dir: `motion.json`, the sheet HTML, `thumbs/` (the pose reference — see above), any
   pose grid images and their sidecar, and a
   generated `manifest.json` (fps, frame count, playback, view, `seam` and `seam_ratio`, source, and
   a SHA-256 per file), all under a `<name>/` folder. The sidecar's grid rides in the manifest as a
   `pose_grid` block. CAG itself no longer reads either one — it takes the loose `thumbs/` and tiles
   its own grid at render time — so the sheets ride along for anyone handing a generator the whole
   pose set directly, and cost the consumer nothing. Prints the bundle path and the zip's own
   SHA-256 — that
   pair is what the motion-director job input references. It warns when `arc` is still empty or
   frames are missing a pose; fix those and re-export rather than handing off a warned bundle.

## Hand-off to KaraokeParty-Graphics

Copy `exports/<name>-<video id>-<start>s-<frames>f-<fps>fps-motion-source.zip` into that repo's ignored
`work/<character>/motion-source/` and reference it by path and by the SHA-256 the export printed,
as the authorized motion source in the motion-director job input. A spec names **one** bundle per
animation (`spec.motions[set_name]` → one bundle directory); CAG chunks it across renders itself.
**Delete the bundle the copy supersedes in the same step.** A bundle is named by its trace, so a
re-cut of the same move lands beside the old one rather than over it, and the stale zip stays
referenceable — a spec pointed at it renders last week's motion with this week's SHA-256 in the job
input. Copy new, remove deprecated, one action.
The bundle's `manifest.json`
carries a SHA-256 per file, so an unzipped copy can be verified file by file. Do not commit
captures there; the repo excludes motion captures by policy. The sheet's
"view" is the *filmed* view — the manifest's `view` still governs the rendered character.

## Vocabulary (agreed with the character generator's repo)

"Sheet" named five objects across the two repos and "grid" three, which produced three wrong
conclusions in one session. **Use these and nothing else; if a term is missing, agree it before
using it.** Never say "sprite sheet" or "skeleton" — both are retired.

| term | is |
| --- | --- |
| motion bundle | the exported directory, identified by its manifest |
| manifest | `manifest.json` |
| motion sheet | the contents of `motion.json` — the only allowed use of "sheet" unqualified |
| traced frame | one photograph of the performer, `thumbs/fNN.jpg` |
| cue / arc | one frame's prose pose / the set's prose shape |
| pose card | one traced frame letterboxed onto a 384×512 card |
| pose grid | pose cards tiled four across, twelve per image |
| pose reference | umbrella: whatever images show the generator the pose |
| frame sheet | **the deliverable** — the finished drawn character, one set per image |
| key art / bible | the approved character render / the identity text in every prompt |
| set | one animation: dance, sing, flinch, guard, entrance, victory, ko |

A motion sheet is a **traced sheet** when it came from a bundle, a **written sheet** when the
consumer generated it from prose — a written sheet has no traced frames and so no pose reference.

## Notes

- Dependencies: `yt-dlp`, `python3` with `opencv-python` and `mediapipe<1` (the 1.x wheel crashes in
  the Metal helper on macOS). Model is cached at `~/.cache/motion-artist/`.
- `selftest` runs the pose-description, span-picking, arc carry-over, figure-geometry and manifest
  checks:
  `python3 "$SKILL/scripts/motion_artist.py" selftest`. Run it after touching any of them.
- Every frame is scaled about the hips so torso length matches the clip median: camera zoom or distance never changes the traced pose's size.
- Cues are heuristic: elbow and knee angles, girdle twist and sole pitch from the world landmarks,
  wrist and hip heights from the image landmarks. They are guidance for the artist, not measurements.
- `pts` stay load-bearing even though the pose reference is now the photographs. CAG's registration
  reads the drawn figure against what a frame's own landmarks say the pose's extent is versus the
  character's crown-to-heel, which is how it recovers the real character height whatever
  magnification the generator picked, and so how it holds one scale across chunks. A set with no
  landmarks falls back to a per-chunk height cluster. Keep them accurate; do not hand-edit them.
- `seam` is read on the CAG side, not just by us: before drawing, it logs that the proof will jump
  from the last frame back to the first unless the verdict is `clean` or empty (an unset seam reads
  as no complaint), and `cag sheets` prints it per bundle. `seam_ratio` rides alongside it — the
  same cut as a number rather than a word, so a log can say "loops on a 2.3-step cut". It is
  additive and nothing reads it yet. `schema` does not move for an added key.
- The bundle's `schema` stays `motion-artist/2`. That version means the landmarks carry a third
  float, `z`, which is still the layout; extra joints in `pts` and reworded `features` are content
  inside it, and CAG reads `pts` joint by joint, so a bundle keeps loading. Bump it only when the
  *shape* changes — and add the new value to `SCHEMAS` in that repo's `cag/motion.py` first, or
  `read_bundle` refuses every bundle you ship.
- The cue names girdle twist in words ("hips turned character-left against the shoulders") whenever
  the two girdles differ by 10° or more. Note that **torso rotation does not survive into the CAG
  render**: a frame traced at 28° of body yaw comes back drawn square-on every time, at 1, 4, 8 and
  12 figures per render, from photographs or skeletons, across three prompt rewordings. Do not spend
  effort encoding yaw more richly on this side expecting CAG to use it. It is not a bundle problem.
- `floor_y` is a **classification threshold, not a ground plane**. It is an 85th *percentile* of the
  lower sole, so by construction about one frame in seven sits below it. It is what `airborne` and
  planted compare the sole against — the sole, not the ankle, because an ankle rides well above the
  floor whenever the dancer is on the balls of her feet, which is not airborne. A renderer that wants
  an actual ground line must use the max of all soles in the clip, or it will draw through feet.
- The sheet is dark only. It carries one `:root` and no `prefers-color-scheme` block, so it looks
  the same whatever the reader's OS is set to.
- The template opens with `<meta charset="utf-8">`. Every cue carries en-dashes, degree signs and
  em-dashes, and a server that sends no charset will mojibake the whole sheet without it.
- Neither side draws a skeleton any more. CAG used to rebuild each pose from the raw landmarks in
  `cag/skeleton.py`; that file is gone, and its own notes call redrawing what the bundle already
  ships the mistake it was — it threw away every hand, every heel and both girdles, then asked a
  generator to draw a dance whose engine is the pelvis turning against the shoulders. Do not describe
  either repo as drawing a skeleton, and do not add one back here.
- The cue names footwork in words ("on the balls of both feet, heels lifted") for planted feet only;
  a lifted foot already says so.
- Sole pitch is measured in 3D. In image x alone a foot pointed at the camera foreshortens to a
  near-vertical sole, which reads as a dramatic heel lift that is not happening — the same trap the
  stance word fell into.
- The cue says nothing about the hand beyond the arm height and elbow it already carries. MediaPipe's
  finger landmarks are the least reliable thing it returns — on the shuffle clip the index-to-pinky
  span resolved on 1 hand in 64 — so there is no honest measurement to report.
- Cues deliberately carry **no stance or foot-spacing word**. Ankle spread measured in the image
  cannot tell a wide stance from a fore-aft step seen at an angle, so the word flipped between
  neighbouring frames the dancer never moved between, and a generator reading it drew a strobe.
  The traced frame already shows foot spacing. Do not add the word back.
