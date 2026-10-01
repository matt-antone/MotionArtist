#!/usr/bin/env python3
"""motion_artist: turn a video of a person moving into a frame-by-frame motion source.

  motion_artist.py extract URL|FILE --fps N --frames N [--start S] [--end S] [--name SLUG]
                   [--playback loop|one-shot|final-hold] [--pingpong] [--out DIR]
  motion_artist.py render DIR/motion.json [--out FILE.html] [--template FILE]
  motion_artist.py export DIR/motion.json [--out DIR] [--sheet FILE.html]
  motion_artist.py selftest
  motion_artist.py generate PROMPT --title TITLE --out DIR [--image FILE] [--duration SECONDS]

`extract` writes DIR/motion.json (+ DIR/thumbs/*.jpg) and prints a compact frame table.
`render` turns motion.json into a self-contained HTML motion sheet.
`export` bundles the json, sheet and thumbs with a SHA-256 manifest for hand-off.
Between extract and render, an agent may fill `arc`, `title` and per-frame `note` in motion.json.
"""
import argparse, base64, fcntl, glob, hashlib, html, json, math, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task")
MODEL_PATH = os.path.expanduser("~/.cache/motion-artist/pose_landmarker_lite.task")

# Captures, marks and bundles are stored on Drive; generated source videos stay local.
# HOME is local disk only because cv2 and ffmpeg need files: work/ holds downloads, split frames
# and each capture as it is made, exports/ each bundle as it is built. Both are a staging copy;
# every command pushes what it wrote as soon as it is written, and fails if the push does. The
# commands run from HOME, so a relative `work/...` or `exports/...` path always means HOME's.
HOME = os.path.expanduser(os.environ.get("MOTION_ARTIST_HOME", "~/.cache/motion-artist"))
REMOTE = os.environ.get("MOTION_ARTIST_REMOTE", "kadrive:MotionArtist").rstrip("/")
# realpath, so a skill linked into ~/.claude/skills still knows which checkout it came from
REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))

# Bump when a field changes meaning or disappears, so a consumer fails loudly instead of mis-parsing.
SCHEMA = "motion-artist/2"
GENDERS = ("male", "female", "any")


def motion_gender(value, context="motion"):
    """User-assigned character compatibility; unknown is distinct from explicitly any."""
    if value is not None and value not in GENDERS:
        raise ValueError(f"{context}: invalid gender {value!r}; expected male, female, any or null")
    return value


def marked_capture(capture_dir):
    """Find the saved clip by its capture path, without allocating a motion number."""
    path = os.path.join(os.path.dirname(os.path.abspath(capture_dir)), "clips.json")
    root = work_root(capture_dir)
    if not root or not os.path.exists(path): return path, None, None
    doc = json.load(open(path))
    rel = os.path.relpath(os.path.abspath(capture_dir), root).replace(os.sep, "/")
    clip = next((c for c in doc.get("clips", []) if c.get("capture") == rel), None)
    return path, doc, clip


def capture_gender(capture_dir, requested=None):
    """Explicit flag, then marked classification, then a previous capture, then unknown."""
    if requested is not None:
        return motion_gender(None if requested == "unclassified" else requested, capture_dir)
    path, _, clip = marked_capture(capture_dir)
    if clip is not None and "gender" in clip:
        return motion_gender(clip["gender"], path)
    previous = os.path.join(capture_dir, "motion.json")
    old = json.load(open(previous)) if os.path.exists(previous) else {}
    # A deleted clip can leave another motion in this capture directory; never inherit its tag.
    if clip is not None and (not clip.get("motion") or old.get("name") != clip["motion"]): return None
    return motion_gender(old.get("gender"), previous)


def remember_gender(capture_dir, gender):
    """Keep a successful extraction's classification in its marks for the next re-cut."""
    path, doc, clip = marked_capture(capture_dir)
    if clip is not None:
        clip["gender"] = motion_gender(gender, path)
        atomic_json(outside_repo(path), doc)

# MediaPipe pose indices. "L"/"R" are the person's own sides == character-left / character-right.
LM = dict(nose=0, eyeL=2, eyeR=5, earL=7, earR=8, shL=11, shR=12, elL=13, elR=14, wrL=15, wrR=16,
          pinkyL=17, pinkyR=18, indexL=19, indexR=20,
          hipL=23, hipR=24, knL=25, knR=26, anL=27, anR=28, heelL=29, heelR=30, toeL=31, toeR=32)
CORE = ["shL", "shR", "elL", "elR", "wrL", "wrR", "hipL", "hipR", "knL", "knR", "anL", "anR"]
BONES = [("hipL", "knL"), ("knL", "anL"), ("anL", "toeL"), ("hipR", "knR"), ("knR", "anR"), ("anR", "toeR"),
         ("hipL", "hipR"), ("shL", "shR"), ("shL", "elL"), ("elL", "wrL"), ("shR", "elR"), ("elR", "wrR")]


# ---------------------------------------------------------------- geometry helpers
def mid(a, b): return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
def dist(a, b): return math.hypot(a[0] - b[0], a[1] - b[1])


def angle3(a, b, c):
    """Angle at b (degrees) between segments b->a and b->c, any dimension."""
    ba = [x - y for x, y in zip(a, b)]; bc = [x - y for x, y in zip(c, b)]
    n = math.sqrt(sum(x * x for x in ba)) * math.sqrt(sum(x * x for x in bc)) or 1e-9
    return math.degrees(math.acos(max(-1, min(1, sum(x * y for x, y in zip(ba, bc)) / n))))


def bend_word(deg):
    if deg > 150: return "straight"
    if deg > 110: return "slightly bent"
    if deg > 65: return "bent ~90°"
    return "folded tight"


# ---------------------------------------------------------------- feature extraction
def facing(world):
    """Yaw from world shoulder line. >0 => character-left side nearer camera (faces screen-left)."""
    l, r = world["shL"], world["shR"]
    yaw = math.degrees(math.atan2(r[2] - l[2], -(r[0] - l[0])))
    a = abs(yaw)
    if a < 22: view = "front"
    elif a < 68: view = "3/4"
    elif a < 112: view = "left" if yaw > 0 else "right"
    else: view = "back"
    return yaw, view


def describe(P, W, floor_y, body_h):
    """P: image-space 2D points (aspect-corrected), W: world 3D points. Returns (features, cue)."""
    f = {}
    yaw, view = facing(W)
    f["yaw"] = round(yaw); f["view"] = view
    side_near = "character-left" if yaw > 0 else "character-right"
    front_ish = view in ("front", "3/4")
    # screen-left in the image == character-right when facing camera, character-left when back-on.
    def screen_to_char(screen_left):  # returns character side for a screen-left/right displacement
        if view == "back": return "character-left" if screen_left else "character-right"
        return "character-right" if screen_left else "character-left"

    sh_mid, hip_mid = mid(P["shL"], P["shR"]), mid(P["hipL"], P["hipR"])
    sh_w = max(dist(P["shL"], P["shR"]), 0.05 * body_h)

    # depth. World z is metres, negative toward the camera, hips at the origin; it is noisy in
    # absolute terms, so every verdict here is a comparison — against the hip plane, or against the
    # paired limb — with a fraction of the world shoulder width as the "clearly nearer" threshold.
    hip_z = (W["hipL"][2] + W["hipR"][2]) / 2
    z_unit = 0.12 * max(math.dist(W["shL"], W["shR"]), 1e-6)
    def limb_z(*ks): return sum(W[k][2] for k in ks) / len(ks) - hip_z
    zl = dict(legL=limb_z("knL", "anL"), legR=limb_z("knR", "anR"),
              armL=limb_z("elL", "wrL"), armR=limb_z("elR", "wrR"))
    def nearness(dz): return "near" if dz < -z_unit else "far" if dz > z_unit else "level"
    # near_side: the side of the body facing camera, in the performer's own terms. Square to the
    # camera, neither side is nearer — say so rather than rounding noise into a side.
    depth = dict(near_side=("L" if yaw > 8 else "R" if yaw < -8 else None),
                 limbs={k: nearness(v) for k, v in zl.items()})

    # arms
    for s, name in (("L", "character-left"), ("R", "character-right")):
        wr, sh = P["wr" + s], P["sh" + s]
        # "chest/waist" alone spanned the whole torso — 56 of 128 arm-frames across the two
        # reference captures, 22 of them a hand level with the shoulder being called waist-high.
        # Subdivide it by where the wrist sits between shoulder (0) and hip (1); the cuts are the
        # two real gaps in that distribution, and splitting here adds no word-change that happens
        # while the wrist is standing still. The outer bounds are left exactly as they were.
        drop = (wr[1] - sh[1]) / max(hip_mid[1] - sh[1], 1e-6)
        if wr[1] < P["nose"][1] - 0.02 * body_h: h = "overhead"
        elif wr[1] < sh[1] - 0.03 * body_h: h = "raised above shoulder"
        elif wr[1] < hip_mid[1] - 0.03 * body_h:
            h = ("at shoulder height" if drop < 0.16 else
                 "at chest height" if drop < 0.44 else "at waist height")
        else: h = "low by the hip"
        bend = angle3(W["sh" + s], W["el" + s], W["wr" + s])
        # Lateral reach. Height and elbow alone leave the wrist anywhere from across the chest to
        # flung out sideways, and a generator draws only what the words name. Measure the wrist from
        # its OWN shoulder along the outward direction, in shoulder widths, so it survives scale:
        # negative is inward (toward and past the midline), positive is outward. Taking the outward
        # direction from the shoulder itself, rather than from the view, keeps the sign right
        # whichever way the body faces. Image x only, so it needs a camera-ish view like `cross` did.
        out_dir = 1.0 if sh[0] >= sh_mid[0] else -1.0
        reach = (wr[0] - sh[0]) * out_dir / sh_w
        # Cuts are measured, not guessed: over both reference captures (128 arm-frames) the gap at
        # -0.55 is the widest in the whole distribution and falls where the wrist passes the midline,
        # and -0.35 sits in the next gap. +0.50 is the one outward cut that never changed the word
        # while the wrist was standing still — cutting higher (0.91, 1.00) is stabler only because it
        # collapses 97% of frames into one word, which is the failure this term exists to fix.
        # Whether an arm is level with or in front of the torso is a z question and z is the weak
        # axis, so it is named only where `cross` already trusted it: a wrist across the body.
        lateral = ""
        if front_ish:
            if reach < -0.55:
                lateral = ", across the body" + {"near": " in front of the torso",
                                                 "far": " behind the torso"}.get(nearness(zl["arm" + s]), "")
            elif reach < -0.35: lateral = ", inside the shoulder"
            elif reach < 0.50: lateral = ", by the side"
            else: lateral = ", out to the side"
        # the number behind the word, for anyone A/B-ing the cue against the render
        f["reach_" + s] = round(reach, 2)
        f["arm_" + s] = f"{name} arm {h}, elbow {bend_word(bend)}{lateral}"

    # legs / weight / airborne
    anL, anR = P["anL"], P["anR"]
    lift = 0.045 * body_h
    # planted is a SOLE question, not an ankle one: on the balls of the feet the ankle sits well
    # above its flat-footed height while the toe is still on the ground, and an ankle-only test
    # reads that as airborne. Use whichever of heel/toe sits lower (larger y = closer to the floor).
    soleL, soleR = max(P["heelL"][1], P["toeL"][1]), max(P["heelR"][1], P["toeR"][1])
    plantedL, plantedR = soleL > floor_y - lift, soleR > floor_y - lift
    kneeL, kneeR = angle3(W["hipL"], W["knL"], W["anL"]), angle3(W["hipR"], W["knR"], W["anR"])
    f["knee_L"], f["knee_R"] = round(kneeL), round(kneeR)
    # Airborne is a far stronger claim than "this heel is up", so it does not reuse `lift`, which is
    # tuned for heel-lift and sits inside the frame-to-frame noise of a hip-normalised sole. cag
    # agrees from the other side: it refuses to lift a grounded frame because soles "sit a few
    # percent off floor_y by noise, and that would come out as jitter" (cag/mask.py placement) — and
    # a false airborne there lifts the character clean off the contact row. Measured on the shuffle,
    # the two classes are far apart: soles genuinely on the ground clear the floor by up to 0.075
    # body heights, a genuinely lifted foot by 0.30-0.59. Nothing lands in between, so the cut sits
    # in that empty band instead of hard against the noise.
    off = 0.15 * body_h
    f["airborne"] = soleL < floor_y - off and soleR < floor_y - off
    if f["airborne"]:
        f["weight"] = "airborne — both feet off the floor"
    elif plantedL and plantedR:
        # weight leans toward the foot the hip centre sits over
        t = (hip_mid[0] - anL[0]) / ((anR[0] - anL[0]) or 1e-9)
        f["weight"] = ("weight centred over both feet" if 0.35 < t < 0.65 else
                       f"weight over the {'character-right' if t >= 0.65 else 'character-left'} foot, both feet down")
    else:
        up = "character-right" if plantedL else "character-left"
        f["weight"] = f"weight on the {'character-left' if plantedL else 'character-right'} foot, {up} foot lifted"
    # no stance word: ankle spread in image x cannot tell a wide stance from a fore-aft step seen
    # at an angle, and the traced frame already shows foot spacing. Words that disagree with it strobe.
    legs = []
    for s, name in (("L", "character-left"), ("R", "character-right")):
        k = kneeL if s == "L" else kneeR
        if k < 150: legs.append(f"{name} knee {bend_word(k)}")
    f["legs"] = ", ".join(legs) if legs else "legs straight"
    # legs overlapping in the image: a flat photograph cannot show which is nearer, so say it in words
    crossed = (anL[0] - anR[0]) * (P["hipL"][0] - P["hipR"][0]) < 0
    dz = zl["legL"] - zl["legR"]
    f["overlap"] = ""
    # ponytail: "overlapping" == ankles closer than about a thigh width; widen if legs read as apart
    if (crossed or abs(anL[0] - anR[0]) < 0.12 * body_h) and abs(dz) > z_unit:
        back, front = ("character-left", "character-right") if dz > 0 else ("character-right", "character-left")
        f["overlap"] = f"The {back} leg passes behind the {front}"

    # footwork: the pitch of each planted sole. A dancer working on the balls of her feet never
    # puts a heel down, and nothing else in the cue carries that — knee angle and weight say where
    # the leg is, not what the foot under it is doing.
    pitch = {}
    for sfx, nm, planted in (("L", "character-left", plantedL), ("R", "character-right", plantedR)):
        if planted:
            h, b = W["heel" + sfx], W["toe" + sfx]
            pitch[nm] = math.degrees(math.atan2(b[1] - h[1],
                                                math.hypot(b[0] - h[0], b[2] - h[2]) or 1e-9))
    ball = [nm for nm, deg in pitch.items() if deg > 15]
    rock = [nm for nm, deg in pitch.items() if deg < -15]
    feet = ["on the balls of both feet, heels lifted"] if len(ball) == 2 else \
           [f"on the ball of the {nm} foot, heel lifted" for nm in ball]
    feet += ["both heels down, toes up"] if len(rock) == 2 else \
            [f"{nm} heel down, toe up" for nm in rock]
    f["feet"] = ", ".join(feet)

    # torso lean (image plane)
    dx, dy = sh_mid[0] - hip_mid[0], hip_mid[1] - sh_mid[1]
    lean = math.degrees(math.atan2(dx, dy or 1e-9))
    f["lean_deg"] = round(lean)
    if abs(lean) < 8: f["torso"] = "torso upright"
    elif front_ish or view == "back": f["torso"] = f"torso leans {screen_to_char(lean < 0)}"
    else:  # profile: screen-x lean is forward/back relative to the nose direction
        nose_dir = P["nose"][0] - sh_mid[0]
        f["torso"] = "torso leans forward" if nose_dir * dx > 0 else "torso leans back"

    # hip and shoulder line tilt (which side is higher)
    def tilt(a, b, what):
        d = math.degrees(math.atan2(a[1] - b[1], abs(a[0] - b[0]) or 1e-9))  # + => L lower
        if abs(d) < 5 or not front_ish and view != "back": return ""
        return f"{'character-right' if d > 0 else 'character-left'} {what} raised"
    # the pelvis turning against the shoulders is the engine of most dance, and no other cue can
    # carry it: knee and elbow angles say nothing about it, and both lines tilt the same way under
    # a plain lean. Yaw each girdle off its own world z, and report only the difference.
    def girdle_yaw(l, r): return math.degrees(math.atan2(W[r][2] - W[l][2], -(W[r][0] - W[l][0])))
    twist = girdle_yaw("hipL", "hipR") - girdle_yaw("shL", "shR")
    f["twist_deg"] = round(twist)
    f["twist"] = (f"hips turned {'character-left' if twist > 0 else 'character-right'} against the "
                  "shoulders" if abs(twist) >= 10 else "")
    f["hips"] = tilt(P["hipL"], P["hipR"], "hip")
    f["shoulders"] = tilt(P["shL"], P["shR"], "shoulder")

    # head turn
    hx = (P["nose"][0] - sh_mid[0]) / sh_w
    if front_ish and abs(hx) > 0.28: f["head"] = f"head turned {screen_to_char(hx < 0)}"
    elif view == "back": f["head"] = "head away from camera"
    else: f["head"] = "head forward"

    cue = ". ".join(x for x in [
        f["weight"][0].upper() + f["weight"][1:], f["legs"], f["overlap"], f["feet"],
        f["arm_L"], f["arm_R"], f["torso"], f["hips"], f["shoulders"], f["twist"], f["head"]] if x) + "."
    return f, cue, depth


# ---------------------------------------------------------------- extract
def fetch(url, out_dir):
    """Download with yt-dlp, capping the SHORT side at 720px. Returns (path, title).

    The cap is a sort key, not a filter, because `res` is yt-dlp's *smaller*
    dimension and so means the same thing whichever way the video is turned. A
    plain `height<=720` filter reads as 720p only for landscape: a portrait
    Short is 1080x1920, every format above 360x640 fails `height<=720`, and the
    capture silently traces a 360-wide frame. The thumbs are the pose reference
    the generator draws from, so that halves the resolution of the one input
    that matters. Measured on three sources: portrait 360x640 -> 720x1280,
    landscape 1280x720 -> 1280x720 (unchanged, and no run-up to 4K).

    h264 is sorted ahead of the cap: YouTube serves AV1 in mp4 too, and the opencv-python wheel
    cannot decode it ("Failed to get pixel format"), so every seek came back empty and extract
    reported the pose missing in all frames — on vDILnrn1Qnw, the country-01 source.
    """
    os.makedirs(out_dir, exist_ok=True)
    tmpl = os.path.join(out_dir, "source-%(id)s.%(ext)s")
    r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--no-simulate",
                        "-f", "bv*[ext=mp4]/bv*/b", "-S", "vcodec:h264,res:720",
                        "--print", "after_move:%(filepath)s\t%(title)s", "-o", tmpl, url],
                       capture_output=True, text=True, check=True)
    path, title = r.stdout.strip().splitlines()[-1].split("\t", 1)
    return path, title


def landmarker(video=False):
    """IMAGE mode for the trace's random seeks; VIDEO mode, with masks, for the clip's straight run."""
    import mediapipe as mp
    from mediapipe.tasks import python as mpt
    from mediapipe.tasks.python import vision
    if not os.path.exists(MODEL_PATH):
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    opts = vision.PoseLandmarkerOptions(base_options=mpt.BaseOptions(model_asset_path=MODEL_PATH, delegate=mpt.BaseOptions.Delegate.CPU),
                                        running_mode=vision.RunningMode.VIDEO if video else vision.RunningMode.IMAGE,
                                        num_poses=1, output_segmentation_masks=video)
    return mp, vision.PoseLandmarker.create_from_options(opts)


def trace(a):
    """Trace every .png/.jpg in a directory, in name order, into {i: pts} (null where no pose).

    pts is {joint: [x * W/H, y, z]} straight off the image landmarks — the same keys as motion.json
    but none of extract's rescale or exaggeration, so a render and the bundle's thumbs traced with
    this compare like for like. Transparent PNGs are flattened onto white, like the footage.
    """
    import cv2, numpy as np
    mp, lm = landmarker()
    names = sorted(n for n in os.listdir(a.images) if n.lower().endswith((".png", ".jpg", ".jpeg")))
    out = {}
    for i, n in enumerate(names):
        im = cv2.imread(os.path.join(a.images, n), cv2.IMREAD_UNCHANGED)
        if im.ndim == 2: im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
        if im.shape[2] == 4:
            al = im[..., 3:4].astype(np.float32) / 255
            im = (im[..., :3] * al + 255 * (1 - al)).astype(np.uint8)
        H, W = im.shape[:2]
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(im, cv2.COLOR_BGR2RGB)))
        p = res.pose_landmarks[0] if res.pose_landmarks else None
        out[i] = p and {k: [round(p[j].x * W / H, 4), round(p[j].y, 4), round(p[j].z, 4)] for k, j in LM.items()}
    json.dump(out, open(a.out, "w"), indent=1)
    print(f"{a.out}: {sum(v is not None for v in out.values())}/{len(out)} traced")


def outside_repo(p):
    """Refuse an output path inside the checkout: motions are stored on the Drive, never in the repo."""
    rp = os.path.realpath(p)
    if rp == REPO or rp.startswith(REPO + os.sep):
        sys.exit(f"refusing to write {p} inside the repo at {REPO} — outputs are stored on "
                 f"{REMOTE or 'the Drive'}, staged in {HOME}")
    return p


# rclone bounded like CharacterAssetGenerator's publish: a stalled link fails in about a minute,
# and an encrypted config with no password fails at once instead of prompting on a captured terminal.
# Low-level retries are 10, not CAG's 3: rclone's shared client_id answered rateLimitExceeded often
# enough that 3 failed a push which went through untouched a minute later.
RCLONE_FLAGS = ["--checksum", "--retries", "1", "--low-level-retries", "10",
                "--contimeout", "15s", "--timeout", "60s", "--ask-password=false"]


def publish(local, rel):
    """Copy `local` to REMOTE/rel. Returns None when it landed (or REMOTE is off), else what failed.

    A directory is `rclone sync`ed, scoped to that one capture or bundle, so a re-cut to fewer
    frames removes the stale thumbs on the Drive exactly as it does on disk. A file is `copyto`.
    """
    if not REMOTE: return None
    if not shutil.which("rclone"): return "rclone is not on PATH"
    dst = REMOTE + ("" if REMOTE.endswith(":") else "/") + rel
    cmd = ["rclone", "sync" if os.path.isdir(local) else "copyto", *RCLONE_FLAGS, local, dst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return f"`{' '.join(cmd)}` timed out"
    # rclone prints NOTICE lines even on success; the error is the rest
    why = [l for l in r.stderr.splitlines() if "NOTICE" not in l]
    return None if r.returncode == 0 else f"`{' '.join(cmd)}` failed: {(why or ['exit %d' % r.returncode])[-1]}"


def stored(errs, local):
    """Fail the command when anything did not reach the Drive: an output not stored is not done."""
    errs = [e for e in errs if e]
    if errs:
        sys.exit("not stored on the Drive — " + "; ".join(errs) +
                 f"\nThe local copy is at {local}; fix rclone and re-run.")


def store_capture(cap_dir, *outs):
    """Push a capture to REMOTE/captures/<video>/<clip>/, with its video's clips.json beside it.

    Only the capture directory goes up — never the downloaded video or the split frames beside it
    in work/<video>/, which are inputs. An output written outside the capture goes up beside it.
    """
    cap_dir = os.path.abspath(cap_dir)
    work = os.path.join(os.path.abspath(HOME), "work") + os.sep
    rel = cap_dir[len(work):].replace(os.sep, "/") if cap_dir.startswith(work) else os.path.basename(cap_dir)
    errs = [publish(cap_dir, f"captures/{rel}")]
    clips = os.path.join(os.path.dirname(cap_dir), "clips.json")
    if "/" in rel and os.path.exists(clips):
        errs.append(publish(clips, f"captures/{os.path.dirname(rel)}/clips.json"))
    errs += [publish(p, f"captures/{os.path.dirname(rel) + '/' if '/' in rel else ''}{os.path.basename(p)}")
             for p in outs if not os.path.abspath(p).startswith(cap_dir + os.sep)]
    stored(errs, cap_dir)
    if REMOTE: print(f"stored {REMOTE}/captures/{rel}")


def remote_motions(genre):
    """Motion numbers already stored on the Drive under this genre, so a wiped cache cannot reuse one.

    A failed listing stops the allocation rather than guessing: a reused number would sync over a
    stored bundle, and the Drive copy is the only one.
    """
    if not REMOTE: return set()
    r = subprocess.run(["rclone", "lsf", "--dirs-only", *RCLONE_FLAGS[1:], f"{REMOTE}/{genre}"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode == 3: return set()   # rclone's "directory not found": the genre's first motion
    if r.returncode: sys.exit(f"--genre could not list {REMOTE}/{genre} to allocate a number: "
                              f"{r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
    return {n for d in r.stdout.split() if (n := motion_num(d.rstrip("/"), genre)) is not None}


def portable(p):
    """A bundle is handed to other machines: never bake an absolute home path into it."""
    ap = os.path.abspath(p)
    return os.path.relpath(ap) if ap.startswith(os.getcwd() + os.sep) else os.path.basename(p)


# CAG letterboxes each thumb onto a 384x512 portrait card, top-aligned and never enlarged
# (cag/poses.py CARD_WIDTH/CARD_HEIGHT). The old thumb was the whole 16:9 frame at 200px wide,
# which put a ~50px dancer in the top strip of that card. So ship the card itself.
THUMB_W, THUMB_H = 384, 512
THUMB_MARGIN = 0.12  # pad past the landmarks (eyes, ankles) as a share of figure height


def fit_aspect(x0, y0, x1, y1, aspect, W, H):
    """Grow the box to `aspect` (w/h) about its centre, then fit it inside WxH.

    Growing can push the box off frame, and clamping it back can break the aspect, so
    the box is shrunk to what the frame can hold before it is re-centred. Returns even ints.
    """
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = x1 - x0, y1 - y0
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    # The frame is the ceiling. Shrink to fit before positioning, keeping the aspect.
    if w > W:
        w, h = W, W / aspect
    if h > H:
        w, h = H * aspect, H
    x = min(max(cx - w / 2, 0), W - w)
    y = min(max(cy - h / 2, 0), H - h)
    return (int(x) // 2 * 2, int(y) // 2 * 2, int(w) // 2 * 2, int(h) // 2 * 2)


def write_thumbs(shots, tdir, Wpx, Hpx):
    """One 3:4 crop around the performer for the whole capture, each frame cut to it at 384x512.

    One box, not one per frame: a per-frame fit would flatten travel and scale changes.
    Grow the padded landmark bounds to 3:4 when they fit. Otherwise preserve the full source
    rectangle and letterbox every pose card with the same scale and padding.
    """
    import cv2
    xs = [x for _, _, pts in shots for x, _ in pts]
    ys = [y for _, _, pts in shots for _, y in pts]
    pad = (max(ys) - min(ys)) * THUMB_MARGIN
    box = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    x, y, w, h = fit_aspect(*box, THUMB_W / THUMB_H, Wpx, Hpx)
    # A tall figure may not fit a 3:4 crop inside portrait footage. Preserve the full
    # source rectangle and letterbox the pose card instead of trimming head or feet.
    wanted = (max(0, box[0]), max(0, box[1]), min(Wpx, box[2]), min(Hpx, box[3]))
    if x > wanted[0] + 2 or y > wanted[1] + 2 or x + w < wanted[2] - 2 or y + h < wanted[3] - 2:
        x, y, w, h = 0, 0, int(Wpx), int(Hpx)
    # A padded box past the frame edge means the performer runs off it: head or feet may be cut.
    cut = [s for s, over in (("top", box[1] < 0), ("bottom", box[3] > Hpx),
                             ("left", box[0] < 0), ("right", box[2] > Wpx)) if over]
    print(f"thumbs: crop {w}x{h} at +{x}+{y} of {Wpx:.0f}x{Hpx:.0f}"
          f"{' (upscaled)' if w < THUMB_W else ''}{' | figure near ' + '/'.join(cut) + ' edge' if cut else ''}")
    for f in os.listdir(tdir):  # a re-cut with fewer frames must not leave stale thumbs behind
        if f.endswith(".jpg"): os.remove(os.path.join(tdir, f))
    for i, bgr, _ in shots:
        crop = bgr[y:y + h, x:x + w]
        scale = min(THUMB_W / w, THUMB_H / h)
        tw, th = max(1, round(w * scale)), max(1, round(h * scale))
        resized = cv2.resize(crop, (tw, th),
                             interpolation=cv2.INTER_AREA if scale <= 1 else cv2.INTER_CUBIC)
        left, top = (THUMB_W - tw) // 2, (THUMB_H - th) // 2
        th = cv2.copyMakeBorder(resized, top, THUMB_H - th - top,
                                left, THUMB_W - tw - left, cv2.BORDER_CONSTANT, value=(28, 19, 20))
        # quality 60 put JPEG artefacts on the limb edges, which is the one thing CAG's generator
        # reads off these: they are its pose reference, not a preview.
        cv2.imwrite(os.path.join(tdir, f"f{i:02d}.jpg"), th, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return x, y, w, h


# Source seconds the clip carries either side of the traced span: slack for a consumer that
# re-times the move, and the same pad cag's own backfill cut, so both clips cover one window.
CLIP_PAD = 0.5
# Pose landmarks 0..10: nose, eyes (inner, centre, outer), ears, mouth corners.
FACE = range(11)


def clip_window(start, end, fps, n):
    """First and last source frame of the clip: the traced span plus CLIP_PAD, inside the video."""
    return max(0, round((start - CLIP_PAD) * fps)), min(n - 1, round((end + CLIP_PAD) * fps))


def head_box(lms, W, H):
    """[x0, y0, x1, y1] in clip pixels around the head, or None when too little of the face shows.

    # ponytail: an estimate from the pose model's face points, sized for a blur, not a head
    # outline. The points sit in the lower middle of the head, so the box is 2.4x their spread and
    # reaches further up (the crown) than down: 1.8x, drawn over club-01, stopped at the hairline
    # and was narrower than the head. A profile shows one ear, so its spread is about half a head
    # and the box comes out tighter; a face landmarker would measure it properly.
    """
    pts = [(lms[j].x * W, lms[j].y * H) for j in FACE if (lms[j].visibility or 0) > 0.5]
    if len(pts) < 3: return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    side = 2.4 * max(max(xs) - min(xs), max(ys) - min(ys))
    box = [max(0, cx - side / 2), max(0, cy - 0.6 * side), min(W, cx + side / 2), min(H, cy + 0.4 * side)]
    return [round(v) for v in box] if box[2] > box[0] and box[3] > box[1] else None


def encoder(path, W, H, rate, pix_fmt, quality):
    """An ffmpeg process taking raw frames on stdin; yuv420p wants even sides, so pad, never scale."""
    return subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix_fmt,
                             "-s", f"{W}x{H}", "-r", rate, "-i", "-", "-an",
                             "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", *quality,
                             "-pix_fmt", "yuv420p", "-fps_mode", "cfr", path], stdin=subprocess.PIPE)


def source_rate(src):
    """Native rational fps; JSON keeps stream side data out of the rate value."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=r_frame_rate", "-of", "json", src],
                       capture_output=True, text=True, check=True)
    streams = json.loads(r.stdout).get("streams", [])
    if not streams: raise ValueError("file has no video stream")
    num, _, den = streams[0].get("r_frame_rate", "0/1").partition("/")
    try: num, den = int(num), int(den or 1)
    except ValueError: raise ValueError("video has no usable frame rate") from None
    if num <= 0 or den <= 0: raise ValueError("video has no usable frame rate")
    return f"{num}/{den}"


def cut_clip(src, c0, c1, path, box):
    """Write source frames c0..c1 to `path` as h264 at the native rate, and return its `clip` block.

    The frames are read with cv2, the decoder the trace used, and piped to ffmpeg, rather than
    cut by ffmpeg from the file: then clip frame k is source frame c0 + k by construction, and a
    traced frame's `clip_frame` is exact instead of a second decoder's idea of the same index.

    The same pass writes the performer mask (`mask.mp4`) and a head box per frame (`heads.json`)
    beside it, frame for frame, from one VIDEO-mode landmarker run: cag masks and blurs the
    drive video from them instead of running SAM3 and guessing the head from the silhouette.
    `box` is the thumbs' crop, which cag centres its drive crop on.
    """
    import cv2
    rate = source_rate(src)
    num, den = (int(x) for x in rate.split("/"))
    cap = cv2.VideoCapture(src)
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, c0)
    d = os.path.dirname(path)
    mask_p, heads_p = os.path.join(d, "mask.mp4"), os.path.join(d, "heads.json")
    enc = encoder(path, W, H, rate, "bgr24", ["-crf", "18"])
    # qp 0 is lossless, so the mask stays two-valued instead of growing grey codec fringes
    menc = encoder(mask_p, W, H, rate, "gray", ["-qp", "0"])
    mp, lm = landmarker(video=True)
    n, heads, empty = 0, [], 0
    for _ in range(c0, c1 + 1):
        ok, bgr = cap.read()
        if not ok: break
        enc.stdin.write(bgr.tobytes())
        res = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)),
                                  round(n * 1000 * den / num))
        # ponytail: the pose model's own mask, thresholded at 0.5 — soft at hair and fingers. Against
        # SAM3 on country-01 (a cluttered shop aisle) it scored IoU 0.93 min / 0.95 median.
        m = (res.segmentation_masks[0].numpy_view().reshape(H, W) > 0.5) if res.segmentation_masks else None
        menc.stdin.write((m.astype("uint8") * 255).tobytes() if m is not None else bytes(W * H))
        empty += m is None
        heads.append(head_box(res.pose_landmarks[0], W, H) if res.pose_landmarks else None)
        n += 1
    for e in (enc, menc): e.stdin.close()
    if enc.wait() or menc.wait(): sys.exit(f"extract: ffmpeg failed writing {path} or {mask_p}")
    json.dump(heads, open(heads_p, "w"))
    if n != c1 - c0 + 1:
        print(f"warning: clip holds {n} frames, expected {c1 - c0 + 1} — the source ended early")
    if empty or heads.count(None):
        print(f"note: clip has {empty} frame(s) with no mask and {heads.count(None)} with no head box")
    return dict(file="clip.mp4", start=round(c0 * den / num, 6), fps=num / den, frame_count=n,
                size=[W + W % 2, H + H % 2], box=list(box), t_offset=0.0, sha256=sha256(path),
                mask=dict(file="mask.mp4", sha256=sha256(mask_p)),
                heads=dict(file="heads.json", sha256=sha256(heads_p)))


def local_source(path, title=None):
    """Content identity and verified generation provenance, independent of a local filename."""
    digest = sha256(path)
    gp = os.path.join(os.path.dirname(os.path.abspath(path)), "generation.json")
    generation = json.load(open(gp)) if os.path.isfile(gp) else None
    if generation and (generation.get("status") != "complete" or generation.get("source_sha256") != digest):
        raise ValueError("generation.json does not verify this source video")
    return dict(source_kind="ai-generated" if generation else "local", identity="sha256:" + digest,
                source_sha256=digest, title=title or (generation or {}).get("settings", {}).get("title")
                or os.path.splitext(os.path.basename(path))[0],
                **({"generation": generation} if generation else {}))


def source_metadata(path, url=None):
    mp = os.path.join(os.path.dirname(os.path.abspath(path)), "meta.json")
    meta = json.load(open(mp)) if os.path.isfile(mp) else {}
    kind = meta.get("source_kind", "youtube" if meta.get("id") else "")
    if kind in ("local", "ai-generated"):
        verified = local_source(path, meta.get("title")) if kind == "local" else {}
        digest = sha256(path)
        if meta.get("identity") != "sha256:" + digest or meta.get("source_sha256") != digest:
            raise ValueError("source video changed since its frames were marked")
        generation = meta.get("generation")
        if kind == "ai-generated" and (not generation or generation.get("status") != "complete"
                                        or generation.get("source_sha256") != digest):
            raise ValueError("imported generation provenance does not verify this source video")
        return {**verified, **{k: meta[k] for k in ("source_kind", "identity", "source_sha256", "title", "generation") if k in meta}, "url": ""}
    if url or meta.get("url"):
        return dict(source_kind="youtube", url=url or meta["url"], title=meta.get("title") or os.path.basename(path),
                    **({"identity": "youtube:" + meta["id"], "video_id": meta["id"]} if meta.get("id") else {}))
    return {**local_source(path), "url": ""}


def source_footer(src):
    url = src.get("url", "")
    if re.match(r"https?://", url):
        safe = html.escape(url, quote=True)
        return f'<a href="{safe}">{safe}</a>'
    label = "Local AI" if src.get("source_kind") == "ai-generated" else "Local"
    return html.escape(f"{label} – {src.get('title', 'video')}")


def extract(a):
    import cv2
    # CAG renders 8 figures per image on a 4-wide grid (FRAME_SHEET_SIZE, renamed from SHEET_FRAMES
    # when it dropped from 12) and chunks a longer motion across as many renders as it needs, so
    # there is no ceiling here — only the grid. A count off a multiple of 4 leaves the last row of
    # the last chunk part-empty. That wastes cells; it breaks nothing.
    if a.frames % 4:
        print(f"note: {a.frames} frames is not a multiple of 4, so CAG's last render row is "
              f"part-empty. Harmless, but {a.frames - a.frames % 4} or {a.frames + 4 - a.frames % 4} "
              f"fills the grid.", file=sys.stderr)
    # `--genre` is the clipper path: the capture directory says which clip this is, and the
    # name is allocated from the genre rather than typed, so no two captures can be handed
    # the same one. `--name` is the manual path. Both would be two names for one capture.
    if a.genre and a.name:
        sys.exit("--genre allocates the name; pass one or the other, not both")
    if a.genre and not a.out:
        sys.exit("--genre needs --out: the capture's directory is how its clip is found")
    out = outside_repo(a.out or os.path.join("work", a.name or "motion"))
    gender = capture_gender(out, getattr(a, "gender", None))
    name = allocate_motion(a.genre, out) if a.genre else a.name
    os.makedirs(os.path.join(out, "thumbs"), exist_ok=True)
    if re.match(r"https?://", a.source):
        # the download is an input, not an output: it stays local, out of the capture that is pushed
        src, title = fetch(a.source, "sources")
    else:
        src, title = a.source, os.path.basename(a.source)
    provenance = source_metadata(src, a.source if re.match(r"https?://", a.source) else a.url)
    if re.match(r"https?://", a.source): provenance["title"] = title
    name = name or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "motion"

    cap = cv2.VideoCapture(src)
    dur = cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 30)
    Wpx, Hpx = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    start = a.start or 0.0
    mp, lm = landmarker()
    win = a.window or a.frames / a.fps   # source seconds to look for; stretched onto the frame count
    if a.search:
        start, end, best = best_loop(cap, mp, lm, start, a.end if a.end is not None else dur, win, Wpx / Hpx)
        print(f"search: best {win:.1f}s loop starts at {start:.2f}s "
              f"(seam {best['seam']:.2f}, energy {best['energy']:.2f}); candidates:\n  " +
              "\n  ".join(f"{c['t']:6.2f}s seam {c['seam']:.2f} energy {c['energy']:.2f}" for c in best['top']))
        end = start + win
    else:
        end = min(dur, a.end if a.end is not None else start + win)
        if a.margin > 0:
            ts, poses = sample_poses(cap, mp, lm, max(0.0, start - a.margin), min(dur, end + a.margin), Wpx / Hpx)
            snap = pick_span(ts, poses, start, end, a.margin, a.playback == "loop")
            if snap:
                print(f"snap: {start:.2f}-{end:.2f}s -> {snap['t']:.2f}-{snap['end']:.2f}s "
                      f"(seam {snap['seam']:.2f}, energy {snap['energy']:.2f}); candidates:\n  " +
                      "\n  ".join(f"{c['t']:6.2f}-{c['end']:.2f}s seam {c['seam']:.2f} energy {c['energy']:.2f}"
                                  for c in snap["top"]))
                start, end = snap["t"], snap["end"]
            else:
                print(f"snap: no fully-tracked cut within {a.margin:.2f}s of either end; span used as given")
    span = end - start
    speed = span / (a.frames / a.fps)  # 1.0 == real time; 2.0 == source played at 2x

    frames, missing, shots = [], [], []
    for i, t in enumerate(sample_times(start, span, a.frames)):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, bgr = cap.read()
        if not ok: missing.append(i); continue
        sf = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1   # the source frame the seek actually landed on
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.pose_landmarks: missing.append(i); continue
        img, wld = res.pose_landmarks[0], res.pose_world_landmarks[0]
        P = {k: [img[j].x * Wpx / Hpx, img[j].y] for k, j in LM.items()}   # aspect-corrected, y down
        W = {k: [wld[j].x, wld[j].y, wld[j].z] for k, j in LM.items()}
        shots.append((i, bgr, [(v.x * Wpx, v.y * Hpx) for v in img]))
        frames.append(dict(i=i, t=round(t, 3), sf=sf, P=P, W=W))
    if len(frames) < 2:
        sys.exit(f"pose not found in enough frames (missing {missing}); try --start/--end on a clearer span")
    crop = write_thumbs(shots, os.path.join(out, "thumbs"), Wpx, Hpx)

    # constant scale: camera zoom / distance must not change body size. Per-frame pixels-per-metre =
    # summed image bone length / summed metric (world) bone length projected to the same plane, so
    # foreshortening cancels; scale every frame about its hip centre to the clip median.
    def ppm(f):
        return (sum(dist(f["P"][x], f["P"][y]) for x, y in BONES) /
                max(sum(dist(f["W"][x][:2], f["W"][y][:2]) for x, y in BONES), 1e-6))
    scales = [ppm(f) for f in frames]
    ref = sorted(scales)[len(scales) // 2]
    for f, sc in zip(frames, scales):
        k, hc = ref / max(sc, 1e-6), mid(f["P"]["hipL"], f["P"]["hipR"])
        for j in LM:
            f["P"][j] = [hc[0] + (f["P"][j][0] - hc[0]) * k, hc[1] + (f["P"][j][1] - hc[1]) * k]
    # stabilize: remove camera pan / tilt / stage travel. Centre each frame's hips horizontally and
    # pin its planted (lower) ankle to one shared floor line; crouches keep their depth.
    if a.stabilize:
        lows = [max(f["P"]["anL"][1], f["P"]["anR"][1]) for f in frames]
        floor_line = sorted(lows)[len(lows) // 2]
        for f, low in zip(frames, lows):
            cx, dy = mid(f["P"]["hipL"], f["P"]["hipR"])[0], floor_line - low
            for k in LM: f["P"][k] = [f["P"][k][0] - cx, f["P"][k][1] + dy]

    # exaggerate: push every landmark away from its clip-mean position (animation wants extremes)
    if a.exaggerate != 1.0:
        for key, dims in (("P", 2), ("W", 3)):
            for k in LM:
                m = [sum(f[key][k][d] for f in frames) / len(frames) for d in range(dims)]
                for f in frames:
                    f[key][k] = [m[d] + a.exaggerate * (f[key][k][d] - m[d]) for d in range(dims)]

    # clip-wide floor and body height; per-frame features. Floor is calibrated against the SOLE
    # (heel or toe, whichever sits lower) rather than the ankle — see describe() — so a dancer who
    # stays on the balls of her feet the whole clip does not read as airborne throughout.
    def sole_y(f): return max(f["P"]["heelL"][1], f["P"]["toeL"][1], f["P"]["heelR"][1], f["P"]["toeR"][1])
    floor = sorted(sole_y(f) for f in frames)[int(0.85 * (len(frames) - 1))]
    body_h = sorted(floor - min(f["P"]["earL"][1], f["P"]["earR"][1]) for f in frames)[len(frames) // 2]
    for f in frames:
        # a moving camera has no fixed floor: with --stabilize the lower sole counts as planted
        fl = sole_y(f) if a.stabilize else floor
        f["features"], f["cue"], f["depth"] = describe(f["P"], f["W"], fl, body_h)

    # motion energy -> keys (local minima: holds/extremes) and pilots (local maxima: fastest transitions)
    n = len(frames); loop = a.playback == "loop"
    def energy(i):
        j = (i + 1) % n if loop else min(i + 1, n - 1)
        return sum(dist(frames[i]["P"][k], frames[j]["P"][k]) for k in CORE) / len(CORE) / body_h
    E = [energy(i) for i in range(n)]
    med = sorted(E)[n // 2] or 1e-9
    for i, f in enumerate(frames):
        prev, nxt = E[(i - 1) % n], E[(i + 1) % n]
        if not loop and (i == 0 or i == n - 1): prev = nxt = E[i]
        f["energy"] = round(E[i] / med, 2)
        f["role"] = ("key" if i == 0 or (not loop and i == n - 1) or (E[i] <= prev and E[i] <= nxt)
                     else "pilot" if E[i] >= prev and E[i] >= nxt and E[i] > 1.3 * med else "inbetween")
        f["pace"] = "hold" if E[i] < 0.4 * med else "fast" if E[i] > 1.7 * med else "steady"
    seam = sum(dist(frames[-1]["P"][k], frames[0]["P"][k]) for k in CORE) / len(CORE) / body_h / med
    views = [f["features"]["view"] for f in frames]
    view = max(set(views), key=views.count)

    # pts carry depth: world z (metres, hips the origin, negative toward camera) scaled by the clip's
    # pixels-per-metre, so z reads in the same units as x and y and a 2D consumer can sort bones by it.
    def zof(f, k):
        return (f["W"][k][2] - (f["W"]["hipL"][2] + f["W"]["hipR"][2]) / 2) * ref

    doc = dict(
        schema=SCHEMA,
        title=name.replace("-", " ").title(), name=name,
        # `--url` matters more than it looks: since a bundle is named `<genre>-NN`, the manifest
        # is the *only* place the origin survives. Cutting several moves out of one video means
        # working from a downloaded copy — re-fetching per move would download it a dozen times —
        # and without this the capture would record a local path where the provenance should be.
        source=dict(**provenance,
                    file=portable(src), start=start, end=round(end, 3),
                    speed_factor=round(speed, 2), duration=round(dur, 2)),
        exaggerate=a.exaggerate, stabilized=a.stabilize, performer=a.performer, gender=gender,
        fps=a.fps, frame_count=a.frames, playback=a.playback, pingpong=a.pingpong, view=view,
        # `seam` is normalised by the median step, so 1.0 is one natural step. A loop now
        # contains `end`, and the snap search picks an `end` whose pose matches `start`, so the
        # last frame can repeat the first: well under a step is a stall -- a frame held at the
        # loop point -- and reads as wrong as a jump does.
        seam=(("stalls" if seam < 0.5 else "clean" if seam < 1.5 else "needs blend") if loop else "n/a"),
        seam_ratio=round(seam, 2) if loop else None,   # the number behind the verdict, for CAG's log
        missing_frames=missing, arc="",
        frames=[dict(i=f["i"], t=f["t"], role=f["role"], pace=f["pace"], energy=f["energy"],
                     cue=f["cue"], note="", features=f["features"], depth=f["depth"],
                     pts={k: [round(v[0], 4), round(v[1], 4), round(zof(f, k), 4)]
                          for k, v in f["P"].items()})
                for f in frames],
        floor_y=round(floor, 4), body_h=round(body_h, 4))
    # The source clip: the video cag drives its animation from, cut from the exact file traced.
    # `clip_frame` pins each traced frame to a clip frame, so a consumer needs no arithmetic on `t`.
    c0, c1 = clip_window(start, end, cap.get(cv2.CAP_PROP_FPS) or 30, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    doc["clip"] = cut_clip(src, c0, c1, os.path.join(out, "clip.mp4"), crop)
    for f, g in zip(doc["frames"], frames): f["clip_frame"] = g["sf"] - c0
    jp = os.path.join(out, "motion.json")
    kept = carry_over_writing(jp, doc)
    json.dump(doc, open(jp, "w"), indent=1)
    remember_gender(out, gender)

    print(kept)
    print(f"{jp}\n{title} | span {start:.2f}-{end:.2f}s | {a.frames}f @ {a.fps}fps | {a.playback} | "
          f"view {view} | source speed x{speed:.2f} | seam {doc['seam']} | missing {missing}")
    # loop rule: the seam is what the artist draws over and over, so its two ends should be
    # something a figure can hold — grounded, not mid-air — or the loop point has no stable pose.
    if a.playback == "loop" and (doc["frames"][0]["features"]["airborne"] or doc["frames"][-1]["features"]["airborne"]):
        print(f"warning: loop boundary is airborne (frame 0 {'airborne' if doc['frames'][0]['features']['airborne'] else 'grounded'}, "
              f"frame {a.frames - 1} {'airborne' if doc['frames'][-1]['features']['airborne'] else 'grounded'}) — "
              f"re-run with a different --start/--search window for a loop that lands on its feet")
    for f in doc["frames"]:
        print(f"{f['i']:>3} {f['t']:6.2f}s {f['role']:<9} {f['pace']:<6} {f['cue']}")
    store_capture(out)
    return jp


def carry_over_writing(jp, doc):
    """A re-run rebuilds motion.json from the video, which silently destroyed the hand-written arc
    and frame notes — the one part of the file a human made and the extractor cannot regenerate.
    Carry them over when the new capture lands on the same span, fps and frame count, so the same
    frame index still holds the same pose. When the span moved, the notes point at different poses,
    so they are dropped on purpose and said so out loud. Returns a line for the operator."""
    if not os.path.exists(jp): return "arc: new capture, nothing to carry over"
    try:
        old = json.load(open(jp))
    except (ValueError, OSError) as e:
        return f"arc: could not read the previous {jp} ({e}); nothing carried over"
    notes = {f["i"]: f.get("note", "") for f in old.get("frames", []) if f.get("note")}
    if not old.get("arc") and not notes: return "arc: previous capture had none, nothing to carry over"
    same = (old.get("fps") == doc["fps"] and old.get("frame_count") == doc["frame_count"]
            and old.get("playback") == doc["playback"]
            and abs(old.get("source", {}).get("start", -1) - doc["source"]["start"]) < 1e-6
            and abs(old.get("source", {}).get("end", -1) - doc["source"]["end"]) < 1e-6)
    if not same:
        o, n = old.get("source", {}), doc["source"]
        return (f"arc: DROPPED — the span moved ({o.get('start')}-{o.get('end')}s @ "
                f"{old.get('fps')}fps x{old.get('frame_count')} -> {n['start']}-{n['end']}s @ "
                f"{doc['fps']}fps x{doc['frame_count']}), so the old notes point at different "
                f"poses. Re-author the arc before export.")
    doc["arc"] = old.get("arc", "")
    for f in doc["frames"]:
        f["note"] = notes.get(f["i"], "")
    return f"arc: carried over from the previous capture ({len(notes)} frame notes), span unchanged"


def pose_dist(a, b): return sum(dist(a[k], b[k]) for k in CORE) / len(CORE)


def sample_times(start, span, n):
    """The instants a capture is cut at: `n` samples across the span, inclusive of both ends.

    Every playback samples the same way, so the first frame is `start` and the last is `end` --
    both boundaries a mark fixed are frames the artist is handed. A loop pays for that: its last
    frame no longer steps to the first, it can repeat it, which is what `seam` reports as a stall.
    """
    step = span / max(n - 1, 1)
    return [start + i * step for i in range(n)]


def sample_rate(cap, hz=None):
    """The rate a search walks the source at, defaulting to the video's own frame rate.

    A loop is cut at a frame, so a search that hops in coarser steps can only ever land its seam on
    a sampled instant — at the old 8 Hz that is a 125 ms grid over a 30 fps source, and the real
    best cut sat up to ~60 ms away from anything scored. A dance step covers a lot of pose in 60 ms.
    Never faster than the source: asking for more samples than there are frames re-scores the same
    frame twice.
    """
    import cv2
    src = cap.get(cv2.CAP_PROP_FPS) or 30
    return min(hz or src, src)


def sample_poses(cap, mp, lm, t0, t1, aspect, hz=None):
    """Walk [t0, t1] at `hz` (default: the source's own frame rate), returning (times, poses). A
    pose is hip-centred and torso-scaled so the comparison is shape only; an untracked sample is
    None, and each time is the frame's real timestamp rather than the instant asked for.

    One seek, then a sequential read: at native rate a seek per sample is slower than decoding
    straight through, and on an inter-frame codec it does not reliably land on the frame asked for.
    """
    import cv2
    hz = sample_rate(cap, hz)
    poses, ts = [], []
    cap.set(cv2.CAP_PROP_POS_MSEC, t0 * 1000)
    nxt, step = t0, 1.0 / hz
    while nxt <= t1 + 1e-9:
        ok, bgr = cap.read()
        if not ok: break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t + 1e-9 < nxt: continue            # decoded past the seek but not yet at this sample
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
        if res and res.pose_landmarks:
            img = res.pose_landmarks[0]
            P = {k: [img[j].x * aspect, img[j].y] for k, j in LM.items()}
            hip, h = mid(P["hipL"], P["hipR"]), max(dist(mid(P["shL"], P["shR"]), mid(P["hipL"], P["hipR"])), 1e-3)
            poses.append({k: [(v[0] - hip[0]) / h, (v[1] - hip[1]) / h] for k, v in P.items()})
        else:
            poses.append(None)
        ts.append(t)
        while nxt <= t + 1e-9: nxt += step     # a dropped or long frame skips its sample, not the walk
    return ts, poses


def livelier_half(cands):
    """Drop the stillest half of the candidates: the cleanest cut in a clip is always the moment
    nothing moves, and a motion source made of a held pose is worthless."""
    med = sorted(c["energy"] for c in cands)[len(cands) // 2]
    return [c for c in cands if c["energy"] >= med] or cands


def pick_span(ts, poses, start, end, margin, loop):
    """Both ends of the requested span are a guess; the move's own ending rarely lands on the
    second the user typed. Probe every cut within ±margin of each end and return the cleanest —
    for a loop the pair of poses that match, otherwise the stillest ending. Span length is free
    inside the margin because the winner is time-stretched onto the frame count anyway."""
    near = lambda t: [i for i, s in enumerate(ts) if abs(s - t) <= margin + 1e-9 and poses[i]]
    heads, tails = near(start), near(end)
    cands = []
    for i in heads:
        for j in tails:
            if j - i < 2 or any(p is None for p in poses[i:j + 1]): continue
            energy = sum(pose_dist(poses[k], poses[k + 1]) for k in range(i, j)) / (j - i)
            cost = pose_dist(poses[i], poses[j]) if loop else pose_dist(poses[j - 1], poses[j])
            cands.append(dict(t=ts[i], end=ts[j], seam=cost, energy=energy))
    if not cands: return None
    live = livelier_half(cands)
    best = min(live, key=lambda c: c["seam"])
    best["top"] = sorted(live, key=lambda c: c["seam"])[:5]
    return best


def best_loop(cap, mp, lm, t0, t1, length, aspect, hz=None):
    """Slide a `length`-second window over [t0, t1]; return (start, end, info) minimising the pose
    distance between window start and window end while keeping real motion inside the window.

    The window slides one source frame at a time (see `sample_rate`), so every cut the video can
    actually be made at is scored, not one in every few."""
    hz = sample_rate(cap, hz)
    ts, poses = sample_poses(cap, mp, lm, t0, t1, aspect, hz)
    pd = pose_dist
    n = round(length * hz)
    cands = []
    for i in range(max(len(poses) - n, 0)):
        win = poses[i:i + n + 1]
        if any(p is None for p in win): continue
        seam = pd(win[0], win[-1])
        energy = sum(pd(win[j], win[j + 1]) for j in range(n)) / n
        cands.append(dict(t=ts[i], seam=seam, energy=energy))
    if not cands: sys.exit("search: no fully-tracked window; try a different range")
    live = livelier_half(cands)
    best = min(live, key=lambda c: c["seam"])
    best["top"] = sorted(live, key=lambda c: c["seam"])[:5]
    return best["t"], best["t"] + length, best


# ---------------------------------------------------------------- render


def render(a):
    d = json.load(open(a.json))
    d["gender"] = motion_gender(d.get("gender"), a.json)
    for f in d["frames"]: f.setdefault("src", f["i"])   # which source frame each cell draws from
    # The sheet holds exactly the frames the animation was specified with — one cell each, no more:
    # ping-pong is a playback question, not a drawing count, and only changes the order the player
    # walks the same cells in. The capture owns that answer -- `render` had its own --pingpong once,
    # which set the flag on the loaded doc and so on the sheet's embedded payload, but never on
    # motion.json: the bundle then shipped a sheet that bounced beside a motion.json that said it
    # did not. One writer now, `extract`; to flip an existing capture, re-cut it or edit the json.
    out = a.out or os.path.join(os.path.dirname(a.json), f"{d['name']}-motion.html")
    tdir = os.path.join(os.path.dirname(a.json), "thumbs")
    rates = sorted({1, 4, d["fps"]})

    thumbs = []
    for f in d["frames"]:
        tp = os.path.join(tdir, f"f{f['src']:02d}.jpg")
        thumbs.append("data:image/jpeg;base64," + base64.b64encode(open(tp, "rb").read()).decode()
                      if os.path.exists(tp) else "")
    payload = dict(d, frames=[{k: v for k, v in f.items() if k != "pts"} for f in d["frames"]])
    src = d["source"]
    arc = "".join(f"<p>{html.escape(p)}</p>" for p in d["arc"].split("\n\n") if p.strip()) or \
          "<p class=muted>No performance arc written yet — fill <code>arc</code> in motion.json and re-render.</p>"
    # A ping-pong sheet walks 2n-2 cells, not n: the return leg replays every frame but the two ends.
    # Lap is what the player takes to come back around, so it counts cells walked, not frames held.
    n = len(d["frames"]); cells = 2 * n - 2 if d.get("pingpong") and n > 2 else n; lap = cells / d["fps"]
    keys = [f["i"] for f in d["frames"] if f["role"] == "key"]
    pilots = [f["i"] for f in d["frames"] if f["role"] == "pilot"]

    rows = "".join(
        f'<div class="maprow" style="--rowc:{ {"key": "var(--step)", "pilot": "var(--tap)"}.get(f["role"], "var(--muted)") }">'
        f'<span class="mv">{f["i"]}</span><span class="ct">{f["t"]:.2f}s · {f["role"]} · {f["pace"]}</span>'
        f'<span class="txt">{html.escape(f["cue"])}'
        f'{("<br><b>Note:</b> " + html.escape(f["note"])) if f["note"] else ""}</span></div>'
        for f in d["frames"])

    page = open(a.template or TEMPLATE_PATH).read()
    for k, v in dict(
        TITLE=html.escape(d["title"]),
        DEK=(f'Motion source from <b>{html.escape(src["title"])}</b>, {src["start"]:.1f}–{src["end"]:.1f}s '
             f'(source speed ×{src["speed_factor"]}, motion exaggerated ×{d.get("exaggerate", 1)}). Plays at <b>{d["fps"]} fps</b>; '
             f'{n} frames, {lap:.2f} s per {"lap" if d["playback"] == "loop" else "run"}'
             + (' — played out and back, the return leg reversing the out leg.'
                if d.get("pingpong") else '.')),
        N=str(n), NLAST=str(n - 1), FPS=str(d["fps"]), LAP=f"{lap:.2f} s", PLAYBACK=d["playback"] + (" · out and back" if d.get("pingpong") else ""), VIEW=html.escape(d["view"]),
        GENDER=d["gender"] or "unclassified",
        # the manifest's verdict, printed as it stands: a ping-pong capture used to have it
        # replaced with "clean", which hid a bad straight seam from the one person reviewing it
        SEAM=d["seam"], KEYS=", ".join(map(str, keys)) or "—", PILOTS=", ".join(map(str, pilots)) or "—",
        URL=html.escape(src.get("url", "")), SOURCE=source_footer(src), ARC=arc, ROWS=rows,
        RATES="".join(f'<button data-fps="{r}" aria-pressed="{str(r == d["fps"]).lower()}">{r} fps</button>' for r in rates),
        THUMBS=json.dumps(thumbs), DATA=json.dumps(payload).replace("</", "<\\/"),
    ).items():
        page = page.replace("{{" + k + "}}", v)
    open(outside_repo(out), "w").write(page)
    print(out)
    store_capture(os.path.dirname(os.path.abspath(a.json)), out)   # the arc was written since extract
    return out


# The sheet's markup, styling and player live in templates/sheet.html next to this script, so the
# design can be edited (or replaced with --template) without touching the extractor.
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "templates", "sheet.html")


# ---------------------------------------------------------------- local video generation
VIDEO_WORKFLOW = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "workflows", "wan22-5b.json"))
VIDEO_NEGATIVE = ("cropped head, cropped feet, cropped hands, close-up, moving camera, camera cuts, "
                  "rear view, turning away, multiple people, cluttered background, text, watermark, "
                  "overexposure, blown highlights, washed out skin, glowing skin, overly bright lighting, "
                  "motion blur, ghosting, blurry details, static pose, low quality, deformed limbs, "
                  "extra limbs, extra fingers, fused fingers")


def atomic_json(path, data):
    outside_repo(path); outside_repo(path + ".tmp")
    with open(path + ".tmp", "w") as fh:
        json.dump(data, fh, indent=2); fh.flush(); os.fsync(fh.fileno())
    os.replace(path + ".tmp", path)


def generation_frames(seconds):
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be finite and positive")
    return 4 * math.ceil(seconds * 24 / 4) + 1


def video_properties(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                        "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,duration",
                        "-of", "json", path], capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    num, den = s["avg_frame_rate"].split("/"); fps = float(num) / float(den)
    return dict(width=int(s["width"]), height=int(s["height"]), fps=fps,
                frame_count=int(s["nb_read_frames"]), duration=float(s["duration"]))


def comfy_request(url, route, data=None, content_type="application/json"):
    if isinstance(data, dict): data = json.dumps(data).encode()
    req = urllib.request.Request(url.rstrip("/") + route, data=data,
                                 headers={"Content-Type": content_type} if data is not None else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def ensure_comfy(url):
    try:
        comfy_request(url, "/system_stats"); return
    except urllib.error.HTTPError: raise  # a reachable server's error is not a request to start another
    except (urllib.error.URLError, TimeoutError): pass
    if url.rstrip("/") != "http://127.0.0.1:8188":
        raise ValueError(f"ComfyUI is unavailable at {url}; start that server and resume")
    launcher = os.path.expanduser("~/ComfyUI/run-local16.sh")
    if not os.path.isfile(launcher): raise ValueError(f"no local ComfyUI launcher at {launcher}")
    log = outside_repo(os.path.join(HOME, "comfy.log"))
    with open(log, "ab") as fh:
        proc = subprocess.Popen([launcher], stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(120):
        try:
            comfy_request(url, "/system_stats"); return
        except (urllib.error.URLError, TimeoutError):
            if proc.poll() is not None: break
            time.sleep(1)
    raise ValueError(f"ComfyUI did not start; see {log}")


def video_preflight(url, graph):
    info = comfy_request(url, "/object_info")
    missing = sorted({n["class_type"] for n in graph.values()} - info.keys())
    if missing: raise ValueError("ComfyUI lacks nodes: " + ", ".join(missing))
    maximum = info["Wan22ImageToVideoLatent"]["input"]["required"].get("length", [None, {}])[1].get("max")
    if maximum and graph["55"]["inputs"]["length"] > maximum:
        raise ValueError(f"requested duration exceeds this ComfyUI node's {maximum} source-frame limit")
    models = {}
    for node, field in (("37", "unet_name"), ("38", "clip_name"), ("39", "vae_name")):
        n = graph[node]; name = n["inputs"][field]
        choices = info[n["class_type"]]["input"]["required"][field][0]
        if name not in choices: raise ValueError(f"ComfyUI lacks model: {name}")
        models[n["class_type"]] = name
    return models


def upload_start_image(url, image, out):
    path = outside_repo(os.path.join(out, "start.png"))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", image, "-frames:v", "1",
                    "-vf", "scale=576:864:force_original_aspect_ratio=decrease,pad=576:864:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1",
                    path], check=True)
    boundary = "motionartist" + uuid.uuid4().hex
    data = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="motion-artist-{sha256(path)}.png"\r\n'
            'Content-Type: image/png\r\n\r\n').encode() + open(path, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    reply = comfy_request(url, "/upload/image", data, "multipart/form-data; boundary=" + boundary)
    return "/".join(x for x in (reply.get("subfolder"), reply["name"]) if x), sha256(path)


def recover_generation(url, token):
    """Recover a submission whose HTTP response was lost, without submitting another job."""
    q = comfy_request(url, "/queue")
    entries = q.get("queue_running", []) + q.get("queue_pending", [])
    entries += [h.get("prompt", []) for h in comfy_request(url, "/history").values()]
    found = [p[1] for p in entries if len(p) > 3 and p[3].get("motion_artist_job") == token]
    if len(set(found)) > 1: raise ValueError("multiple jobs share this generation token; refusing to guess")
    return found[0] if found else None


def wait_generation(url, state, state_path, timeout=90 * 60, poll=5):
    job = state["prompt_id"]
    last, running = time.monotonic(), False
    while True:
        now = time.monotonic()
        if running: state["running_seconds"] = state.get("running_seconds", 0) + now - last
        last = now
        h = comfy_request(url, "/history/" + urllib.parse.quote(job, safe="")).get(job, {})
        status = h.get("status", {})
        if status.get("status_str") == "error" or any(m[0] in ("execution_error", "execution_interrupted") for m in status.get("messages", [])):
            state.update(status="failed", error=status); atomic_json(state_path, state)
            raise ValueError(f"ComfyUI job {job} failed: {status}")
        if status.get("completed"):
            output = h.get("outputs", {}).get("58", {})
            videos = output.get("images", []) + output.get("videos", [])
            matches = [v for v in videos if v.get("filename", "").lower().endswith(".mp4")]
            if len(matches) != 1: raise ValueError(f"job {job} completed without exactly one MP4 output")
            stamps = {m[0]: m[1].get("timestamp") for m in status.get("messages", []) if isinstance(m[1], dict)}
            if stamps.get("execution_start") and stamps.get("execution_success"):
                state["execution_seconds"] = (stamps["execution_success"] - stamps["execution_start"]) / 1000
            state.update(status="encoded", output=matches[0]); atomic_json(state_path, state)
            return
        q = comfy_request(url, "/queue")
        running = any(p[1] == job for p in q.get("queue_running", []))
        pending = any(p[1] == job for p in q.get("queue_pending", []))
        state["status"] = "running" if running else "queued"
        if running:
            stats = comfy_request(url, "/system_stats")
            observed = state.setdefault("observed_memory", {})
            for device in stats.get("devices", []):
                used = device["vram_total"] - device["vram_free"]
                observed["peak_device_used_bytes"] = max(observed.get("peak_device_used_bytes", 0), used)
                observed["device"] = device["name"]
            system = stats.get("system", {})
            if "ram_total" in system and "ram_free" in system:
                used = system["ram_total"] - system["ram_free"]
                observed["peak_system_ram_used_bytes"] = max(observed.get("peak_system_ram_used_bytes", 0), used)
        atomic_json(state_path, state)
        if running and state.get("running_seconds", 0) >= timeout:
            raise TimeoutError(f"job {job} exceeded 90 minutes running; job preserved, rerun to resume")
        if not running and not pending and not h:
            # History and queue are separate snapshots; completion can fall between them.
            if comfy_request(url, "/history/" + urllib.parse.quote(job, safe="")).get(job): continue
            raise ValueError(f"job {job} is absent from queue and history; state preserved, no new submission")
        time.sleep(poll)


def generate(a):
    out = outside_repo(os.path.abspath(os.path.join(HOME, a.out)))
    os.makedirs(out, exist_ok=True)
    state_path = outside_repo(os.path.join(out, "generation.json"))
    video_path = outside_repo(os.path.join(out, "source.mp4"))
    with open(outside_repo(os.path.join(out, "generation.lock")), "a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError("another generate command is using this output directory")
        return generate_locked(a, out, state_path, video_path)


def generate_locked(a, out, state_path, video_path):
    if not a.prompt.strip() or not a.title.strip(): raise ValueError("prompt and title must not be empty")
    if not 0 <= a.seed < 2 ** 64: raise ValueError("seed must be an unsigned 64-bit integer")
    graph = json.load(open(VIDEO_WORKFLOW))
    expanded = ("One full-body adult performer facing forward throughout, alone, filmed head to toe. "
                "Fixed camera, matte light-gray background, soft diffuse studio lighting, balanced exposure, "
                "natural skin midtones and clear arm and hand detail. Visible hands and feet with room "
                "above the hands and below the feet. A continuous natural performance with clear preparation, "
                "action, follow-through and recovery to a relaxed starting stance. Motion: " + a.prompt.strip())
    if a.image:
        expanded = ("Animate the single full-body character in the supplied starting image. Preserve the "
                    "character's identity, proportions, clothing, colors, visual style and plain background "
                    "from that image. Apply requested lighting changes while preserving identity and style. "
                    "Fixed camera, character facing the camera throughout. "
                    "Keep the complete figure, both hands and both feet visible throughout the movement, "
                    "including the raised hand. Clear preparation, action, follow-through and recovery "
                    "to the starting stance. Motion: " + a.prompt.strip())
    graph["6"]["inputs"]["text"] = expanded; graph["7"]["inputs"]["text"] = VIDEO_NEGATIVE
    graph["3"]["inputs"]["seed"] = a.seed
    graph["55"]["inputs"]["length"] = generation_frames(a.duration)
    settings = dict(title=a.title, requested_prompt=a.prompt, expanded_prompt=expanded, negative_prompt=VIDEO_NEGATIVE,
                    duration=a.duration, seed=a.seed, comfy_url=a.comfy_url.rstrip("/"),
                    input_image_sha256=sha256(a.image) if a.image else None,
                    workflow_template_sha256=sha256(VIDEO_WORKFLOW), width=576, height=864, fps=24,
                    source_frames=graph["55"]["inputs"]["length"])
    state = json.load(open(state_path)) if os.path.isfile(state_path) else None
    if state and state["settings"] != settings:
        raise ValueError("generation settings changed; use a new output directory")
    if state and state["status"] == "complete":
        if not os.path.isfile(video_path) or sha256(video_path) != state["source_sha256"]:
            raise ValueError("completed source video is missing or changed; state preserved")
        print(f"{video_path}\n{state_path}\n{state['video']['duration']:.3f}s encoded (reused)"); return video_path
    if state and state["status"] == "failed": raise ValueError(f"previous job failed; use a new directory: {state.get('error')}")
    if not state and os.path.exists(video_path): raise ValueError("source.mp4 already exists without generation state; use a new directory")
    url = settings["comfy_url"]; ensure_comfy(url)
    if not state:
        if a.image:
            graph["56"] = dict(class_type="LoadImage", inputs=dict(image="start.png"))
            graph["55"]["inputs"]["start_image"] = ["56", 0]
        models = video_preflight(url, graph)
        image_digest = None
        if a.image:
            graph["56"]["inputs"]["image"], image_digest = upload_start_image(url, a.image, out)
        token = uuid.uuid4().hex
        graph["58"]["inputs"]["filename_prefix"] = "motion-artist/" + token
        state = dict(settings=settings, status="submitting", job_token=token, running_seconds=0,
                     submitted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     model_references=models, uploaded_image_sha256=image_digest,
                     workflow_sha256=hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(), workflow=graph)
        atomic_json(state_path, state)  # submission intent survives a crash or a lost response
        reply = comfy_request(url, "/prompt", dict(prompt=graph, client_id=token, extra_data=dict(motion_artist_job=token)))
        if not reply.get("prompt_id"): raise ValueError(f"ComfyUI did not return a job id: {reply}")
        state.update(prompt_id=reply["prompt_id"], status="queued"); atomic_json(state_path, state)
        print(f"ComfyUI job {state['prompt_id']} ({settings['source_frames']} source frames)", flush=True)
    if not state.get("prompt_id"):
        job = recover_generation(url, state["job_token"])
        if not job: raise ValueError("submission outcome unknown; state preserved, refusing duplicate submission")
        state.update(prompt_id=job, status="queued"); atomic_json(state_path, state)
    if state["status"] != "encoded":
        # A retry gets a fresh running allowance; cumulative observed time remains in provenance.
        wait_generation(url, state, state_path, timeout=state.get("running_seconds", 0) + 90 * 60)
    query = urllib.parse.urlencode({k: state["output"][k] for k in ("filename", "subfolder", "type") if k in state["output"]})
    temp = outside_repo(video_path + ".part")
    with urllib.request.urlopen(url + "/view?" + query, timeout=60) as r, open(temp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    props = video_properties(temp)
    if (props["width"], props["height"], props["fps"], props["frame_count"]) != (576, 864, 24, settings["source_frames"]):
        raise ValueError(f"encoded video properties differ from workflow: {props}; job preserved")
    os.replace(temp, video_path)
    state.update(status="complete", video=props, source_sha256=sha256(video_path),
                 completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    atomic_json(state_path, state)
    print(f"{video_path}\n{state_path}\n{props['duration']:.3f}s encoded ({props['frame_count']} source frames at {props['fps']:g} fps)")
    return video_path


# ---------------------------------------------------------------- selftest
def tstamp(v):
    """'83', '1:23', '1:23.5' -> seconds."""
    parts = [float(x) for x in str(v).split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def selftest():
    """Synthetic poses: arm overhead + one foot lifted must be described as such."""
    from unittest.mock import patch
    from types import SimpleNamespace
    # Phone footage carries stream side data; ffprobe's CSV can append a comma to its rate.
    probe = json.dumps({"streams": [{"r_frame_rate": "60000/1001", "side_data_list": [{}]}]})
    with patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=probe)):
        assert source_rate("phone.m4v") == "60000/1001"
    with patch.object(subprocess, "run", return_value=SimpleNamespace(stdout='{"streams": []}')):
        try: source_rate("audio.m4a")
        except ValueError: pass
        else: raise AssertionError("accepted a source without a video stream")
    def pose(wrL_y, anR_y):
        P = dict(nose=[0.5, 0.20], eyeL=[0.52, 0.19], eyeR=[0.48, 0.19], earL=[0.54, 0.20], earR=[0.46, 0.20],
                 shL=[0.60, 0.30], shR=[0.40, 0.30], elL=[0.66, 0.42], elR=[0.34, 0.42], wrL=[0.70, wrL_y], wrR=[0.30, 0.55],
                 hipL=[0.56, 0.55], hipR=[0.44, 0.55], knL=[0.56, 0.75], knR=[0.44, 0.75], anL=[0.56, 0.95], anR=[0.44, anR_y],
                 heelL=[0.55, 0.96], heelR=[0.43, anR_y + .01], toeL=[0.59, 0.96], toeR=[0.47, anR_y + .01])
        W = {k: [v[0] - 0.5, v[1] - 0.55, 0.0] for k, v in P.items()}
        W["shL"][2] = -0.05  # character-left shoulder slightly nearer camera => 3/4 view
        return P, W
    f, cue, depth = describe(*pose(0.10, 0.95), floor_y=0.95, body_h=0.75)
    assert "overhead" in f["arm_L"] and "low" in f["arm_R"], f
    assert "both feet down" in f["weight"] or "centred" in f["weight"], f
    assert f["view"] in ("front", "3/4"), f
    assert depth["near_side"] == "L" and depth["limbs"]["legL"] == "level", depth
    f, _, _ = describe(*pose(0.60, 0.80), floor_y=0.95, body_h=0.75)
    assert "character-right foot lifted" in f["weight"], f
    # crossed legs: the leg with the more negative world z is in front, and the cue must say so
    P, W = pose(0.60, 0.95)
    P["anL"], P["anR"] = [0.44, 0.95], [0.56, 0.95]          # ankles swapped == legs crossed
    for k in ("knL", "anL"): W[k][2] = -0.20                 # character-left leg toward the camera
    for k in ("knR", "anR"): W[k][2] = 0.20
    f, cue, depth = describe(P, W, floor_y=0.95, body_h=0.75)
    assert depth["limbs"] == dict(legL="near", legR="far", armL="level", armR="level"), depth
    assert "The character-right leg passes behind the character-left." in cue, cue
    # a 2.0 s cycle asked for as 0.0-1.75 s: the clean cut is the pose repeat at 2.0, inside the
    # margin, and the picker must move the end there rather than obey the second that was typed.
    hz, period = 8, 2.0
    ts = [i / hz for i in range(int(3 * hz) + 1)]
    cyc = lambda t: {k: [math.sin(2 * math.pi * t / period + j), math.cos(2 * math.pi * t / period + j)]
                     for j, k in enumerate(CORE)}
    got = pick_span(ts, [cyc(t) for t in ts], 0.0, 1.75, 0.5, loop=True)
    assert got["seam"] < 1e-9 and abs(got["end"] - got["t"] - period) < 1e-9, got   # a whole cycle
    assert abs(got["t"]) <= 0.5 and abs(got["end"] - 1.75) <= 0.5, got              # inside the margin
    # one-shot scores the stillest ending instead: motion stops dead at 1.5 s
    still = [cyc(min(t, 1.5)) for t in ts]
    assert pick_span(ts, still, 0.0, 1.75, 0.5, loop=False)["end"] >= 1.625
    assert pick_span(ts, [None] * len(ts), 0.0, 1.75, 0.5, loop=True) is None
    # both boundaries a mark fixed are frames the capture contains, whatever the playback
    assert sample_times(1.0, 2.0, 5) == [1.0, 1.5, 2.0, 2.5, 3.0], sample_times(1.0, 2.0, 5)
    assert sample_times(1.0, 2.0, 1) == [1.0]                 # one frame cannot span anything
    # pelvis wound against the shoulders: the one thing no knee or elbow angle can carry
    P, W = pose(0.60, 0.95)
    W["shL"][2] = 0.0                                   # shoulders square to camera
    W["hipL"][2], W["hipR"][2] = 0.12, -0.12            # character-right hip forward
    f, cue, _ = describe(P, W, floor_y=0.95, body_h=0.75)
    assert "hips turned character-right against the shoulders" in cue, cue
    assert f["twist_deg"] < -10, f["twist_deg"]
    for k in ("hipL", "hipR", "shL", "shR"): W[k][2] = 0.0   # both girdles square: no twist word
    assert describe(P, W, floor_y=0.95, body_h=0.75)[0]["twist"] == ""
    # a re-extract must not eat the hand-written arc when it lands on the same span, and must not
    # silently keep it when the span moved, because the notes then point at different poses
    import tempfile
    def doc_at(start, end, n=2):
        return dict(fps=4, frame_count=n, playback="loop", source=dict(start=start, end=end),
                    arc="", frames=[dict(i=i, note="") for i in range(n)])
    with tempfile.TemporaryDirectory() as td:
        jp = os.path.join(td, "motion.json")
        assert "new capture" in carry_over_writing(jp, doc_at(1.0, 2.0))
        prior = doc_at(1.0, 2.0)
        prior["arc"], prior["frames"][1]["note"] = "the hips lead", "hit pose"
        json.dump(prior, open(jp, "w"))
        same = doc_at(1.0, 2.0)
        assert "carried over" in carry_over_writing(jp, same)
        assert same["arc"] == "the hips lead" and same["frames"][1]["note"] == "hit pose", same
        moved = doc_at(1.5, 2.5)
        assert "DROPPED" in carry_over_writing(jp, moved)
        assert moved["arc"] == "" and not any(f["note"] for f in moved["frames"]), moved
        assert "DROPPED" in carry_over_writing(jp, doc_at(1.0, 2.0, n=3))   # frame count changed
    # footwork: a planted foot with the heel above the ball is "on the ball", and a flat foot says
    # nothing at all rather than padding every cue with a word the artist can ignore
    P, W = pose(0.60, 0.95)
    def sole(sfx, heel, toe):   # the cue reads the sole in world space, so move both
        P["heel" + sfx], P["toe" + sfx] = list(heel), list(toe)
        for k, v in (("heel" + sfx, heel), ("toe" + sfx, toe)):
            W[k] = [v[0] - 0.5, v[1] - 0.55, 0.0]
    sole("L", [0.55, 0.88], [0.59, 0.95])                       # heel well above the ball
    f, cue, _ = describe(P, W, floor_y=0.95, body_h=0.75)
    assert "on the ball of the character-left foot, heel lifted" in cue, cue
    sole("L", [0.55, 0.95], [0.59, 0.955])                      # flat
    assert "ball of the character-left" not in describe(P, W, floor_y=0.95, body_h=0.75)[1]
    sole("L", [0.55, 0.95], [0.59, 0.88])                       # toe up, rocked back on the heel
    assert "character-left heel down, toe up" in describe(P, W, floor_y=0.95, body_h=0.75)[1]
    sole("L", [0.55, 0.88], [0.59, 0.95])                       # both on the ball: said once
    sole("R", [0.43, 0.88], [0.47, 0.95])
    assert describe(P, W, floor_y=0.95, body_h=0.75)[0]["feet"] == "on the balls of both feet, heels lifted"
    # a sole pointed at the camera is foreshortened in image x to near-vertical; measured in 3D it
    # is still the flat foot it actually is, and must not be called "on the ball"
    sole("L", [0.55, 0.95], [0.551, 0.99])
    W["toeL"] = [W["heelL"][0] + 0.004, W["heelL"][1] + 0.04, W["heelL"][2] + 0.22]
    assert "ball of the character-left" not in describe(P, W, floor_y=0.95, body_h=0.75)[1]
    # a lifted foot is not reported: the cue already says the foot is off the floor
    f2, cue2, _ = describe(*pose(0.60, 0.80), floor_y=0.95, body_h=0.75)
    assert "character-right" not in f2["feet"], f2["feet"]
    assert bend_word(170) == "straight" and bend_word(80) == "bent ~90°"
    assert tstamp("1:23.5") == 83.5 and tstamp("7") == 7
    # the manifest carries the capture's own seam verdict and the number behind it: CAG reads the
    # verdict before drawing, and an absent seam must read as no complaint rather than a bad one
    cap = dict(name="t", title="T", fps=4, frame_count=2, playback="loop", view="front",
               seam="needs blend", seam_ratio=2.4, source={}, arc="  ")
    man = bundle_manifest(cap, [("motion.json", __file__)])
    assert man["files"]["motion.json"] == sha256(__file__) and len(man["files"]["motion.json"]) == 64
    assert man["arc_written"] is False and man["bundle"] == "motion-source"
    assert man["seam"] == "needs blend" and man["seam_ratio"] == 2.4, man
    # performer rides through when stated, and reads as unstated rather than guessed when it is not
    assert man["performer"] is None
    assert man["gender"] is None
    for value in (*GENDERS, None):
        assert bundle_manifest({**cap, "gender": value, "performer": "female"}, [])["gender"] == value
    for value in ("", "unknown", "unclassified", False, 1, [], {}):
        try: bundle_manifest({**cap, "gender": value}, [])
        except ValueError: pass
        else: raise AssertionError(f"accepted invalid gender {value!r}")
    with tempfile.TemporaryDirectory() as td:
        assert capture_gender(td) is None
        atomic_json(os.path.join(td, "motion.json"), {"gender": "female"})
        assert capture_gender(td) == "female"
        assert capture_gender(td, "male") == "male"
        assert capture_gender(td, "any") == "any"
        assert capture_gender(td, "unclassified") is None
        capture = os.path.join(td, "work", "video", "clip-01")
        os.makedirs(capture)
        atomic_json(os.path.join(capture, "motion.json"), {"name": "test-01", "gender": "female"})
        marks = os.path.join(td, "work", "video", "clips.json")
        clip = dict(capture="work/video/clip-01", motion="test-02")
        atomic_json(marks, {"clips": [clip]})
        assert capture_gender(capture) is None     # another motion occupied this directory
        clip["motion"] = "test-01"; atomic_json(marks, {"clips": [clip]})
        assert capture_gender(capture) == "female"
        clip["gender"] = None; atomic_json(marks, {"clips": [clip]})
        assert capture_gender(capture) is None     # a saved clear takes priority over the capture
    # the per-frame spread rides alongside the majority `view`, and is absent rather than empty
    # when a caller has no frames to count
    assert man["view_frames"] is None, man["view_frames"]
    mixed = {**cap, "frames": [{"features": {"view": v}} for v in ("front", "front", "3/4", "back")]}
    assert bundle_manifest(mixed, [])["view_frames"] == {"front": 2, "3/4": 1, "back": 1}
    assert bundle_manifest({**cap, "performer": "female"}, [])["performer"] == "female"
    # out-and-back playback is declared, defaults to off, and never edits the seam it sits beside:
    # a consumer with no ping-pong plays the straight loop and must still see that jump coming
    assert man["pingpong"] is False
    pp = bundle_manifest({**cap, "pingpong": True}, [])
    assert pp["pingpong"] is True and pp["seam"] == "needs blend" and pp["seam_ratio"] == 2.4, pp
    cap.pop("seam_ratio")                                  # a schema/1 capture predates the number
    assert bundle_manifest(cap, [("motion.json", __file__)])["seam_ratio"] is None
    # a loop is cut at a frame: the search walks the source at its own rate by default, never
    # coarser by accident and never finer than there are frames to score
    class _Cap:
        def get(self, prop): return 29.97
    assert abs(sample_rate(_Cap()) - 29.97) < 1e-9
    assert abs(sample_rate(_Cap(), 8) - 8) < 1e-9, "an explicit slower rate is still honoured"
    assert abs(sample_rate(_Cap(), 120) - 29.97) < 1e-9, "never ask for more samples than frames"
    # the genre is the name minus the motion number, and a genre may carry digits of its own
    assert set_name({"name": "hiphop-07"}) == "hiphop"
    assert set_name({"name": "hip-hop-2-11"}) == "hip-hop-2"
    assert set_name({"name": "dougie"}) == "dougie"
    assert motion_num("hiphop-07", "hiphop") == 7 and motion_num("hiphop-07", "karate") is None
    assert motion_num("hip-hop-2-07", "hip-hop-2") == 7   # a genre may carry its own digits
    # A clip's position in its video's array is not its motion number: numbers are handed out
    # across every video in the genre, so the second video's first clip is not hiphop-01.
    import tempfile as _t
    with _t.TemporaryDirectory() as _d:
        def _clips(key, genre, caps):
            os.makedirs(os.path.join(_d, "work", key), exist_ok=True)
            json.dump({"video": key, "genre": genre,
                       "clips": [{"capture": f"work/{key}/{c}"} for c in caps]},
                      open(os.path.join(_d, "work", key, "clips.json"), "w"))
        cap = lambda k, c: os.path.join(_d, "work", k, c)

        _clips("britney-spears-toxic", "hiphop", ["clip-01", "clip-02"])
        _clips("dojo-kata", "hiphop", ["clip-01"])
        assert allocate_motion("hiphop", cap("britney-spears-toxic", "clip-01")) == "hiphop-01"
        assert allocate_motion("hiphop", cap("britney-spears-toxic", "clip-02")) == "hiphop-02"
        # the other video's *first* clip is the genre's third motion, not its first
        assert allocate_motion("hiphop", cap("dojo-kata", "clip-01")) == "hiphop-03"
        # asked again, a capture reads back the number it was given -- a re-cut must replace
        # its bundle, not take a second number
        assert allocate_motion("hiphop", cap("dojo-kata", "clip-01")) == "hiphop-03"
        # and it was written back beside the clip, which is the only place it lives
        doc = json.load(open(os.path.join(_d, "work", "dojo-kata", "clips.json")))
        assert doc["clips"][0]["motion"] == "hiphop-03", doc
        # a number already exported is taken even though no clips file claims it
        os.makedirs(os.path.join(_d, "exports", "hiphop", "hiphop-09"))
        _clips("sensei-kick", "hiphop", ["clip-01"])
        assert allocate_motion("hiphop", cap("sensei-kick", "clip-01")) == "hiphop-10"
        # a genre of its own starts at 01 again
        _clips("sensei-flip", "karate", ["clip-01"])
        assert allocate_motion("karate", cap("sensei-flip", "clip-01")) == "karate-01"
        assert work_root(cap("dojo-kata", "clip-01")) == _d
        assert work_root("/nowhere/near/a/capture") is None
    assert portable(os.path.join(os.getcwd(), "work", "x.mp4")) == os.path.join("work", "x.mp4")
    assert portable("/somewhere/else/x.mp4") == "x.mp4"
    # nothing lands in the checkout, by default or by --out
    assert not os.path.realpath(HOME).startswith(REPO + os.sep), f"HOME {HOME} is inside the repo"
    assert outside_repo("/elsewhere/x") == "/elsewhere/x"
    for p in (os.path.join(REPO, "work", "x"), os.path.join(REPO, "exports")):
        try: outside_repo(p)
        except SystemExit: pass
        else: raise AssertionError(f"{p} is in the repo and was allowed")
    # the clip is the traced span plus half a second each side, clamped to the video
    assert clip_window(10.0, 12.0, 30, 10000) == (285, 375)
    assert clip_window(0.2, 1.0, 30, 40) == (0, 39)
    # a head box is None with under 3 visible face points, and stays inside the frame near an edge
    class _L:
        def __init__(self, x, y, v=0.9): self.x, self.y, self.visibility = x, y, v
    face = [_L(0.5 + 0.01 * (j % 3), 0.1 + 0.01 * (j // 3)) for j in range(11)] + [_L(0, 0, 0)] * 22
    b = head_box(face, 1000, 1000)
    assert b and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000 and b[1] < 100 < b[3], b
    assert head_box([_L(0.5, 0.5, 0.1)] * 33, 1000, 1000) is None
    assert head_box([_L(0.001 * j, 0.001 * j) for j in range(33)], 1000, 1000)[:2] == [0, 0]
    # fit_aspect: a wide box grows tall and slides back into frame; a too-tall one shrinks to the
    # frame; one already at the aspect stays put; every result is even (the thumb crop's box).
    assert fit_aspect(100, 100, 500, 300, 9 / 16, 1280, 720) == (100, 0, 400, 710)
    x, y, w, h = fit_aspect(600, -200, 700, 900, 9 / 16, 1280, 720)
    assert (w, h) == (404, 720) and y == 0 and 0 <= x and x + w <= 1280
    assert fit_aspect(0, 0, 360, 640, 9 / 16, 1280, 720) == (0, 0, 360, 640)
    for b in ((10, 10, 33, 77), (5, 5, 101, 203), (0, 0, 1279, 719)):
        assert all(v % 2 == 0 for v in fit_aspect(*b, THUMB_W / THUMB_H, 1280, 720))
    # A tall portrait must retain both ends of the figure instead of forcing a shorter crop.
    import cv2, numpy as np
    with tempfile.TemporaryDirectory() as td:
        portrait = np.zeros((640, 360, 3), dtype=np.uint8)
        portrait[20:40, 160:200] = (0, 0, 255)
        portrait[600:620, 160:200] = (0, 255, 0)
        box = write_thumbs([(0, portrait, [(180, 30), (180, 610)])], td, 360, 640)
        card = cv2.imread(os.path.join(td, 'f00.jpg'))
        assert box == (0, 0, 360, 640) and card.shape == (512, 384, 3)
        assert card[24, 192, 2] > 240 and card[488, 192, 1] > 240
        assert abs(int(card[256, 20, 0]) - 28) < 5  # side padding, not stretched footage
    generation_selftest()
    print("selftest ok")


def generation_selftest():
    """Exercise the HTTP contract with a local fake server and real encoded video/image files."""
    import tempfile, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from unittest.mock import patch
    assert generation_frames(5) == 121 and generation_frames(0.1) == 5
    for seconds in (0, -1, float("nan"), float("inf")):
        try: generation_frames(seconds)
        except ValueError: pass
        else: raise AssertionError("invalid duration accepted")
    with tempfile.TemporaryDirectory() as tmp:
        video = os.path.join(tmp, "test.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=white:s=576x864:r=24",
                        "-frames:v", "121", "-c:v", "libx264", "-pix_fmt", "yuv420p", video], check=True)
        info = {n["class_type"]: {"input": {"required": {}}} for n in json.load(open(VIDEO_WORKFLOW)).values()}
        info["LoadImage"] = {"input": {"required": {}}}
        for kind, field, name in (("UNETLoader", "unet_name", "wan2.2_ti2v_5B_fp16.safetensors"),
                                  ("CLIPLoader", "clip_name", "umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
                                  ("VAELoader", "vae_name", "wan2.2_vae.safetensors")):
            info[kind]["input"]["required"][field] = [[name]]
        fake = dict(jobs={}, submissions=0, mode="complete", uploads=[])

        class FakeComfy(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, data, raw=False):
                body = data if raw else json.dumps(data).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                if self.path == "/system_stats": return self.reply({})
                if self.path == "/object_info": return self.reply(info)
                if self.path == "/queue":
                    active = [h["prompt"] for h in fake["jobs"].values()]
                    return self.reply(dict(queue_running=active if fake["mode"] == "running" else [],
                                           queue_pending=active if fake["mode"] == "queued" else []))
                if self.path.startswith("/view?"): return self.reply(open(video, "rb").read(), raw=True)
                if self.path.startswith("/history"):
                    jobs = fake["jobs"] if fake["mode"] in ("complete", "failed") else {}
                    if fake["mode"] == "failed":
                        jobs = {k: {**h, "status": {"status_str": "error", "messages": [["execution_error", {"exception_message": "test failure"}]]}} for k, h in jobs.items()}
                    job = self.path.removeprefix("/history/")
                    return self.reply(jobs if self.path == "/history" else {k: h for k, h in jobs.items() if k == job})
                self.send_error(404)
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/upload/image":
                    fake["uploads"].append(body); return self.reply(dict(name="start.png", subfolder=""))
                assert self.path == "/prompt"
                data = json.loads(body); fake["submissions"] += 1; job = str(fake["submissions"])
                fake["jobs"][job] = dict(prompt=[0, job, data["prompt"], data["extra_data"]],
                     status=dict(status_str="success", completed=True),
                     outputs={"58": {"images": [dict(filename="test.mp4", subfolder="motion-artist", type="output")]}})
                self.reply(dict(prompt_id=job))

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfy)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        a = SimpleNamespace(prompt="victory fist pump with recovery", title="Victory", image=None,
                            duration=5, seed=1234, comfy_url=f"http://127.0.0.1:{server.server_port}", out=os.path.join(tmp, "text"))
        def rejected(fn):
            try: fn()
            except (ValueError, TimeoutError, urllib.error.URLError): pass
            else: raise AssertionError("invalid operation accepted")
        try:
            info["VAELoader"]["input"]["required"]["vae_name"][0] = []
            rejected(lambda: generate(a)); assert fake["submissions"] == 0
            info["VAELoader"]["input"]["required"]["vae_name"][0] = ["wan2.2_vae.safetensors"]
            saved_info = info.pop("Wan22ImageToVideoLatent")
            rejected(lambda: generate(a)); assert fake["submissions"] == 0
            info["Wan22ImageToVideoLatent"] = saved_info
            generate(a); state = json.load(open(os.path.join(a.out, "generation.json")))
            assert state["settings"]["source_frames"] == 121 and state["video"]["duration"] > 5
            assert state["source_sha256"] == sha256(video) and "start_image" not in state["workflow"]["55"]["inputs"]
            assert local_source(os.path.join(a.out, "source.mp4"))["source_kind"] == "ai-generated"
            with patch(__name__ + ".ensure_comfy", side_effect=AssertionError("completed reuse contacted server")):
                generate(a)
            assert fake["submissions"] == 1
            a.seed = 9; rejected(lambda: generate(a)); a.seed = 1234
            a.out = os.path.join(tmp, "image"); a.image = os.path.join(tmp, "wide.png")
            import cv2, numpy as np
            cv2.imwrite(a.image, np.zeros((100, 400, 3), dtype=np.uint8))
            generate(a); state = json.load(open(os.path.join(a.out, "generation.json")))
            im = cv2.imread(os.path.join(a.out, "start.png"))
            assert im.shape == (864, 576, 3) and np.all(im[0] == 255) and np.all(im[430] == 0)
            assert state["settings"]["input_image_sha256"] == sha256(a.image) and fake["uploads"]
            assert state["workflow"]["55"]["inputs"]["start_image"] == ["56", 0]
            a.image = None; a.out = os.path.join(tmp, "interrupt")
            with patch(__name__ + ".wait_generation", side_effect=KeyboardInterrupt):
                try: generate(a)
                except KeyboardInterrupt: pass
            count = fake["submissions"]; generate(a); assert fake["submissions"] == count
            a.out = os.path.join(tmp, "lost-response")
            real_request = comfy_request
            def lose_response(url, route, *args):
                reply = real_request(url, route, *args)
                if route == "/prompt": raise urllib.error.URLError("lost response")
                return reply
            with patch(__name__ + ".comfy_request", side_effect=lose_response): rejected(lambda: generate(a))
            count = fake["submissions"]; generate(a); assert fake["submissions"] == count
            a.out = os.path.join(tmp, "failure"); fake["mode"] = "failed"
            rejected(lambda: generate(a)); count = fake["submissions"]
            rejected(lambda: generate(a)); assert fake["submissions"] == count
            a.out = os.path.join(tmp, "timeout"); fake["mode"] = "running"
            real_wait = wait_generation
            with patch(__name__ + ".wait_generation", side_effect=lambda u, s, p, **kw: real_wait(u, s, p, timeout=0, poll=0)):
                rejected(lambda: generate(a))
            state_path = os.path.join(a.out, "generation.json"); pending = json.load(open(state_path))
            assert pending["prompt_id"] and pending["status"] == "running"
            fake["mode"] = "queued"
            ticks = iter([0, 1000, 2000])
            with patch(__name__ + ".time.monotonic", side_effect=lambda: next(ticks)), patch(__name__ + ".time.sleep", side_effect=KeyboardInterrupt):
                try: real_wait(a.comfy_url, pending, state_path, timeout=1, poll=0)
                except KeyboardInterrupt: pass
            assert pending["status"] == "queued" and pending["running_seconds"] == 0
            fake["mode"] = "complete"; count = fake["submissions"]
            generate(a); assert fake["submissions"] == count
            a.out = os.path.join(tmp, "unknown")
            with patch(__name__ + ".comfy_request", side_effect=lose_response): rejected(lambda: generate(a))
            count = fake["submissions"]; fake["jobs"] = {}
            rejected(lambda: generate(a)); assert fake["submissions"] == count
            # Completion reuse verifies content instead of silently returning changed footage.
            a.out = os.path.join(tmp, "text")
            with open(os.path.join(a.out, "source.mp4"), "ab") as fh: fh.write(b"changed")
            rejected(lambda: generate(a)); assert fake["submissions"] == count
            link = os.path.join(tmp, "checkout-link"); os.symlink(REPO, link)
            a.out = link
            try: generate(a)
            except SystemExit: pass
            else: raise AssertionError("generation followed a link into checkout")
            a.out = REPO
            try: generate(a)
            except SystemExit: pass
            else: raise AssertionError("generation wrote inside checkout")
            assert source_footer(dict(url="", source_kind="ai-generated", title="<Victory>")) == "Local AI – &lt;Victory&gt;"
            assert '<a href="https://' in source_footer(dict(url="https://youtube.com/watch?v=123"))
            man = bundle_manifest(dict(name="emote-01", title="Victory", fps=12, frame_count=4, view="front",
                    frames=[], playback="one-shot", seam="n/a", source=local_source(os.path.join(tmp, "image", "source.mp4"))), [])
            assert man["schema"] == SCHEMA and man["source"]["generation"]["source_sha256"] == sha256(video)
        finally:
            server.shutdown(); server.server_close(); thread.join()


# ---------------------------------------------------------------- export
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()


def set_name(d):
    """The genre a capture belongs to: its name with the motion number stripped.

    Names are assigned by `clipper`, not derived here. Every motion is `<genre>-NN`, numbered from
    01 within its genre — so `hiphop-07` is the seventh motion filed under `hiphop` and lives in
    `exports/hiphop/hiphop-07/`. The number is the motion's identity, which means a re-cut
    overwrites that bundle instead of landing beside it under a second name, and the genre is in
    the bundle's own name so it survives being copied into a consumer's flat motions/ directory.
    Provenance — url, start second, span — rides in `manifest.json`, which is where a consumer
    reads it; it is no longer spelled into the file name.
    """
    return re.sub(r"-\d+$", "", d["name"]) or d["name"]


def motion_num(name, genre):
    """The NN out of a `<genre>-NN` name, or None if it is not one of this genre's."""
    m = re.fullmatch(re.escape(genre) + r"-(\d+)", name or "")
    return int(m.group(1)) if m else None


def work_root(path):
    """The directory holding `work/` and `exports/`: the parent of the `work` above `path`.

    Found by walking up rather than by counting levels — a capture is
    `work/<creator>-<title>/clip-NN/`, and fixed arithmetic put `exports/` inside `work/`.
    """
    d = os.path.abspath(path)
    while os.path.basename(d) != "work" and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    return os.path.dirname(d) if os.path.basename(d) == "work" else None


def allocate_motion(genre, capture_dir):
    """This capture's `<genre>-NN`, allocated once and then remembered in clips.json.

    The number is not in `clips.json` when the marks are made, because it does not exist
    yet — and the clip's position in that array cannot stand in for it. Numbers are handed
    out across *every* video clipped into a genre: two videos open on `hiphop` produce
    01, 02 in one and 03 in the other, so position 3 of one video is not the third motion
    of that genre.

    So it is allocated here, the first time the clip is cut, and written back beside the
    clip it came from. A re-cut reads it back instead of taking a second number, which is
    what makes the re-export replace that bundle in place rather than land beside it.
    """
    root = work_root(capture_dir)
    if not root:
        sys.exit(f"--genre needs the capture under work/, and {capture_dir} is not")
    cpath = os.path.join(os.path.dirname(os.path.abspath(capture_dir)), "clips.json")
    if not os.path.exists(cpath):
        sys.exit(f"--genre reads the video's clips.json, and there is none at {cpath}")
    doc = json.load(open(cpath))
    rel = os.path.relpath(os.path.abspath(capture_dir), root).replace(os.sep, "/")
    clip = next((c for c in doc.get("clips", []) if c.get("capture") == rel), None)
    if clip is None:
        sys.exit(f"no clip in {cpath} captures into {rel}")
    if clip.get("motion"):
        return clip["motion"]
    if doc.get("genre") and doc["genre"] != genre:
        sys.exit(f"{cpath} files this video under {doc['genre']!r}, not {genre!r}")
    # every number already spoken for: exported as a directory, or claimed by any video's
    # clips.json. A number becomes a directory only on export, so exports/ alone would hand
    # the same 01 to two videos cut in the same genre before either one was exported.
    used = remote_motions(genre)
    gdir = os.path.join(root, "exports", genre)
    if os.path.isdir(gdir):
        used |= {n for d in os.listdir(gdir) if (n := motion_num(d, genre)) is not None}
    wdir = os.path.join(root, "work")
    for vd in (sorted(os.listdir(wdir)) if os.path.isdir(wdir) else []):
        p = os.path.join(wdir, vd, "clips.json")
        if not os.path.exists(p):
            continue
        other = json.load(open(p))
        if other.get("genre") != genre:
            continue
        used |= {n for c in other.get("clips", [])
                 if (n := motion_num(c.get("motion"), genre)) is not None}
    clip["motion"] = f"{genre}-{(max(used) + 1) if used else 1:02d}"
    with open(cpath, "w") as fh:
        json.dump(doc, fh, indent=2)
    return clip["motion"]


def view_counts(d):
    """How many frames face each way — the spread the single `view` throws away.

    `view` is one majority vote over the per-frame classifications, so a set that is half front and
    half three-quarter, or one carrying five rear frames inside a seven-way tie, reports a single
    tidy word. The consumer then screens on a number that cannot support the weight put on it: a
    character was rendered faceless for five of twenty-four frames off a bundle whose declared view
    was perfectly legal.

    Emitting the raw counts costs nothing and cannot break the consumer — CAG ignores every key it
    does not know — and it lets each consumer set its own threshold per character. (Which keys it
    *requires* is deliberately not written down here: that list is in another repo, it has already
    gone stale once in this docstring, and nothing we do depends on knowing it.) That matters because purity is not
    always available to offer: a captioned Two-Step turns in every window the footage contains, so
    a front-only rule would delete a real dance rather than protect anything.
    """
    c = {}
    for f in d.get("frames") or []:
        v = (f.get("features") or {}).get("view")
        if v: c[v] = c.get(v, 0) + 1
    return c or None


def bundle_manifest(d, files):
    """What the motion-director job input references: what the capture is, and a SHA-256 per file.

    One bundle is one animation of any length: CAG chunks it across as many 12-figure renders as it
    needs and assembles one sheet, so a motion is never split across bundles — two bundles are two
    unrelated animations there, each separately proofed and scaled. `seam` therefore means what it
    always did: this capture's last frame against its own first.
    """
    return dict(
        bundle="motion-source", schema=d.get("schema", SCHEMA), name=d["name"], title=d["title"],
        fps=d["fps"], frame_count=d["frame_count"], playback=d["playback"], view=d["view"],
        # Out-and-back playback, declared so a consumer can walk 0..N-1..1 instead of jumping N-1->0.
        # `seam` below still measures the straight loop, deliberately: a consumer that does not
        # implement ping-pong plays that jump, and hiding the ratio behind this flag would hide it.
        pingpong=d.get("pingpong", False),
        seam=d["seam"], seam_ratio=d.get("seam_ratio"),   # the verdict, and the number behind it
        stabilized=d.get("stabilized", False), exaggerate=d.get("exaggerate"),
        performer=d.get("performer"),   # the filmed body, not the character the render must draw
        gender=motion_gender(d.get("gender"), d["name"]),
        view_frames=view_counts(d),   # the spread `view` averages away — see below
        missing_frames=d.get("missing_frames", []), source=d["source"],
        arc_written=bool(d.get("arc", "").strip()),
        files={rel: sha256(p) for rel, p in files})




def pose_grid(a):
    """Grid of every frame's stick figure at identical scale, in one image — a single pose-reference
    to hand an image generator so it draws the whole sprite across every frame in one call, instead
    of frame by frame (which is where style and proportions drift). Writes a `<name>-pose-grid.json`
    sidecar declaring the exact geometry, so a consumer slices by stated numbers instead of
    reverse-engineering the pixels (thresholding, finding bands, measuring gaps)."""
    d = json.load(open(a.json))
    import cv2, numpy as np
    n = len(d["frames"])
    tdir = os.path.join(os.path.dirname(a.json), "thumbs")
    thumbs = [os.path.join(tdir, f"f{f['i']:02d}.jpg") for f in d["frames"]]
    missing = [p for p in thumbs if not os.path.exists(p)]
    if missing:
        sys.exit(f"pose-grid needs one traced frame per motion frame; missing {len(missing)} "
                 f"(first: {os.path.basename(missing[0])}). Re-run extract.")
    imgs = [cv2.imread(p) for p in thumbs]
    cell_w, cell_h = max(i.shape[1] for i in imgs), max(i.shape[0] for i in imgs)
    gap = 8
    label_h = 0 if a.no_labels else 22
    tile_w, tile_h = cell_w + gap, cell_h + label_h + gap
    # Four across and twelve to a sheet mirrored cag's render grid when that grid was twelve. It is
    # now eight there, and cag no longer reads this image at all — it tiles its own from thumbs/ —
    # so the twelve is this tool's number, for handing a generator the whole set in few images. It
    # is stated in the sidecar's per_sheet and overridable with --cols; nothing downstream reads it.
    cols = a.cols or 4
    per_sheet = 12
    # BGR, matching templates/sheet.html's :root — key #FF74A8, pilot #A8A2FF, otherwise --muted.
    role_ink = {"key": (168, 116, 255), "pilot": (255, 162, 168)}
    out = a.out or os.path.join(os.path.dirname(a.json), f"{d['name']}-pose-grid.png")
    stem = out[:-4] if out.endswith(".png") else out
    chunks = [range(s, min(s + per_sheet, n)) for s in range(0, n, per_sheet)]
    written = []
    for c_i, chunk in enumerate(chunks):
        rows = math.ceil(len(chunk) / cols)
        sheet = np.full((rows * tile_h - gap, cols * tile_w - gap, 3), (28, 19, 20), np.uint8)
        for slot, fi in enumerate(chunk):
            f, im = d["frames"][fi], imgs[fi]
            r, c = divmod(slot, cols)
            x, y = c * tile_w, r * tile_h
            if not a.no_labels:
                cv2.putText(sheet, f"{f['i']:02d}", (x + 3, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            role_ink.get(f["role"], (172, 150, 154)), 1, cv2.LINE_AA)
            sheet[y + label_h:y + label_h + im.shape[0], x:x + im.shape[1]] = im
        # one chunk keeps the plain name; several get numbered.
        path = f"{stem}.png" if len(chunks) == 1 else f"{stem}-{c_i:02d}.png"
        cv2.imwrite(path, sheet)
        written.append(path)
    layout = dict(
        cols=cols, rows=math.ceil(min(per_sheet, n) / cols), frame_count=n, scale=1,
        # PNG pixels already. Frame i sits on sheet i//12, at slot i%12: row slot//cols, col
        # slot%cols, tile origin (col*tile_w, row*tile_h) from the top-left, no outer margin. The
        # photograph occupies (0, label_h)-(cell_w, label_h+cell_h) inside that tile; the
        # frame-number band (absent with --no-labels) occupies (0,0)-(*, label_h).
        tile_w=tile_w, tile_h=tile_h, cell_w=cell_w, cell_h=cell_h,
        label_h=label_h, gap=gap, per_sheet=per_sheet, sheets=len(chunks),
        sheet_w=cols * tile_w - gap, sheet_h=math.ceil(min(per_sheet, n) / cols) * tile_h - gap,
        labeled=not a.no_labels, source="thumbs")
    sidecar = f"{stem}.json"
    json.dump(layout, open(sidecar, "w"), indent=1)
    print("\n".join(written) + f"\n{sidecar}")
    print(f"{cols} across, {per_sheet} per image, {len(chunks)} image(s), {n} frames, "
          f"{cell_w}x{cell_h}px cells")
    store_capture(os.path.dirname(os.path.abspath(a.json)), *written, sidecar)
    return written[0]


def export(a):
    """Write motion.json + the sheet + thumbs and a SHA-256 manifest into one uncompressed bundle."""
    jp = a.json
    d = json.load(open(jp))
    src = os.path.dirname(os.path.abspath(jp))
    sheet = a.sheet or os.path.join(src, f"{d['name']}-motion.html")
    if not os.path.exists(sheet):
        sys.exit(f"export: no motion sheet at {sheet} — run `render` first, or pass --sheet")
    # The bundle is named by the capture, and the shipped motion.json already says that name — so
    # nothing is rewritten on the way out and the directory, the manifest and the json agree by
    # construction rather than by a copy that could drift.
    bundle = d["name"]
    files = [("motion.json", jp), (os.path.basename(sheet), sheet)]
    tdir = os.path.join(src, "thumbs")
    if os.path.isdir(tdir):
        files += [(f"thumbs/{n}", os.path.join(tdir, n)) for n in sorted(os.listdir(tdir))
                  if os.path.isfile(os.path.join(tdir, n))]
    # the pose grid is optional — only `pose-grid` produces it, and not every capture needs one —
    # so it rides along when found next to the json rather than requiring its own export flag.
    # glob, not an exact name: a motion longer than one image chunks into -00.png, -01.png … and an
    # exact `<name>-pose-grid.png` matched none of them, so the images silently never reached the
    # bundle while the manifest still described them.
    for p in sorted(glob.glob(os.path.join(src, f"{d['name']}-pose-grid*.png"))):
        files.append((os.path.basename(p), p))
    man = bundle_manifest(d, files)
    # the pose grid's geometry (cols/rows/tile pitch/label band) rides in the manifest too, not
    # just as a sidecar file, so a consumer slices the PNG by declared numbers instead of measuring
    # pixels back out of it.
    layout_p = os.path.join(src, f"{d['name']}-pose-grid.json")
    if os.path.exists(layout_p): man["pose_grid"] = json.load(open(layout_p))
    # Bundles land in exports/<genre>/<genre>-NN/, beside work/ and never in the capture dir: one
    # place to hand off from, one directory per genre. The directory is the bundle's name and
    # nothing else — frame count and fps used to ride in it, which meant a re-cut at a different
    # rate landed beside the old bundle instead of replacing it, and both stayed installable.
    # exports/ is the sibling of the `work` directory the capture sits under (work_root): a
    # capture is work/<creator>-<title>/clip-NN/ now, and counting levels put exports/ in work/.
    root = None if a.out else work_root(src)
    if not a.out and not root:
        sys.exit(f"export: {src} is not under work/, so there is no exports/ beside it — "
                 f"pass --out to say where the bundle goes")
    out = outside_repo(a.out or os.path.join(root, "exports", set_name(d), bundle))
    # A plain directory, not a zip: the consumer reads thumbs/ frame by frame, so compressing them
    # only to have them unpacked again bought nothing. The layout is what unzipping used to give,
    # minus the redundant <bundle>/ level the archive needed to avoid spilling on extract.
    # Re-export replaces the bundle in place, so clear the old one first — a shorter re-cut would
    # otherwise leave stale thumbs behind, and a thumb count past frame_count silently costs the
    # consumer the whole pose reference.
    if os.path.isdir(out): shutil.rmtree(out)
    for rel, p in files:
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(p, dst)
    if "gender" not in d:
        # Legacy inputs export an explicit unknown in both copies of the contract.
        atomic_json(os.path.join(out, "motion.json"), dict(d, gender=man["gender"]))
        man["files"]["motion.json"] = sha256(os.path.join(out, "motion.json"))
    # The clip, mask and head boxes ride beside the files, not in `files`: cag git-ignores footage
    # and commits the manifest, so each is hashed in the `clip` block instead of the file list.
    clip = d.get("clip")
    if clip:
        parts = [clip] + [clip[k] for k in ("mask", "heads") if clip.get(k)]
        lost = [b["file"] for b in parts if not os.path.exists(os.path.join(src, b["file"]))]
        if lost:
            print(f"warning: motion.json names {', '.join(lost)} but the capture has none — re-extract")
        else:
            for b in parts: shutil.copy2(os.path.join(src, b["file"]), os.path.join(out, b["file"]))
            def dig(b): return sha256(os.path.join(out, b["file"]))
            man["clip"] = dict(clip, sha256=dig(clip),
                               **{k: dict(clip[k], sha256=dig(clip[k])) for k in ("mask", "heads") if clip.get(k)})
    manifest_p = os.path.join(out, "manifest.json")
    json.dump(man, open(manifest_p, "w"), indent=1)
    ratio = man["seam_ratio"]
    # A directory has no digest of its own. The manifest carries a SHA-256 per file, so its own
    # digest covers every one of them transitively — that is the pair a job input references now.
    print(f"{out}\nmanifest sha256 {sha256(manifest_p)} | {len(files) + 1} files | {d['frame_count']}f @ "
          f"{d['fps']}fps | {d['playback']} | view {d['view']} | seam {man['seam']}"
          f"{f' ({ratio:.2f}x median step)' if ratio is not None else ''}")
    if not man["arc_written"]:
        print("warning: arc is empty — write the performance arc before hand-off")
    if man["missing_frames"]:
        print(f"warning: frames with no pose: {man['missing_frames']}")
    # CAG builds its pose reference from the thumbs, one figure per thumb in name order, so a gap
    # (a frame with no pose) or a leftover from an earlier, longer capture slides figure n off
    # frame n and every per-frame note with it.
    thumbs = [rel for rel, _ in files if rel.startswith("thumbs/")]
    if len(thumbs) != d["frame_count"]:
        print(f"warning: {len(thumbs)} thumbs for {d['frame_count']} frames — CAG reads them as the "
              f"pose reference, one per frame. Clear {tdir} and re-extract.")
    # The Drive is where a motion is stored, so a bundle that did not reach it is not exported.
    # Re-running `export` retries the push.
    rel = f"{set_name(d)}/{os.path.basename(os.path.normpath(out))}"
    stored([publish(out, rel)], out)
    if REMOTE: print(f"stored {REMOTE}/{rel}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("prompt")
    g.add_argument("--title", required=True); g.add_argument("--out", required=True)
    g.add_argument("--image"); g.add_argument("--duration", type=float, default=5)
    g.add_argument("--seed", type=int, default=1234)
    g.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    e = sub.add_parser("extract"); e.add_argument("source")
    e.add_argument("--fps", type=int, required=True); e.add_argument("--frames", type=int, required=True)
    e.add_argument("--start", type=tstamp, help="trim: seconds or m:ss"); e.add_argument("--end", type=tstamp, help="trim: seconds or m:ss")
    e.add_argument("--name"); e.add_argument("--out")
    e.add_argument("--genre", help="allocate this capture's name as <genre>-NN against "
                                   "exports/<genre>/ and record it in the video's clips.json; "
                                   "needs --out and replaces --name")
    e.add_argument("--url", help="origin URL, when `source` is a local copy of it: the manifest is "
                                 "the only place a bundle's provenance lives")
    e.add_argument("--exaggerate", type=float, default=1.25, help="motion amplification about the mean pose (1.0 = as filmed)")
    e.add_argument("--stabilize", action="store_true", help="centre hips horizontally each frame (moving camera / travelling performer)")
    e.add_argument("--window", type=tstamp, help="source seconds the search looks for (default frames/fps); the winner is stretched onto the frame count")
    e.add_argument("--margin", type=tstamp, default=0.5, help="slack in seconds around --start and --end: probe both ends for the move's own cut (loop: matching poses; one-shot: the stillest ending) and stretch the winner onto the frame count. 0 uses the span exactly as given")
    e.add_argument("--search", action="store_true", help="slide a frames/fps-second window over --start..--end and pick the tightest loop")
    e.add_argument("--playback", choices=["loop", "one-shot", "final-hold"], default="loop")
    e.add_argument("--pingpong", action="store_true", help="the capture plays out and back (0..N-1..1), so the seam is the motion reversed. Recorded in the manifest; `seam` still measures the straight loop a consumer without ping-pong will play")
    e.add_argument("--performer", choices=["female", "male"], help="the filmed performer's body, carried into the manifest; omit when it should not be stated")
    e.add_argument("--gender", choices=[*GENDERS, "unclassified"],
                   help="user-assigned character compatibility; unclassified writes null. "
                        "Defaults to the saved clip or previous capture; never inferred from performer")
    r = sub.add_parser("render"); r.add_argument("json"); r.add_argument("--out")
    r.add_argument("--template", help=f"sheet template to render into (default {TEMPLATE_PATH})")
    sp = sub.add_parser("pose-grid"); sp.add_argument("json"); sp.add_argument("--out")
    sp.add_argument("--cols", type=int, help="grid columns (default: near-square given the figure's own aspect)")
    sp.add_argument("--no-labels", action="store_true", help="omit the per-cell frame-number text (nothing for an image generator to copy into the art)")
    x = sub.add_parser("export"); x.add_argument("json"); x.add_argument("--out")
    x.add_argument("--sheet", help="motion sheet HTML (default <name>-motion.html beside the json)")
    t = sub.add_parser("trace"); t.add_argument("images"); t.add_argument("out")
    sub.add_parser("selftest")
    a = ap.parse_args()
    # Every command runs from HOME, so work/ and exports/ are HOME's and nothing lands in the
    # checkout. A path given on the command line that exists here is an input and is made absolute
    # first; any other relative path is read under HOME.
    for k in ("source", "json", "template", "sheet", "images", "image"):
        v = getattr(a, k, None)
        if v and os.path.exists(v): setattr(a, k, os.path.abspath(v))
    if a.cmd == "trace": a.out = os.path.abspath(a.out)   # trace writes cag's file, where cag says
    if a.cmd != "selftest":
        os.makedirs(HOME, exist_ok=True); os.chdir(HOME)
    {"generate": generate, "extract": extract, "render": render, "pose-grid": pose_grid, "export": export, "trace": trace,
     "selftest": lambda _: selftest()}[a.cmd](a)


if __name__ == "__main__":
    main()
