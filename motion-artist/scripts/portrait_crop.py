#!/usr/bin/env python3
"""portrait_crop: crop a landscape video to a portrait window around the performer.

  portrait_crop.py VIDEO --start S --end S [--aspect 9:16] [--margin 0.12] [--out FILE]

A thumb is the whole frame resized to 200px wide (motion_artist.py:339), so a 16:9
source leaves the performer about 66px tall — a third of what a portrait source gives.
Cropping the video before `extract` fixes that at the only place it can be fixed: the
pixels. Tracing runs on the cropped frames, so `pts`, the thumbs and the figure all
share one coordinate space and nothing downstream has to be told about the crop.

The crop is computed from the performer's landmarks across --start..--end, then applied
to the WHOLE video so every timestamp keeps its original meaning: marks in clips.json
and `extract --start/--end` stay valid, and the manifest still records the true second.
A different move from the same video may sit elsewhere in frame and need its own crop.
"""
import argparse, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SAMPLES = 24  # frames traced across the span to find the performer; enough for a stable union


def fit_aspect(x0, y0, x1, y1, aspect, W, H):
    """Grow the box to `aspect` (w/h) about its centre, then fit it inside WxH.

    Growing can push the box off frame, and clamping it back can break the aspect, so
    the box is shrunk to what the frame can hold before it is re-centred. Returns even
    numbers: h264 refuses odd dimensions.
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


def performer_box(path, start, end, margin):
    """Union of the performer's landmark box over sampled frames, padded by `margin`."""
    import cv2
    from motion_artist import landmarker

    mp, lm = landmarker()
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    xs0, ys0, xs1, ys1 = [], [], [], []
    for k in range(SAMPLES):
        t = start + (end - start) * k / max(SAMPLES - 1, 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, bgr = cap.read()
        if not ok:
            continue
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        res = lm.detect(img)
        if not res.pose_landmarks:
            continue
        p = res.pose_landmarks[0]
        xs0.append(min(v.x for v in p)); xs1.append(max(v.x for v in p))
        ys0.append(min(v.y for v in p)); ys1.append(max(v.y for v in p))
    cap.release()
    if not xs0:
        raise SystemExit("no performer found in that span — check --start/--end")

    x0, x1 = min(xs0) * W, max(xs1) * W
    y0, y1 = min(ys0) * H, max(ys1) * H
    # Landmarks stop at the eyes and ankles; the real figure runs past both, so pad by a
    # share of its own height rather than a fixed number of pixels.
    pad = (y1 - y0) * margin
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad), W, H, len(xs0)


def main(a):
    aspect_w, _, aspect_h = a.aspect.partition(":")
    aspect = float(aspect_w) / float(aspect_h)
    box, W, H, n = performer_box(a.video, a.start, a.end, a.margin)
    x, y, w, h = fit_aspect(*box, aspect, W, H)

    fig_h = (box[3] - box[1]) / (1 + 2 * a.margin)  # undo the pad to report the figure itself
    print(f"traced {n}/{SAMPLES} samples | source {W}x{H} | crop {w}x{h} at +{x}+{y}")
    print(f"figure {fig_h:.0f}px tall -> {fig_h / h * 200 / aspect:.0f}px in a 200px-wide thumb "
          f"(was {fig_h / H * 200 * H / W:.0f}px uncropped)")

    out = a.out or f"{os.path.splitext(a.video)[0]}-portrait.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", a.video,
                    "-vf", f"crop={w}:{h}:{x}:{y}", "-c:v", "libx264", "-crf", "18",
                    "-preset", "veryfast", "-an", out], check=True)
    print(out)


def selftest():
    # Wider than target: height grows to suit, and the box slides up to stay in frame.
    x, y, w, h = fit_aspect(100, 100, 500, 300, 9 / 16, 1280, 720)
    assert (x, y, w, h) == (100, 0, 400, 710), (x, y, w, h)
    assert abs(w / h - 9 / 16) < 0.01, w / h
    # Taller than the frame allows: shrink to the frame height, keep the aspect.
    x, y, w, h = fit_aspect(600, -200, 700, 900, 9 / 16, 1280, 720)
    assert (w, h) == (404, 720) and y == 0, (x, y, w, h)
    assert 0 <= x and x + w <= 1280
    # A box already at the target aspect is left where it is.
    x, y, w, h = fit_aspect(0, 0, 360, 640, 9 / 16, 1280, 720)
    assert (w, h) == (360, 640) and (x, y) == (0, 0), (x, y, w, h)
    # Every result is even, or h264 rejects it.
    for args in ((10, 10, 33, 77), (5, 5, 101, 203), (0, 0, 1279, 719)):
        assert all(v % 2 == 0 for v in fit_aspect(*args, 9 / 16, 1280, 720))
    print("ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", nargs="?")
    p.add_argument("--start", type=float, help="span the performer is found in (seconds)")
    p.add_argument("--end", type=float)
    p.add_argument("--aspect", default="9:16", help="target w:h (default 9:16, what a Short gives)")
    p.add_argument("--margin", type=float, default=0.12,
                   help="pad around the landmark box, as a share of figure height")
    p.add_argument("--out")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    if not (a.video and a.start is not None and a.end is not None):
        p.error("video, --start and --end are required")
    main(a)
