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
| `--playback` | default `loop` | `loop` (samples exclude `end`, so the last→first cut is one natural step), `one-shot`, `final-hold`. **Only `loop` reaches CAG as meaning.** Its sole test is `playback == "loop"`; everything else is just not-loop, and whether a set holds its last frame comes from CAG's own per-set plan, never from the bundle — so `final-hold` is decoration there today. It still changes what happens *here*: a non-loop span is snapped to the stillest ending rather than to matching poses. For `loop`, frame 0 and the last frame should both land on their feet — a loop seam an artist has to hold mid-air has no stable pose to draw. `extract` warns when either boundary frame comes back airborne; pick a different `--start`/`--search` window when it does. When the clip came from `clipper`, take this from the clip's `playback` rather than choosing it: the user picked it while watching the frames. |
| `--name` | yes | `<set>-<index>` — the set name the user gave for this video, plus this move's index. See **Naming** below. |
| `--pingpong` | optional | the capture plays out and back — `0..N-1..1` — so the seam is the motion reversed rather than a cut. Recorded in `motion.json` and the manifest as `pingpong`, and `render` walks the sheet that way without repeating the flag. **It buys nothing downstream on its own**: CAG has no ping-pong concept (nothing matches `pingpong` in `cag/`, and `playback` is only ever tested `== "loop"`), so until it reads the flag a consumer still jumps from the last frame to the first. `seam` and `seam_ratio` therefore keep measuring that straight loop — the flag never edits them, because the jump is what an unaware consumer plays. Do not bake the out-and-back into the frames instead: 8 frames is one CAG render chunk, 14 is two, and the duplicated return poses come back drawn differently in the second chunk. |
| `--performer` | optional | `female` or `male` — the filmed performer's body, carried into `motion.json` and the manifest. It describes the trace, not the character the render must draw; omit it rather than guessing. |
| `--margin` | default `0.5` s | slack around **both** `--start` and `--end`. The span you type is a guess; the move's own cut rarely lands on that exact second. Both ends are probed within the margin for the real one — matching poses for a `loop`, the stillest ending otherwise — and the winner is time-stretched onto the frame count, so the source span may come back a little shorter or longer than asked. `--margin 0` uses the span exactly as typed. Ignored with `--search`, which picks its own span. |
| `--search` | optional | "find the best loop": slide a `frames / fps`-second window over `--start..--end` (whole video when `--end` is omitted), score each start by loop-closure distance vs motion energy, and use the tightest seam among the livelier half. Prints the top candidates. **The window slides one source frame at a time** — see below. |
| `--window` | optional | source seconds to take, when that differs from `frames / fps` — what `--search` looks for, and the span length when `--end` is omitted. Use it when a scene cut or lost tracking leaves less usable footage than the playback length, or to run a fast move slower. The winner is stretched onto the frame count. |
| `--stabilize` | optional | centre the hips horizontally in every frame. Use when the camera pans, tilts or zooms, or the performer travels; otherwise the seam check and the strip show camera motion as body motion. With a moving camera there is no fixed floor, so airborne is never called: the lower sole counts as planted. |
| `--exaggerate` | default `1.25` | amplify each landmark's deviation from the clip-mean pose; `1.0` = as filmed |

If the user gives no trim and the video is longer than ~15 s, make a contact sheet first
(sample one frame per second with cv2, tile them, Read the image) and propose a span. If they
ask for "the best loop" inside a range, pass the range as `--start/--end` plus `--search`. When the
search reports no fully-tracked window, the range holds a scene cut or a shot with no visible body:
sample it to find the usable span, then search inside that span with `--window`.

## Cutting several moves out of one video

A set is one video, so the usual job is "extract as many unique moves from this URL as it holds".
The procedure that worked:

1. **Download once.** `yt-dlp` into `work/src/`, then run every `extract` against that local file with
   `--url` so the origin still reaches the manifest. Passing the URL to `extract` per move
   re-downloads the video once per move.
   When the user would rather point at frames than describe them, `clipper.py` is the faster path
   to the same list: they mark in and out per move in a browser and it writes
   `exports/<set>/clips.json`, whose `start`, `end`, `playback`, `capture_fps` and `capture_frames`
   are all taken as given — it derives the frame count from the marked window the right way round,
   so do not recompute it. Steps 2 and 3 are how you find the boundaries when nobody has marked
   them for you.
2. **Contact-sheet it** (one frame a second, tiled, labelled with the second). Many dance videos
   caption their own moves — "1. Skate", "2. Lock it Down" — and when they do, the captions *are*
   the move boundaries and no scoring is needed to find them.
3. **When nothing is labelled, score windows.** At the source's frame rate, score every
   (start, length) pair in a plausible length band; take the best-closing window, then the best that
   shares no footage with it, and so on. Reject a candidate that matches an earlier pick at some
   phase offset — a shuffle repeats its step for bars at a time, and each repeat is not a new move.
   Cache the pose walk per video: every one of these searches reads the same poses.
4. **Set the frame count from the window: `frames = fps x window`.** Never pick a frame count and
   an fps first and then go looking for a window that fits them — see **Timing** below. That is the
   single worst mistake available here and it is invisible in the output.
5. **Tempo is not a move.** A video that teaches a step slowly and then "speeds it up" has one move,
   not two: both passes are normalised onto the same frame count with the window matched to the
   cycle, so they come out as two copies of one animation. What *is* a second move is a stage that
   changes the body — legs-only before the arms are added.

**Read the contact sheet even when it costs context.** A video that captions its own moves is
authoritative and cheaper than any scoring: skipping four sheets to save tokens produced a set with
two bundles of the same move under different indices, one bundle straddling the boundary between two
moves, and an arc describing a captioned Charleston as "a crossing step that opens both arms wide".
The consumer rendered that as lunging with a knife. Where the source names a move, the source wins.

**Loop closure cannot tell dancing from talking.** The unlabelled search scores pose-loop closure
and motion energy, and a presenter gesturing to camera mid-sentence scores well on both. One
shipped bundle was a man explaining the dance with his hands — the consumer read his pointing at the
lens as choreography. Before trusting an unlabelled pick, look at the footage and confirm someone is
actually dancing in it.

**Emit the spread, not your judgement of it.** `view` is a single majority vote over the per-frame
classifications, so a set that is half front and half three-quarter, or one carrying five rear
frames inside a seven-way tie, reports one tidy word. A consumer then screens on a number that
cannot bear the weight — a character came back faceless for five of twenty-four frames off a bundle
whose declared `view` was perfectly legal. The manifest therefore also carries `view_frames`, the
raw per-frame counts.

Do not "fix" this by screening for purity here instead. A captioned Two-Step turns in **every**
window its footage contains, so a front-only rule deletes a real dance rather than protecting
anything. The consumer can set a threshold per character; it cannot recover what the manifest
averaged away. The one thing that is not a judgement call is a `back` frame: `cag/animation.py`
accepts only `front`, `left`, `right` and `3/4`, so a bundle declaring `back` fails outright.

Additive manifest keys are free: CAG requires exactly `fps`, `frame_count`, `playback`, `view` and
`files` and ignores everything else (`cag/motion.py`). Verify that against *their* `origin/main`
before relying on it — see the hand-off notes.

**Check framing on the frames, not the landmarks.** `thumbs/` is the pose reference, so a capture
whose figure leaves the frame hands the generator a body with no head or no feet. MediaPipe does not
report this: it **extrapolates** landmarks outside the image rather than dropping them, so a nose
reads at y=0.20 — comfortably inside the frame — on footage cropped at the chin. An ankle below y=1
does fire, so a foot crop is detectable from `motion.json`; a head crop needs eyes on the image. An
instructional video that cuts between wide shots and teaching close-ups will hand you both within
one labelled section, so check the span you actually chose rather than the section it came from.

**The candidate ranking is not `seam_ratio`.** Whatever a search scores, the number that ships comes
from `extract`, over the 24 output frames, in the tool's own normalisation — and the two orderings
disagree, sometimes wildly (one window ranked fourth-best by a search came out of `export` at 8.5x,
and the search's own first choice was worse than its third). So: propose candidates by search, then
**measure** by extracting the top few and keeping whichever the tool scores best. Only worth the
extra extracts on a move that came back bad.

Expect a demo clip to yield weak seams. A "six moves in twenty seconds" video does one pass of each
move and cuts, so for most of them no window in the footage repeats at all and there is nothing to
find. Take the best available, say the ratio in the hand-off, and write the arc so it tells the
artist where to blend.

## Timing: the window length IS the playback length

`frames / fps` is how long the bundle plays. The source span is stretched onto it. So the only way
the drawn motion runs at the speed it was danced is:

    frames = fps * window

Choose the window first, on the dance; derive the frame count from it. A window of 3.67 s at 12 fps
is 44 frames, and 44/12 = 3.67 s, so `speed_factor` comes out 1.000 by construction.

**The failure this replaces cost a full re-cut of every bundle.** A fixed 24 frames at 12 fps was
read as "every move is exactly 2.0 s of source", and the window search was then given a band of
1.6-2.4 s so its results would fit. Measured afterwards against each move's real cycle: 31 of 41
bundles had their true cycle outside that band, 16 played more than 15% fast, the worst at 2.35x,
and others crawled - one at 0.28x. A 4.70 s move was never a candidate because the search could not
see past 2.4 s.

**And the built-in check could not catch it**, which is why it survived review. `speed_factor` is
`span / (frames/fps)`. When the search band is itself pinned to `frames/fps`, that ratio can only
come back near 1.0 — it was restating the constraint, not measuring the dance. It was quoted in
every arc and every hand-off as evidence the timing was right. A check computed from a quantity you
constrained is not a check. Now that the window sets the frame count, the same number is a real
assertion: **any bundle whose `speed_factor` is not 1.00 is a bug.**

`clipper.py` is the one tool here that already works in this order: the marks fix the window before
any count exists, so it derives `capture_frames = capture_fps x window` and writes the resulting
`speed_factor` per clip. A clip that comes back off `1.0` has marks that do not land on a whole
frame at that fps — nudge a mark rather than accepting the rounding. Its Play button previews the
capture itself: the frames `extract` will sample, at the fps they will play, so a rate can be judged
before the clip is captured. Its playback list offers `ping-pong` alongside the three `--playback`
values; that one is this `--pingpong` flag, and a clip that picked it is written as
`"playback": "loop", "pingpong": true` so nothing hands `extract` a word it would reject.

Keep `fps` fixed across a library (12 is CAG's editor default) and let the frame count vary per
move. The marker records fps **per clip**, so this is a convention it will not enforce — a set whose
clips carry different `capture_fps` is a deliberate choice, and worth confirming before capture. Frame count drives cost — CAG renders 8 figures per call — so a 4 s move is 48 frames and 6
calls. That is the honest price of real-time playback; the alternative, a constant frame count, buys
its predictable cost by time-scaling every move that is not exactly `frames/fps` long.

Constrain candidate window lengths to multiples of `4/fps` seconds. The frame count then lands on a
multiple of 4 — a full last render row — with **no rounding of the duration**, so timing stays exact
rather than being traded against a tidy frame count.

### Do not estimate the period

The obvious repair is to measure each move's cycle by self-similarity and use that as the window. It
does not work, because a move that does not repeat has no period and the measurement returns one
anyway. On one non-repeating move the lag curve read 0.272, 0.273, 0.275, 0.276 across every
candidate — flat, noise, and the "deepest" minimum was whichever lag happened to sit lowest. Long
lags are also measured on few overlapping samples, so they win by having less evidence against them.

None of it is needed: `frames = fps * window` is exact whether or not the move repeats. Period
belongs to choosing a good *seam*, and the seam search already does that.

### Take the longest window that still closes

Seam ratio falls with window length — a shorter window simply has less room to diverge — so ranking
on it alone bottoms every move out at the minimum and ships a 1 s scrap of a 3.7 s phrase. Skate's
two mirrored pushes came back as one push. A relative tolerance does not fix it either: when the
best ratio is 0.36, a 25% band is 0.50 and still excludes every longer window.

Use the tool's own verdict as the floor. Under 1.5x is "clean", so **take the longest window whose
ratio is under `max(best * 1.25 + 0.05, 1.5)`**. Content beats a tighter seam once the seam is clean.

## Search at the source's own frame rate

`--search` and the `--margin` snap both walk the source at **its native frame rate**, one sample per
real frame, and every candidate time they report is a real frame timestamp.

A loop is cut at a frame. Scoring on a coarser grid can only ever land the seam on a sampled
instant, so the best cut the video can actually be made at is missed by up to half a hop — and half
a hop on the old 8 Hz grid is ~60 ms, which is a lot of pose in a dance step. `sample_rate()` is
where the default lives; it also refuses to sample faster than the source, which would only score
the same frame twice.

Sampling is one seek then a sequential decode, not a seek per sample: at native rate seeking per
frame is slower, and on an inter-frame codec it does not reliably return the frame asked for.

It costs real time — a native-rate search over a whole video is several times the work of a coarse
one. Trim the range with `--start/--end` before searching a long video rather than trading the rate
back down.

### `--search` ranks the absolute gap, not the ratio the verdict uses

The search reports `seam` as an absolute landmark distance and prefers the smallest, biasing it
toward the **stillest** windows — it prints `energy` beside each candidate, and the winners are the
low-energy ones. But the verdict that decides "clean" is `seam_ratio` = gap ÷ median step, and
stillness shrinks that denominator. The two objectives pull apart on a slow move.

Measured on one 12.5-minute source, searching 143–149s at three window lengths: the winners came
back at ratios 12.06, 3.68 and 5.65, against 2.21 for a cut chosen by hand from the same range. All
three searches made the seam worse, and the worst was the one the search liked best. So on a calm
move, do not use `--search` to repair a flagged seam — score candidates on `seam_ratio` directly.

The same scale-relativity makes `seam_ratio` **incomparable between moves**. A busy phrase measured
3.29x on a gap of 0.129; a calm one measured 9.17x on a gap of 0.081 — the second number is worse
while the second gap is smaller. Compare ratios only between cuts of the same move.

### A move that travels does not loop, and no cut will make it

Before hunting a seam, check whether the phrase returns to its own start: measure each frame's
landmark distance back to frame 0. A loop dips near zero somewhere; a one-shot climbs away and ends
at its maximum. One clip here rose steadily to the final frame, so every candidate end was the worst
available, the best cut in a 6-second range still scored 2.21, and cutting to reach it cost a third
of the move. That is the footage, not the cutting. When the curve says travel, either ship it as
`--playback one-shot`, where the seam stops mattering at all, or pick a different move.

Estimating a re-cut's seam from the current capture's frames — distance from each candidate last
frame back to frame 0 — is worth doing, but it only holds when the new span's effective rate is near
the capture's. It predicted 1.12 and the re-extract returned exactly 1.12 on a span that stayed at
12fps; the same method missed by 1.1 on a 20-frame span over 1.766s, which is 11.3fps. Treat it as
a shortlist, then re-extract the winner and read the real number.

## Naming: every capture is `<set>-<index>`

**A video URL always arrives with a set name.** If one is missing, ask for it — do not invent a
label from the video title, and do not fall back to the old descriptive slugs.

One video is one set. Extract as many *unique* moves from it as it holds, and name them
`<set>-1`, `<set>-2`, `<set>-3` … — index from 1, incrementing per unique move, in the order you
cut them. `hip-hop-1` as a set name yields `hip-hop-1-1`, `hip-hop-1-2` and so on; the set's own
trailing digit is part of the set, not a move index. Unique means a different move, not a different
cut of the same one: a re-cut of move 3 stays `<set>-3` and replaces it.

Everything follows the name. The capture directory is `work/<set>-<index>/`, the bundle inside the
zip is `<set>-<index>/`, and the zip lands in **`exports/<set>/`** — one directory per source video,
never flat. `set_name()` derives the set by stripping the trailing `-<index>`, so nothing needs a
second flag.

This replaces the old `<label>-<video id>-<start>s` trace name, which existed because a descriptive
label was not an identifier — "shuffle" named two different dances within an hour. An assigned
`<set>-<index>` *is* an identifier, so the provenance no longer has to ride in the file name: url,
start second and span live in `manifest.json`, which is where a consumer reads them anyway. The
trade is deliberate — a re-cut now **overwrites** its move instead of landing beside it as a second
zip, which is what the hand-off wanted all along.

**Everything under `exports/` from before this rule is deprecated and has been removed.** Do not
resurrect one, and do not name a new capture after one.

## Frame count: any length, but prefer a multiple of 8

One motion is **one capture and one bundle**, whatever its length. Do not split a long motion into
several — CAG has no concept of chaining bundles, so two bundles are two unrelated animations there,
each with its own frame sheet, its own proof and its own registration scale, and the loop relation
between them is lost.

The number 8 belongs to CAG's renderer, not to the bundle: it draws 8 figures per image on a grid
four wide, and chunks a longer motion across as many renders as it needs before assembling
one sheet. A 24-frame bundle renders as 8 + 8 + 8, a 16 as 8 + 8, a 14 as 8 + 6 — all one animation
out the other side. That ceiling is not reachable from here: `frame_sheet()` chunks `motion.frames`
by `FRAME_SHEET_SIZE` unconditionally and no spec field raises it, so nothing this skill ships can
put more than 8 figures in one render whatever `frame_count` says. (A scratch script calling
`draw()` directly can, and does tile one pose across the last row past the ceiling — but that path
is below CAG.)

8 is a chosen size, not the ceiling. That repo measured the ladder 8/12/16/24/32 on one 4-column
grid: at 16 the generator stops reading distinct poses and tiles one across the final row (figures
12-15 came back at silhouette IoU 0.88-0.95, the same pose four times), and at 24 and 32 it will
not draw the count asked for at all — 20 and 28 — which makes every per-frame measurement
meaningless, because figure `n` stops being frame `n`. 8 and 12 both came back clean. So 12 is the
hard ceiling and 8 is where they settled inside it.

It was 12 until today, under the older name `SHEET_FRAMES`, and both the rename and the drop to 8
landed on that repo's main together — so a grep for the old constant finds nothing and a stale
checkout reads 12 with no sign it ever moved. Check `cag/animation.py` on *their* main, not a
branch, before trusting a number here. The drop is a resolution argument with one roll behind it:
at 12 a photograph lands about 344px into a cell that holds about 390px and is upscaled, at 8 it
lands about 476px and is downsampled. On the same footage, costume bleed went from 11 of 24 figures
in the dancer's white socks — plus a whole sheet in denim shorts — to 0 of 24. One roll each way,
on a measure that had read zero at 12 on an earlier roll, so treat the direction as supported by
the resolution argument rather than proven by the count.

**Each chunk is a separate generation call, and every boundary is a chance for the character to
drift** — costume, face, proportions. The approved key art is attached to every render, which is
real mitigation and is also exactly what failed to stop a dancer's trainers arriving on a lifted
foot. Nothing measures identity across chunks and nothing warns. So prefer a count that divides by
8: it costs the fewest calls. 16 is two; 24 is three; 26 is four, one of them drawing two figures.

Failing that, prefer a multiple of 4, so the last row of the last chunk fills rather than sitting
part-empty (a chunk of 8 is two full rows of four). That is wasted cells, not a failure, and `extract` says so as a note. Nothing breaks
at 14.

What does **not** need managing from here is drawn size. The generator draws figures bigger in a
sparse render than a full one, so an 8 + 2 chunking draws its last two much larger — but
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

**Take that warning as a blocker, not a note.** CAG's `read_bundle` takes the photographs only when
`len(thumbs) == frame_count`; on any mismatch it sets `photos = ()` and draws the whole set with no
pose reference at all. Deliberately — a partial set would pair figure `n` with the wrong frame —
but it is silent, so one dropped thumb does not read downstream as a missing input. It reads as a
weak render, and only an amplitude measurement tells the difference. Never hand off a bundle whose
thumb count disagrees with its frame count, and say so in the hand-off if one ever cannot be made.

Thumbs are written 200px wide at JPEG quality **88**. Both numbers are deliberate. 200px is the
width every amplitude measurement above was taken at, and CAG letterboxes each thumb onto a 384×512
card at native size, so it is demonstrably enough; 384 wide would fill the card exactly, but it is
not the measured condition, so it needs a measurement before it is worth taking. The quality came up
from 60 because JPEG artefacts at 60 sit on the limb edges, which is precisely what the generator is
reading off them. That one is reasoning about the failure mode, not a measured delta.

**That width is a measurement of portrait footage, and it does not survive a landscape source.**
The thumb is the whole frame scaled to 200px wide, aspect kept, so what 200px buys depends entirely
on the frame's shape. A 720×1280 Short becomes a 200×355 thumb carrying a figure around 250px tall —
the condition every number above was measured in. A 1280×720 source becomes a 200×112 thumb carrying
a figure **66px tall**, a third of it, and the dancer is then smaller than the costume detail the
generator reads off limb edges. Measured on one clip of each: 262px against 66px, same code.

A better download does not help. The 200px cap is applied after the fetch, so a 4K landscape source
still yields 200×112. The fix has to happen in the pixels, before `extract`:

```bash
python3 motion-artist/scripts/portrait_crop.py VIDEO --start S --end S
```

It traces the performer across the span, takes the union of their landmark box padded by a share of
figure height, grows that to 9:16 about its centre, clamps it inside the frame, and writes one
ffmpeg crop at native resolution. Run `extract` against the cropped file. Crop the **video**, never
the thumbs: `extract` re-traces the cropped frames, so `pts`, the thumbs and the figure all end up
in one coordinate space, and nothing downstream has to be told the crop happened. Cropping thumbs
after the fact would decouple them from `pts`, which CAG measures the drawn figure against.

The crop is fixed for the whole file and computed from one span, so it preserves every timestamp —
marks in `clips.json` and `--start`/`--end` keep their meaning — but a different move from the same
video may sit elsewhere in frame and needs its own crop. Check `missing_frames` after: a limb that
leaves a tight crop drops a trace, and by the rule above that costs the pose reference entirely.

Known failure mode of the photo route: photographs bleed the **dancer's costume** into the character
(a brown boot came back as the dancer's white sneaker on a lifted foot, 2 figures in 16). It is fixed
on the CAG side with a positive costume sentence — nothing to do here. Worth carrying the general
lesson though: telling the model to *ignore* the clothing did not work. Negation is weak; naming the
right thing positively is what held. That applies to any prompt text this skill generates.

## `--exaggerate` cannot repair downstream compression

When a render comes back smaller than the reference, raising `--exaggerate` is the obvious reach and
the wrong one. It amplifies each landmark's deviation from the **clip-mean** pose, so its effect
scales with how far a pose already sits from the mean — which is backwards from what compression
needs. Going 1.25 → 1.6 on one capture widened the frames that had come back *correct* by 2.1x and
the wide straddle that had actually compressed by 1.19x. The frame needing help moved least.

It is also not free. Amplifying pushes borderline frames across classification boundaries: the same
1.25 → 1.6 turned a grounded frame airborne on an ankle that rose about 1%, inventing a hop that is
not in the footage. `features.airborne` physically lifts the character off the contact row
downstream, so that is a visible defect, and it arrives without anyone touching the threshold —
raising `--exaggerate` loosens it as a side effect. Re-read `missing_frames` and the airborne list
after any change to it, and compare against the previous capture rather than reading the new one
alone.

If the reference is right and the render is small, the gap is downstream. Say so rather than
over-driving the source to compensate.

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
   to a sheet**. The twelve is this tool's own number now: it mirrored CAG's render grid when that
   grid was twelve, and CAG has since dropped to eight *and* stopped reading the grid at all, so one
   image is no longer one of its generation calls. Nothing downstream depends on the twelve —
   `--cols` and the sidecar's `per_sheet` state whatever it actually is — but do not cite it as
   CAG's batch size. Writes
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
   Writes `exports/hip-hop-1/hip-hop-1-3-24f-4fps-motion-source.zip` — always `exports/<set>/` at
   the repo root, never inside
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

Copy `exports/<set>/<set>-<index>-<frames>f-<fps>fps-motion-source.zip` into that repo's ignored
`work/<character>/motion-source/` and reference it by path and by the SHA-256 the export printed,
as the authorized motion source in the motion-director job input. A spec names **one** bundle per
animation (`spec.motions[set_name]` → one bundle directory); CAG chunks it across renders itself.
A bundle is named by its set and move index, so a re-cut of the same move replaces it in place and
no stale twin is left behind to be referenced — but the **SHA-256 changes**, so re-reference it in
the job input rather than assuming the old digest still describes the file at that path.
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
  a full render or a sparse one, from photographs or skeletons, across three prompt rewordings. Do not spend
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
