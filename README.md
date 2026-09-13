# MotionArtist

A Claude Code skill that turns a video of a person moving into a **motion sheet** for animation
agents: skeletal stick-figure frames at a declared fps and frame count, each with a pose
instruction written in the vocabulary used by
[KaraokeParty-Graphics](https://github.com/matt-antone/KaraokeParty-Graphics) motion and
keyframe roles (character-left / character-right, keys, pilots, loop seam).

```
motion-artist/
  SKILL.md                 # the skill (what Claude does, step by step)
  scripts/motion_artist.py # extract (video → motion.json + thumbs) and render (motion.json → HTML)
```

## Install

```bash
pip install "mediapipe<1" opencv-python   # plus yt-dlp on PATH
ln -s "$PWD/motion-artist" ~/.claude/skills/motion-artist   # or copy into a repo's .claude/skills or .agents/skills
```

## Use

```bash
python3 motion-artist/scripts/motion_artist.py extract "https://www.youtube.com/shorts/…" \
  --fps 4 --frames 16 --start 0:16 --end 0:20 --name dance
python3 motion-artist/scripts/motion_artist.py render work/dance/motion.json
```

Trim with `--start` / `--end` (seconds or `m:ss`). Between the two commands, fill `arc` and any
per-frame `note` in `motion.json`; the sheet renders them. See `motion-artist/SKILL.md`.

Outputs live under `work/` (git-ignored, as are downloaded videos).
