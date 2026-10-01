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
  workflows/wan22-5b.json      # native ComfyUI API workflow for local AI previews
  scripts/motion_artist.py    # generate (prompt → local AI preview), extract (video → motion.json + thumbs), render (→ HTML), pose-grid (→ pose grid), export (→ bundle), trace (images → landmarks)
  scripts/clipper.py          # browser tool: step a video frame by frame and mark clip in/out points
  templates/sheet.html        # the motion sheet's markup, CSS and player; render() fills its placeholders
AGENTS.md                     # how to work in this repo: pipeline, conventions, verification
```

## Install

```bash
export MOTION_ARTIST_HOME="${MOTION_ARTIST_HOME:-$HOME/.cache/motion-artist}"
uv venv --python 3.12 "$MOTION_ARTIST_HOME/.venv"
uv pip install --python "$MOTION_ARTIST_HOME/.venv/bin/python" "mediapipe<1" opencv-python
source "$MOTION_ARTIST_HOME/.venv/bin/activate"   # plus yt-dlp, ffmpeg/ffprobe and rclone on PATH
ln -s "$PWD/motion-artist" ~/.claude/skills/motion-artist   # or copy into a repo's .claude/skills or .agents/skills
```

`mediapipe<1` is deliberate: the 1.x macOS wheel aborts in its Metal helper. The pose model is
downloaded once to `~/.cache/motion-artist/`.

## Local AI video generation

Describe a movement, generate one preview, approve its movement and framing, then mark the clip
and run the existing capture pipeline. One candidate is generated at a time. The separate Python
3.12 environment above preserves the shared ComfyUI/ROCm environment.
An optional starting image anchors the character, outfit, proportions, visual style and background.
Lighting follows the image unless a change is requested. Stylized key art still needs a moving preview
review and a tracking check.

Reuse `~/ComfyUI/run-local16.sh` and its RX 9070 settings. Put only missing Wan2.2 files in that
installation; reuse the installed `umt5_xxl_fp8_e4m3fn_scaled.safetensors` text encoder. Downloads
from the [official workflow guide](https://docs.comfy.org/tutorials/video/wan/wan2_2):

- [wan2.2_ti2v_5B_fp16.safetensors](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors) → `~/ComfyUI/models/diffusion_models/`
- [wan2.2_vae.safetensors](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/vae/wan2.2_vae.safetensors) → `~/ComfyUI/models/vae/`

```bash
python3 motion-artist/scripts/motion_artist.py generate \
  "Victory fist pump: prepare, raise character-right fist overhead once, then recover" \
  --title "Victory fist pump" --out generations/victory-01
# After approving the preview:
python3 motion-artist/scripts/clipper.py \
  --source "$MOTION_ARTIST_HOME/generations/victory-01/source.mp4" --genre emote
```

| Flag | Meaning |
| --- | --- |
| `generate PROMPT` | requested motion; expanded with full-body performer, fixed camera, plain light backdrop, visible hands/feet, preparation, action and recovery |
| `generate --title` | required preview title |
| `generate --out` | required local output directory, relative to `MOTION_ARTIST_HOME`; checkout paths refused |
| `generate --image` | optional starting image; letterboxed to preserve full framing before local upload |
| `generate --duration` | seconds, default `5`; source frames = `4 × ceil(seconds × 24 / 4) + 1` |
| `generate --seed` | unsigned 64-bit seed, default `1234` |
| `generate --comfy-url` | default `http://127.0.0.1:8188`; starts the default server through its existing launcher when needed |
| `clipper --source` | approved local video to import or reopen by content SHA-256 |
| `clipper --genre` | required with `--source`; motion genre |
| `clipper --title` | optional local title, default generation title or filename |

Defaults: 576×864, 24 fps, 20 steps, CFG 5, UniPC, simple schedule, shift 8. Five requested seconds
makes 121 source frames; the actual encoded duration is 5.042 seconds. This source count never sets
the capture count: the user's marks and capture fps do that.

`source.mp4` and `generation.json` stay local. The record carries requested/expanded/negative prompts,
seed, workflow digest, models, input-image digests, job id, observed running time, actual video
properties, sampled memory use and source SHA-256. Repeat the same command/directory to resume or reuse completed output;
changed settings need a new directory. The shared queue is left intact. A 90-minute running timeout
excludes queued time and preserves the job for resumption. An uncertain submission stops without a
duplicate job; failed jobs require a reviewed new candidate. Source approval and clip marking remain
user decisions. Generated footage is listed as **Local AI – Title**, ordinary local footage as
**Local – Title**. Generation provenance survives mark saves and reaches the exported manifest.

After marking, use every clip's recorded start/end, playback, capture fps/count, genre and capture
directory exactly, with `--margin 0` and the separate `--pingpong` flag where selected. Extract
allocates its motion number. Author the arc, render, build the pose grid and export the motion bundle.

Initial RX 9070 trial (2026-09-29): five requested seconds encoded as 121 frames in 215.8 seconds.
Five-second samples observed peak device use of 10.1 GiB and whole-system RAM use of 32.0 GiB;
these are sampled observations, not guaranteed maxima. Native VAE decoding fell back to tiled
decoding. Tracking found all 121 poses, but classified 46 as rear-facing. The user rejected the
preview because excessive brightness erased detail in the arms and face. A diffuse-light candidate
took 200.9 seconds, tracked all 121 poses with no rear frames, and observed 10.2 GiB device / 37.7 GiB
whole-system RAM use. It remains unapproved. The first pixel-key-art candidate took 204.2 seconds and
observed 10.3 GiB device / 39.5 GiB whole-system RAM use; it lost body detail, missed 69 poses and
classified 3 tracked frames as rear-facing. The user requested a hard-lighting revision. These are
five-second memory samples, not guaranteed maxima. Show the full moving preview
and wait for explicit approval before clip marking. Selftests, marker HTTP import/
save/reopen and a separate fixture's render/export/hash/CAG source-clip load passed. Later isolated
browser checks verified saved gender edits and reopening. These trials set no performance guarantee.

The hard-lighting key-art trial (2026-09-30) took 201.4 seconds and observed 11.2 GiB device /
39.8 GiB whole-system RAM use. Body detail still broke into magenta haze; tracking missed 66 of
121 poses and classified one tracked frame as rear-facing. It is unsuitable for extraction. Native
node connections, models and sampler settings match the official template; no wiring fault was found.

Follow-up diagnostics reused the saved hard-lighting render: all intermediate values were finite,
and a different tiled decode reproduced the breakup. A photographic starting-image control tracked
all 49 frames with no rear frames. A built-in imagegen edit prepared a neutral-background copy of
the pixel key art; the next five-second trial took 201.4 seconds and observed 10.3 GiB device /
40.3 GiB whole-system RAM use. All 121 poses tracked (115 three-quarter, 6 front, no rear frames).
The body stayed visible, supporting the saturated backdrop as a contributing factor. Detail still
softened and the arm had not fully recovered by the last frame. User review remains pending; no
motion number or accepted motion bundle has been created. Preserve original key art when preparing
starting-image copies, and record their provenance and digests.

## Marking clips

A source video holds several moves, and choosing where each one starts and ends by scrubbing a
YouTube player is guesswork — a frame either side is a visibly different pose. `clipper` is a
local web tool for settling that:

```bash
python3 motion-artist/scripts/clipper.py            # --port 8765 by default
```

Open `http://localhost:8765`, pick a video you have opened before — listed as `Creator - Title` —
or use **Browse local video**, paste a local path, or paste a YouTube URL for a new one.
Enter its **Motion genre** and click **Open**. Browser-selected footage uploads only to the local
marker and keeps the same content identity as a path import. It copies or downloads the video into
`work/<creator>-<title>/`, splits every source frame into `work/<creator>-<title>/frames/`, and serves a
frame-by-frame viewer. Step to the frame you want, mark in and out, drag either mark along the
frame track to adjust it, add it to the list.

Work is keyed by the video, so a clip can never be read against the wrong source. The directory is
named for the video to be recognisable, but its YouTube id is what decides whether two URLs are the
same video. Local videos use content SHA-256: identical footage reopens its frames and marks. A name already held by a different id becomes `<creator>-<title>-2`, so two uploads
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
handoff: `extract --start S --end S --margin 0` takes the seconds straight from it. The tool runs no part of the
pipeline — it only settles which frames the pipeline is pointed at.

To remove a video, open it and click **Delete video**. A confirmation alert names the video and
explains that its local source, frames, marks and captures will move to
`$MOTION_ARTIST_HOME/trash/videos/`. Cancel leaves everything in place. Published bundles and
Drive copies are kept. After deletion, the marker shows the recovery directory; move its video
folder back into `work/` to restore it when that name is free.

## Thumbs

Each thumb is a 384×512 crop around the performer — CAG's pose card size, so it fills the card.
`extract` takes one crop for the whole capture (the union of every frame's landmarks, padded and
grown to 3:4), so landscape and portrait footage both work with no extra step, and the dancer's
travel across the frame survives.

`motion_artist.py trace <images_dir> <out.json>` traces any folder of images into raw landmarks,
so a render and the thumbs it was drawn from can be compared like for like.

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
`extract` takes both. It adds no cells and reaches `motion.json` and the manifest. CAG reads it from `motion.json`;
`seam_ratio` still measures the straight loop for consumers that do not read the flag.

Or by hand:

```bash
python3 motion-artist/scripts/motion_artist.py extract "https://www.youtube.com/shorts/…" \
  --fps 4 --frames 16 --start 0:16 --end 0:20 --margin 0 --genre hiphop --out work/britney-spears-toxic/clip-01
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
| `--gender` | User-assigned character compatibility: `male`, `female`, `any`, or `unclassified` (JSON `null`). Defaults to the saved clip, then the previous capture, then unclassified. An explicit override is saved back into the clip after successful extraction. |
| `--search` | find the best loop: slide a `frames / fps`-second window over `--start..--end` (whole video if `--end` is omitted), score each start by loop-closure pose distance against motion energy, pick the tightest seam among the livelier half |
| `--window` | source seconds the search looks for, when that differs from `frames / fps` (a scene cut leaves a short usable span, or a fast move should play slower); the winner is stretched onto the frame count |
| `--stabilize` | centre the hips horizontally in every frame; use for a moving camera or a travelling performer (airborne is then never called, since there is no fixed floor). Body scale is always normalised per frame from pixels-per-metre, so camera zoom never changes the traced pose's size |
| `--playback` | `loop` (default), `one-shot`, `final-hold`. CAG reads `loop`; `one-shot` and `final-hold` both become `once` downstream |
| `--exaggerate` | amplify each landmark's deviation from the clip-mean pose; default `1.25`, `1.0` = as filmed |
| `--name`, `--genre`, `--out` | the capture's name and directory. `--genre hiphop` allocates the name as `<genre>-NN` against `exports/<genre>/`, records it in the video's `clips.json`, and needs `--out` (the clip's `capture` directory). `--name` names it by hand instead; passing both is refused. `--out` defaults to `work/<name>/`. |
| `render --template FILE` | render into a different motion-sheet template (default `motion-artist/templates/sheet.html`) |
| `pose-grid --cols` | pose cards per row; default `4` |
| `pose-grid --no-labels` | drop the frame-number band, and say so in the sidecar |
| `export --out`, `--sheet` | bundle path (default `exports/<genre>/<genre>-NN/`, an uncompressed directory, resolved against the `work/` the capture sits under) and the motion sheet HTML to include (default `<name>-motion.html` beside the json) |

The marker's **Gender** selector classifies each clip by which characters it suits. Edit a clip,
choose `male`, `female`, `any` or **unclassified**, and replace it to save. Extraction reads that
choice automatically and preserves it on re-cuts. Both `motion.json` and the manifest carry
`gender`, and the motion sheet displays it. Unclassified is JSON `null`, distinct from explicitly
choosing `any`; no classification is inferred from the filmed performer. Existing unclassified
bundles remain unclassified. CAG's agreed behavior preserves explicit motion assignments and warns
on mismatches; automatic selection prefers compatible motions.

`extract` prints one line per frame (index, source time, key / pilot / in-between, pace, pose cue)
and writes `motion.json`, `thumbs/`, and `clip.mp4`, `mask.mp4` and `heads.json` — the source clip,
performer mask and head boxes cag animates from (see SKILL.md). Landmarks carry depth — `pts` is `[x, y, z]`, z negative
toward the camera with the hips at zero — and every frame adds a `depth` block naming the near
side and each limb's `near` / `far` / `level`, so a 2D consumer can sort bones instead of guessing. Between `extract` and `render`, fill `arc` and any
per-frame `note` in `motion.json`; the sheet renders them. `selftest` checks pose heuristics, the export manifest and fake-ComfyUI generation/resumption.
`clipper.py --selftest` checks local identity, preserved marks, genre pinning and provenance.

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

`export` is the last step: it bundles the motion sheet, the HTML, `thumbs/` and any pose grid images with a
generated
`manifest.json` (fps, frame count, playback, view and the per-frame `view_frames` spread that
average hides, seam and the `seam_ratio` behind the verdict, `stabilized`, `exaggerate`,
`performer`, source, the pose grid's geometry, and a SHA-256 per file), and
prints the bundle directory and the manifest's SHA-256 — the pair a KaraokeParty-Graphics motion-director
job input references. It warns if `arc` is empty or any frame has no pose. A re-cut keeps its
`<genre>-NN` and replaces that bundle in place, so no stale twin is left behind to be referenced —
but the SHA-256 changes, so re-reference it rather than assuming the old digest still holds.

## Where motions are stored

Nothing is written into the repo. Every command runs from `$MOTION_ARTIST_HOME` (default
`~/.cache/motion-artist`), so the `work/` and `exports/` paths above are that directory's: local
staging for downloads, split frames, captures and bundles. Captures, marks and bundles are stored on Google Drive
through rclone, at `$MOTION_ARTIST_REMOTE` (default `kadrive:MotionArtist`, the Karaoke Arcade
account CharacterAssetGenerator also uses):

- bundles at `<genre>/<genre>-NN/`, pushed by `export`;
- captures at `captures/<creator>-<title>/clip-NN/`, pushed by `extract`, `render` and `pose-grid`;
- the clipper's marks at `captures/<creator>-<title>/clips.json`, pushed on every save.

A command whose push fails exits non-zero and keeps the local copy; re-run it once rclone works.
Every command refuses an output path inside the checkout. `MOTION_ARTIST_REMOTE=` (empty) turns
pushing off for offline tests.
