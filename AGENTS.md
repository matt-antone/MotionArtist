# Working in this repo

All agents must use caveman skill all the time.

This repo is one skill and one script. `motion-artist/SKILL.md` tells an agent how to turn a video
of a person moving into a motion source; `motion-artist/scripts/motion_artist.py` does the work.
Everything else is docs and ignored scratch.

```
motion-artist/SKILL.md                 the skill an agent loads
motion-artist/scripts/motion_artist.py extract | render | pose-grid | export | selftest  (single file, ~1030 lines)
motion-artist/templates/sheet.html     the motion sheet: markup, CSS and player, with {{PLACEHOLDER}}s
                                       render() fills. Edit the sheet's design here, not in the script
.claude/skills/motion-artist           symlink to motion-artist/, so the skill loads in this repo —
                                       git-ignored, so a fresh clone has to create it (see Setup)
work/                                  captures and downloaded video — git-ignored, never commit
exports/<set>/                         finished bundles, one zip per capture — committed
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
directory per source video, as `<set>-<index>-<frames>f-<fps>fps-motion-source.zip`.

An assigned name is an identifier in a way a descriptive label never was — "shuffle" is a genre,
and two different dances landed on it once and one silently overwrote the other. Provenance (url,
start second, span) rides in `manifest.json` rather than in the file name. Everything exported
under the older `<name>-<video id>-<start>s` scheme is deprecated and removed.
Hand off that zip as it is. Do not unpack it, and do not copy loose `motion.json`/`thumbs/` into a
consuming repo — the zip is the unit, and its `manifest.json` is what verifies it.
A re-cut of a move keeps its `<set>-<index>` and replaces the bundle in place, so no stale twin is
left to be referenced — but the SHA-256 changes, so re-reference it in any job input that names it.

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
- Never commit anything under `work/`, any `.mp4`, or a capture bundle.

## graft skill

This repo is indexed by graft. Use the graft skill for codebase context — `graft_find_code`, `graft_find_all`, `graft_trace_calls`, `graft_file_api`, `graft_repo_map` — before grepping or reading source files. Never commit graft caches.
