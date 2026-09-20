# Working in this repo

This repo is one skill and one script. `motion-artist/SKILL.md` tells an agent how to turn a video
of a person moving into a motion source; `motion-artist/scripts/motion_artist.py` does the work.
Everything else is docs and ignored scratch.

```
motion-artist/SKILL.md                 the skill an agent loads
motion-artist/scripts/motion_artist.py extract | render | export | selftest  (single file, ~680 lines)
.claude/skills/motion-artist           symlink to motion-artist/, so the skill loads in this repo —
                                       git-ignored, so a fresh clone has to create it (see Setup)
work/                                  captures and downloaded video — git-ignored, never commit
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

Four steps, in order. A capture is not finished until step 4.

1. `extract` — video → `work/<name>/motion.json` + `thumbs/`, and a printed frame table.
2. Author the arc — fill `arc`, and per-frame `note` only where the generated cue misses intent.
3. `render` — `motion.json` → the self-contained HTML motion sheet.
4. `export` — zip the json, sheet, thumbs and a generated `manifest.json` with a SHA-256 per file.
   The printed bundle path and zip digest are what a KaraokeParty-Graphics job input references.

Read the printed table, not `motion.json` — the JSON is large and mostly landmarks. Never hand-edit
`cue`, `role`, `pts` or `t`; they are extractor output. Re-run `extract` instead.

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
