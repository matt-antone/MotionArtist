#!/usr/bin/env python3
"""motion_artist: turn a video of a person moving into a frame-by-frame motion source.

  motion_artist.py extract URL|FILE --fps N --frames N [--start S] [--end S] [--name SLUG]
                   [--playback loop|one-shot|final-hold] [--out DIR]
  motion_artist.py render DIR/motion.json [--out FILE.html] [--pingpong] [--template FILE]
  motion_artist.py export DIR/motion.json [--out FILE.zip] [--sheet FILE.html]
  motion_artist.py selftest

`extract` writes DIR/motion.json (+ DIR/thumbs/*.jpg) and prints a compact frame table.
`render` turns motion.json into a self-contained HTML motion sheet.
`export` bundles the json, sheet and thumbs with a SHA-256 manifest for hand-off.
Between extract and render, an agent may fill `arc`, `title` and per-frame `note` in motion.json.
"""
import argparse, base64, hashlib, html, json, math, os, re, subprocess, sys, urllib.request, zipfile

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
             "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task")
MODEL_PATH = os.path.expanduser("~/.cache/motion-artist/pose_landmarker_lite.task")

# Bump when a field changes meaning or disappears, so a consumer fails loudly instead of mis-parsing.
SCHEMA = "motion-artist/2"

# MediaPipe pose indices. "L"/"R" are the person's own sides == character-left / character-right.
LM = dict(nose=0, eyeL=2, eyeR=5, earL=7, earR=8, shL=11, shR=12, elL=13, elR=14, wrL=15, wrR=16,
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
    arms = {}
    for s, name in (("L", "character-left"), ("R", "character-right")):
        wr, sh = P["wr" + s], P["sh" + s]
        if wr[1] < P["nose"][1] - 0.02 * body_h: h = "overhead"
        elif wr[1] < sh[1] - 0.03 * body_h: h = "raised above shoulder"
        elif wr[1] < hip_mid[1] - 0.03 * body_h: h = "at chest/waist height"
        else: h = "low by the hip"
        bend = angle3(W["sh" + s], W["el" + s], W["wr" + s])
        # wrist crossing torso midline (image x), only meaningful when facing camera-ish
        cross = ""
        if front_ish:
            other = P["shR" if s == "L" else "shL"]
            if (wr[0] - other[0]) * (sh[0] - other[0]) < 0 and dist(wr, sh_mid) < 1.2 * sh_w:
                cross = ", crossing the body" + {"near": " in front of the torso",
                                                 "far": " behind the torso"}.get(nearness(zl["arm" + s]), "")
        arms[s] = dict(height=h, elbow=round(bend))
        f["arm_" + s] = f"{name} arm {h}, elbow {bend_word(bend)}{cross}"

    # legs / weight / airborne
    anL, anR = P["anL"], P["anR"]
    lift = 0.045 * body_h
    plantedL, plantedR = anL[1] > floor_y - lift, anR[1] > floor_y - lift
    kneeL, kneeR = angle3(W["hipL"], W["knL"], W["anL"]), angle3(W["hipR"], W["knR"], W["anR"])
    f["knee_L"], f["knee_R"] = round(kneeL), round(kneeR)
    f["airborne"] = not plantedL and not plantedR
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
    stance = abs(anL[0] - anR[0]) / sh_w
    f["stance"] = "feet together" if stance < 0.5 else "shoulder-width stance" if stance < 1.4 else "wide stance"
    legs = []
    for s, name in (("L", "character-left"), ("R", "character-right")):
        k = kneeL if s == "L" else kneeR
        if k < 150: legs.append(f"{name} knee {bend_word(k)}")
    f["legs"] = ", ".join(legs) if legs else "legs straight"
    # legs overlapping in the image: a flat skeleton cannot show which is nearer, so say it in words
    crossed = (anL[0] - anR[0]) * (P["hipL"][0] - P["hipR"][0]) < 0
    dz = zl["legL"] - zl["legR"]
    f["overlap"] = ""
    # ponytail: "overlapping" == ankles closer than about a thigh width; widen if legs read as apart
    if (crossed or abs(anL[0] - anR[0]) < 0.12 * body_h) and abs(dz) > z_unit:
        back, front = ("character-left", "character-right") if dz > 0 else ("character-right", "character-left")
        f["overlap"] = f"The {back} leg passes behind the {front}"

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
        if abs(d) < 7 or not front_ish and view != "back": return ""
        return f"{'character-right' if d > 0 else 'character-left'} {what} raised"
    f["hips"] = tilt(P["hipL"], P["hipR"], "hip")
    f["shoulders"] = tilt(P["shL"], P["shR"], "shoulder")

    # head turn
    hx = (P["nose"][0] - sh_mid[0]) / sh_w
    if front_ish and abs(hx) > 0.28: f["head"] = f"head turned {screen_to_char(hx < 0)}"
    elif view == "back": f["head"] = "head away from camera"
    else: f["head"] = "head forward"

    cue = ". ".join(x for x in [
        f["weight"][0].upper() + f["weight"][1:], f["stance"], f["legs"], f["overlap"],
        f["arm_L"], f["arm_R"], f["torso"], f["hips"], f["shoulders"], f["head"]] if x) + "."
    return f, cue, depth


# ---------------------------------------------------------------- extract
def fetch(url, out_dir):
    """Download with yt-dlp (<=720p mp4). Returns (path, title)."""
    os.makedirs(out_dir, exist_ok=True)
    tmpl = os.path.join(out_dir, "source-%(id)s.%(ext)s")
    r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--no-simulate",
                        "-f", "bv*[height<=720][ext=mp4]/b[height<=720]/b",
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
    end = a.end if a.end is not None and not a.search else min(dur, start + win)
    if a.search: end = start + win
    span = end - start
    # loop: samples exclusive of `end` so the last->first cut is one natural step
    step = span / a.frames if a.playback == "loop" else span / max(a.frames - 1, 1)
    speed = span / (a.frames / a.fps)  # 1.0 == real time; 2.0 == source played at 2x

    frames, missing = [], []
    for i in range(a.frames):
        t = start + i * step
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
        cv2.imwrite(os.path.join(out, "thumbs", f"f{i:02d}.jpg"), th, [cv2.IMWRITE_JPEG_QUALITY, 60])
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

    # clip-wide floor and body height; per-frame features
    floor = sorted(max(f["P"]["anL"][1], f["P"]["anR"][1]) for f in frames)[int(0.85 * (len(frames) - 1))]
    body_h = sorted(floor - min(f["P"]["earL"][1], f["P"]["earR"][1]) for f in frames)[len(frames) // 2]
    for f in frames:
        # a moving camera has no fixed floor: with --stabilize the lower ankle counts as planted
        fl = max(f["P"]["anL"][1], f["P"]["anR"][1]) if a.stabilize else floor
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
        source=dict(url=a.source if re.match(r"https?://", a.source) else portable(a.source),
                    file=portable(src), title=title, start=start, end=round(end, 3),
                    speed_factor=round(speed, 2), duration=round(dur, 2)),
        exaggerate=a.exaggerate, stabilized=a.stabilize,
        fps=a.fps, frame_count=a.frames, playback=a.playback, view=view,
        seam=("clean" if seam < 1.5 else "needs blend") if loop else "n/a",
        missing_frames=missing, arc="",
        frames=[dict(i=f["i"], t=f["t"], role=f["role"], pace=f["pace"], energy=f["energy"],
                     cue=f["cue"], note="", features=f["features"], depth=f["depth"],
                     pts={k: [round(v[0], 4), round(v[1], 4), round(zof(f, k), 4)]
                          for k, v in f["P"].items()})
                for f in frames],
        floor_y=round(floor, 4), body_h=round(body_h, 4))
    jp = os.path.join(out, "motion.json")
    json.dump(doc, open(jp, "w"), indent=1)

    print(f"{jp}\n{title} | span {start:.2f}-{end:.2f}s | {a.frames}f @ {a.fps}fps | {a.playback} | "
          f"view {view} | source speed x{speed:.2f} | seam {doc['seam']} | missing {missing}")
    for f in doc["frames"]:
        print(f"{f['i']:>3} {f['t']:6.2f}s {f['role']:<9} {f['pace']:<6} {f['cue']}")
    return jp


def best_loop(cap, mp, lm, t0, t1, length, aspect, hz=8):
    """Slide a `length`-second window over [t0, t1]; return (start, end, info) minimising the pose
    distance between window start and window end while keeping real motion inside the window."""
    import cv2
    poses, ts = [], []
    t = t0
    while t <= t1 + 1e-9:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, bgr = cap.read()
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))) if ok else None
        if res and res.pose_landmarks:
            img = res.pose_landmarks[0]
            P = {k: [img[j].x * aspect, img[j].y] for k, j in LM.items()}
            hip, h = mid(P["hipL"], P["hipR"]), max(dist(mid(P["shL"], P["shR"]), mid(P["hipL"], P["hipR"])), 1e-3)
            poses.append({k: [(v[0] - hip[0]) / h, (v[1] - hip[1]) / h] for k, v in P.items()})  # hip-centred, torso-scaled
        else:
            poses.append(None)
        ts.append(t); t += 1.0 / hz
    def pd(a, b): return sum(dist(a[k], b[k]) for k in CORE) / len(CORE)
    n = round(length * hz)
    cands = []
    for i in range(len(poses) - n):
        win = poses[i:i + n + 1]
        if any(p is None for p in win): continue
        seam = pd(win[0], win[-1])
        energy = sum(pd(win[j], win[j + 1]) for j in range(n)) / n
        cands.append(dict(t=ts[i], seam=seam, energy=energy))
    if not cands: sys.exit("search: no fully-tracked window; try a different range")
    med = sorted(c["energy"] for c in cands)[len(cands) // 2]
    live = [c for c in cands if c["energy"] >= med] or cands   # ponytail: 'best' = tightest seam among the livelier half
    best = min(live, key=lambda c: c["seam"])
    best["top"] = sorted(live, key=lambda c: c["seam"])[:5]
    return best["t"], best["t"] + length, best


# ---------------------------------------------------------------- render
def figure_svg(pts, box, body_scale, color, accent=None, label=""):
    """Stick figure from image-space points. box = (x0, y0, w, h) of the clip's figure bounds."""
    x0, y0, w, h = box
    s = 200 / h
    def X(p): return (p[0] - x0) * s + 10
    def Y(p): return (p[1] - y0) * s + 10
    L = []
    def z(k): return pts[k][2] if len(pts[k]) > 2 else 0.0   # schema 1 had no depth: flat is fine
    hm, sm = mid(pts["hipL"], pts["hipR"]), mid(pts["shL"], pts["shR"])
    segs = [(pts[a], pts[b], 5, (z(a) + z(b)) / 2) for a, b in BONES]   # (a, b, stroke, depth)
    segs.append((hm, sm, 6, (z("hipL") + z("hipR") + z("shL") + z("shR")) / 4))
    for pa, pb, sw, _ in sorted(segs, key=lambda s: -s[3]):   # far bones first, near ones drawn over
        L.append(f'<line x1="{X(pa):.1f}" y1="{Y(pa):.1f}" x2="{X(pb):.1f}" y2="{Y(pb):.1f}" '
                 f'stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>')
    hc = mid(pts["earL"], pts["earR"])
    r = 0.07 * body_scale * s
    L.append(f'<circle cx="{X(hc):.1f}" cy="{Y(hc):.1f}" r="{r:.1f}" fill="none" stroke="{color}" stroke-width="5"/>')
    # face centre line: hangs from the head centre to the rim, leaning toward the nose side,
    # so front = vertical, turned = tilted, profile = horizontal toward the face
    ear_w = max(abs(X(pts["earR"]) - X(pts["earL"])), 1e-6)
    dx = max(-r, min(r, (X(pts["nose"]) - X(hc)) / ear_w * 2 * r))
    L.append(f'<line x1="{X(hc):.1f}" y1="{Y(hc):.1f}" x2="{X(hc) + dx:.1f}" y2="{Y(hc) + math.sqrt(r * r - dx * dx):.1f}" '
             f'stroke="{accent or color}" stroke-width="3" stroke-linecap="round"/>')
    # character-right limbs get a hollow marker so sides read at a glance
    for k in ("wrR", "anR"):
        L.append(f'<circle cx="{X(pts[k]):.1f}" cy="{Y(pts[k]):.1f}" r="4.5" fill="var(--paper)" '
                 f'stroke="{accent or color}" stroke-width="2.5"/>')
    vw = w * s + 20   # w is the clip box width, unpacked at the top
    return (f'<svg viewBox="0 0 {vw:.0f} 220" role="img" aria-label="{html.escape(label)}">'
            f'<line x1="0" y1="{Y([0, FLOOR]):.1f}" x2="{vw:.0f}" y2="{Y([0, FLOOR]):.1f}" '
            f'stroke="{color}" stroke-width="1" opacity=".35" stroke-dasharray="3 4"/>{"".join(L)}</svg>')


FLOOR = 0.0  # set by render() before figure_svg is called


def render(a):
    global FLOOR
    d = json.load(open(a.json))
    for f in d["frames"]: f.setdefault("src", f["i"])   # which source frame each cell draws from
    # The sheet holds exactly the frames the animation was specified with — one cell each, no more.
    # Reviewing a seam is a playback question, not a drawing count: the player already loops forever,
    # and --pingpong only changes the order it walks the same cells in.
    if a.pingpong and len(d["frames"]) > 2:
        d["pingpong"] = True
    out = a.out or os.path.join(os.path.dirname(a.json), f"{d['name']}-motion.html")
    tdir = os.path.join(os.path.dirname(a.json), "thumbs")
    FLOOR = d["floor_y"]
    xs = [v[0] for f in d["frames"] for v in f["pts"].values()]
    ys = [v[1] for f in d["frames"] for v in f["pts"].values()] + [FLOOR]
    pad = 0.08 * d["body_h"]
    box = (min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)
    rates = sorted({1, 4, d["fps"]})

    figs, thumbs = [], []
    for f in d["frames"]:
        col = {"key": "var(--step)", "pilot": "var(--tap)"}.get(f["role"], "var(--fig)")
        figs.append(figure_svg(f["pts"], box, d["body_h"], "var(--fig)", col, f"Frame {f['i']}: {f['cue']}"))
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
        FIGS=json.dumps(figs), THUMBS=json.dumps(thumbs), DATA=json.dumps(payload).replace("</", "<\\/"),
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
    assert bend_word(170) == "straight" and bend_word(80) == "bent ~90°"
    assert tstamp("1:23.5") == 83.5 and tstamp("7") == 7
    man = bundle_manifest(dict(name="t", title="T", fps=4, frame_count=2, playback="loop", view="front",
                               seam="clean", source={}, arc="  "), [("motion.json", __file__)])
    assert man["files"]["motion.json"] == sha256(__file__) and len(man["files"]["motion.json"]) == 64
    assert man["arc_written"] is False and man["bundle"] == "motion-source"
    assert portable(os.path.join(os.getcwd(), "work", "x.mp4")) == os.path.join("work", "x.mp4")
    assert portable("/somewhere/else/x.mp4") == "x.mp4"
    print("selftest ok")


# ---------------------------------------------------------------- export
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()


def bundle_manifest(d, files):
    """What the motion-director job input references: what the capture is, and a SHA-256 per file."""
    return dict(
        bundle="motion-source", schema=d.get("schema", SCHEMA), name=d["name"], title=d["title"],
        fps=d["fps"], frame_count=d["frame_count"], playback=d["playback"], view=d["view"],
        seam=d["seam"], stabilized=d.get("stabilized", False), exaggerate=d.get("exaggerate"),
        missing_frames=d.get("missing_frames", []), source=d["source"],
        arc_written=bool(d.get("arc", "").strip()),
        files={rel: sha256(p) for rel, p in files})


def export(a):
    """Zip motion.json + the sheet + thumbs with a SHA-256 manifest, ready for KP-Graphics."""
    d = json.load(open(a.json))
    src = os.path.dirname(os.path.abspath(a.json))
    sheet = a.sheet or os.path.join(src, f"{d['name']}-motion.html")
    if not os.path.exists(sheet):
        sys.exit(f"export: no motion sheet at {sheet} — run `render` first, or pass --sheet")
    files = [("motion.json", os.path.abspath(a.json)), (os.path.basename(sheet), sheet)]
    tdir = os.path.join(src, "thumbs")
    if os.path.isdir(tdir):
        files += [(f"thumbs/{n}", os.path.join(tdir, n)) for n in sorted(os.listdir(tdir))
                  if os.path.isfile(os.path.join(tdir, n))]
    man = bundle_manifest(d, files)
    # Bundles land in exports/ beside work/, not in the capture dir: one place to hand off from. The
    # name carries frame count and fps — exports/ is flat, and two cuts of one move differ only there.
    exports = os.path.join(os.path.dirname(os.path.dirname(src)), "exports")
    out = a.out or os.path.join(exports, f"{d['name']}-{d['frame_count']}f-{d['fps']}fps-motion-source.zip")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, p in files: z.write(p, f"{d['name']}/{rel}")
        z.writestr(f"{d['name']}/manifest.json", json.dumps(man, indent=1))
    print(f"{out}\nsha256 {sha256(out)} | {len(files) + 1} files | {d['frame_count']}f @ {d['fps']}fps | "
          f"{d['playback']} | view {d['view']} | seam {d['seam']}")
    if not man["arc_written"]:
        print("warning: arc is empty — write the performance arc before hand-off")
    if man["missing_frames"]:
        print(f"warning: frames with no pose: {man['missing_frames']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("source")
    e.add_argument("--fps", type=int, required=True); e.add_argument("--frames", type=int, required=True)
    e.add_argument("--start", type=tstamp, help="trim: seconds or m:ss"); e.add_argument("--end", type=tstamp, help="trim: seconds or m:ss")
    e.add_argument("--name"); e.add_argument("--out")
    e.add_argument("--exaggerate", type=float, default=1.25, help="motion amplification about the mean pose (1.0 = as filmed)")
    e.add_argument("--stabilize", action="store_true", help="centre hips horizontally each frame (moving camera / travelling performer)")
    e.add_argument("--window", type=tstamp, help="source seconds the search looks for (default frames/fps); the winner is stretched onto the frame count")
    e.add_argument("--search", action="store_true", help="slide a frames/fps-second window over --start..--end and pick the tightest loop")
    e.add_argument("--playback", choices=["loop", "one-shot", "final-hold"], default="loop")
    r = sub.add_parser("render"); r.add_argument("json"); r.add_argument("--out")
    r.add_argument("--template", help=f"sheet template to render into (default {TEMPLATE_PATH})")
    r.add_argument("--pingpong", action="store_true", help="walk the frames out and back (0..N-1..1) so the seam is the motion reversed")
    x = sub.add_parser("export"); x.add_argument("json"); x.add_argument("--out")
    x.add_argument("--sheet", help="motion sheet HTML (default <name>-motion.html beside the json)")
    sub.add_parser("selftest")
    a = ap.parse_args()
    {"extract": extract, "render": render, "export": export, "selftest": lambda _: selftest()}[a.cmd](a)


if __name__ == "__main__":
    main()
