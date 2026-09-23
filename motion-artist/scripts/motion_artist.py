#!/usr/bin/env python3
"""motion_artist: turn a video of a person moving into a frame-by-frame motion source.

  motion_artist.py extract URL|FILE --fps N --frames N [--start S] [--end S] [--name SLUG]
                   [--playback loop|one-shot|final-hold] [--pingpong] [--out DIR]
  motion_artist.py render DIR/motion.json [--out FILE.html] [--pingpong] [--template FILE]
  motion_artist.py export DIR/motion.json [--out FILE.zip] [--sheet FILE.html]
  motion_artist.py selftest

`extract` writes DIR/motion.json (+ DIR/thumbs/*.jpg) and prints a compact frame table.
`render` turns motion.json into a self-contained HTML motion sheet.
`export` bundles the json, sheet and thumbs with a SHA-256 manifest for hand-off.
Between extract and render, an agent may fill `arc`, `title` and per-frame `note` in motion.json.
"""
import argparse, base64, glob, hashlib, html, json, math, os, re, subprocess, sys, urllib.request, zipfile

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task")
MODEL_PATH = os.path.expanduser("~/.cache/motion-artist/pose_landmarker_lite.task")

# Bump when a field changes meaning or disappears, so a consumer fails loudly instead of mis-parsing.
SCHEMA = "motion-artist/2"

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
    """
    os.makedirs(out_dir, exist_ok=True)
    tmpl = os.path.join(out_dir, "source-%(id)s.%(ext)s")
    r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--no-simulate",
                        "-f", "bv*[ext=mp4]/bv*/b", "-S", "res:720",
                        "--print", "after_move:%(filepath)s\t%(title)s", "-o", tmpl, url],
                       capture_output=True, text=True, check=True)
    path, title = r.stdout.strip().splitlines()[-1].split("\t", 1)
    return path, title


def landmarker():
    import mediapipe as mp
    from mediapipe.tasks import python as mpt
    from mediapipe.tasks.python import vision
    if not os.path.exists(MODEL_PATH):
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    opts = vision.PoseLandmarkerOptions(base_options=mpt.BaseOptions(model_asset_path=MODEL_PATH, delegate=mpt.BaseOptions.Delegate.CPU),
                                        running_mode=vision.RunningMode.IMAGE, num_poses=1)
    return mp, vision.PoseLandmarker.create_from_options(opts)


def portable(p):
    """A bundle is handed to other machines: never bake an absolute home path into it."""
    ap = os.path.abspath(p)
    return os.path.relpath(ap) if ap.startswith(os.getcwd() + os.sep) else os.path.basename(p)


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
    out = a.out or os.path.join("work", a.name or "motion")
    os.makedirs(os.path.join(out, "thumbs"), exist_ok=True)
    if re.match(r"https?://", a.source):
        src, title = fetch(a.source, out)
    else:
        src, title = a.source, os.path.basename(a.source)
    name = a.name or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "motion"

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

    frames, missing = [], []
    for i, t in enumerate(sample_times(start, span, a.frames)):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, bgr = cap.read()
        if not ok: missing.append(i); continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.pose_landmarks: missing.append(i); continue
        img, wld = res.pose_landmarks[0], res.pose_world_landmarks[0]
        P = {k: [img[j].x * Wpx / Hpx, img[j].y] for k, j in LM.items()}   # aspect-corrected, y down
        W = {k: [wld[j].x, wld[j].y, wld[j].z] for k, j in LM.items()}
        th = cv2.resize(bgr, (200, int(200 * Hpx / Wpx)))
        # quality 60 put JPEG artefacts on the limb edges, which is the one thing CAG's generator
        # reads off these: they are its pose reference, not a preview. 200px is the width every
        # amplitude measurement was taken at, and CAG pastes them at native size, so it stays.
        cv2.imwrite(os.path.join(out, "thumbs", f"f{i:02d}.jpg"), th, [cv2.IMWRITE_JPEG_QUALITY, 88])
        frames.append(dict(i=i, t=round(t, 3), P=P, W=W))
    if len(frames) < 2:
        sys.exit(f"pose not found in enough frames (missing {missing}); try --start/--end on a clearer span")

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
        # `--url` matters more than it looks: since a bundle is named `<set>-<index>`, the manifest
        # is the *only* place the origin survives. Cutting several moves out of one video means
        # working from a downloaded copy — re-fetching per move would download it a dozen times —
        # and without this the capture would record a local path where the provenance should be.
        source=dict(url=a.source if re.match(r"https?://", a.source) else (a.url or portable(a.source)),
                    file=portable(src), title=title, start=start, end=round(end, 3),
                    speed_factor=round(speed, 2), duration=round(dur, 2)),
        exaggerate=a.exaggerate, stabilized=a.stabilize, performer=a.performer,
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
    jp = os.path.join(out, "motion.json")
    kept = carry_over_writing(jp, doc)
    json.dump(doc, open(jp, "w"), indent=1)

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
    for f in d["frames"]: f.setdefault("src", f["i"])   # which source frame each cell draws from
    # The sheet holds exactly the frames the animation was specified with — one cell each, no more.
    # Reviewing a seam is a playback question, not a drawing count: the player already loops forever,
    # and --pingpong only changes the order it walks the same cells in.
    if (a.pingpong or d.get("pingpong")) and len(d["frames"]) > 2:
        d["pingpong"] = True
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
    n = len(d["frames"]); lap = n / d["fps"]
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
        N=str(n), FPS=str(d["fps"]), LAP=f"{lap:.2f} s", PLAYBACK=d["playback"] + (" · out and back" if d.get("pingpong") else ""), VIEW=html.escape(d["view"]),
        SEAM=("clean — the return leg reverses the out leg" if d.get("pingpong") else d["seam"]), KEYS=", ".join(map(str, keys)) or "—", PILOTS=", ".join(map(str, pilots)) or "—",
        URL=html.escape(src["url"]), ARC=arc, ROWS=rows,
        RATES="".join(f'<button data-fps="{r}" aria-pressed="{str(r == d["fps"]).lower()}">{r} fps</button>' for r in rates),
        THUMBS=json.dumps(thumbs), DATA=json.dumps(payload).replace("</", "<\\/"),
    ).items():
        page = page.replace("{{" + k + "}}", v)
    open(out, "w").write(page)
    print(out)
    return out


# The sheet's markup, styling and player live in templates/sheet.html next to this script, so the
# design can be edited (or replaced with --template) without touching the extractor.
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "templates", "sheet.html")


# ---------------------------------------------------------------- selftest
def tstamp(v):
    """'83', '1:23', '1:23.5' -> seconds."""
    parts = [float(x) for x in str(v).split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def selftest():
    """Synthetic poses: arm overhead + one foot lifted must be described as such."""
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
    # the set is the name minus the move index, and a name without one is its own set
    assert set_name({"name": "hip-hop-1-3"}) == "hip-hop-1"
    assert set_name({"name": "shuffle-2-11"}) == "shuffle-2"
    assert set_name({"name": "dougie"}) == "dougie"
    assert portable(os.path.join(os.getcwd(), "work", "x.mp4")) == os.path.join("work", "x.mp4")
    assert portable("/somewhere/else/x.mp4") == "x.mp4"
    print("selftest ok")


# ---------------------------------------------------------------- export
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()


def set_name(d):
    """The set a capture belongs to: its name with the move index stripped.

    Names are assigned here, not derived. Each source video is given a set name, and every unique
    move cut out of it is `<set>-<index>`, index from 1 — so `hip-hop-1-3` is the third move of the
    `hip-hop-1` set and lives in `exports/hip-hop-1/`. The name is the identifier, which means a
    re-cut of a move overwrites that move instead of landing beside it under a different trace.
    Provenance — url, start second, span — rides in `manifest.json`, which is where a consumer
    reads it; it is no longer spelled into the file name.
    """
    return re.sub(r"-\d+$", "", d["name"]) or d["name"]


def view_counts(d):
    """How many frames face each way — the spread the single `view` throws away.

    `view` is one majority vote over the per-frame classifications, so a set that is half front and
    half three-quarter, or one carrying five rear frames inside a seven-way tie, reports a single
    tidy word. The consumer then screens on a number that cannot support the weight put on it: a
    character was rendered faceless for five of twenty-four frames off a bundle whose declared view
    was perfectly legal.

    Emitting the raw counts costs nothing and cannot break the consumer — CAG requires exactly
    fps, frame_count, playback, view and files and ignores every other key (`cag/motion.py`) — and
    it lets each consumer set its own threshold per character. That matters because purity is not
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
    return written[0]


def export(a):
    """Zip motion.json + the sheet + thumbs with a SHA-256 manifest, ready for KP-Graphics."""
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
    # Bundles land in exports/<set>/, beside work/ and never in the capture dir: one place to hand
    # off from, one directory per source video. The file name still carries frame count and fps —
    # a re-cut at a different rate is a different animation from the same move.
    exports = os.path.join(os.path.dirname(os.path.dirname(src)), "exports", set_name(d))
    out = a.out or os.path.join(exports, f"{bundle}-{d['frame_count']}f-{d['fps']}fps-motion-source.zip")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, p in files: z.write(p, f"{bundle}/{rel}")
        z.writestr(f"{bundle}/manifest.json", json.dumps(man, indent=1))
    ratio = man["seam_ratio"]
    print(f"{out}\nsha256 {sha256(out)} | {len(files) + 1} files | {d['frame_count']}f @ "
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("source")
    e.add_argument("--fps", type=int, required=True); e.add_argument("--frames", type=int, required=True)
    e.add_argument("--start", type=tstamp, help="trim: seconds or m:ss"); e.add_argument("--end", type=tstamp, help="trim: seconds or m:ss")
    e.add_argument("--name"); e.add_argument("--out")
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
    r = sub.add_parser("render"); r.add_argument("json"); r.add_argument("--out")
    r.add_argument("--template", help=f"sheet template to render into (default {TEMPLATE_PATH})")
    r.add_argument("--pingpong", action="store_true", help="walk the frames out and back (0..N-1..1) so the seam is the motion reversed")
    sp = sub.add_parser("pose-grid"); sp.add_argument("json"); sp.add_argument("--out")
    sp.add_argument("--cols", type=int, help="grid columns (default: near-square given the figure's own aspect)")
    sp.add_argument("--no-labels", action="store_true", help="omit the per-cell frame-number text (nothing for an image generator to copy into the art)")
    x = sub.add_parser("export"); x.add_argument("json"); x.add_argument("--out")
    x.add_argument("--sheet", help="motion sheet HTML (default <name>-motion.html beside the json)")
    sub.add_parser("selftest")
    a = ap.parse_args()
    {"extract": extract, "render": render, "pose-grid": pose_grid, "export": export,
     "selftest": lambda _: selftest()}[a.cmd](a)


if __name__ == "__main__":
    main()
