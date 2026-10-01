#!/usr/bin/env python3
"""clipper: mark clip in/out points on a video, frame by frame, in a browser.

  motion_artist/scripts/clipper.py [--port 8765]

Open http://localhost:8765, pick a video you have already opened or paste a YouTube URL,
name the genre it belongs to, and mark clips.

Work is keyed by the video, exports by genre. A video downloads once into
work/<creator>-<title>/ -- the source, every split frame, and the clips marked against
them -- so a clip can never be read against the wrong source. The marks are saved to
work/<creator>-<title>/clips.json.

The directory is named for the video so it can be recognised, but the *video* is its
YouTube id, which is what decides whether two URLs are the same video. Both are kept:
the id lives in meta.json, and a second upload that shares a creator and a title gets
the next free `-2` rather than being opened as the first one.

Clips are numbered under their own video -- `clip-01`, `clip-02` -- which is nothing more
than their place in that file's array, and names the directory each is captured into.

That is *not* the motion number. Motions are numbered across every video in a genre, so
this video's first clip may well be `hiphop-04`. A position in one video's array cannot
say that, so nothing here writes it: `extract --genre` allocates the name the first time
a clip is cut and writes it back into clips.json as `motion`. A clip that has one keeps
it, which is what makes the re-export replace that bundle instead of taking a second
number.

That file is the handoff: `motion_artist.py extract <url> --start S --end S ...` takes
the seconds straight from it, and each clip names the directory it is captured into.
This tool does not run the pipeline; it only settles which frames the pipeline should be
pointed at.
"""
import argparse, errno, json, os, re, shutil, subprocess, sys, tempfile, threading, urllib.parse, urllib.request, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from motion_artist import fetch, publish, HOME, outside_repo, local_source, source_rate, sha256, atomic_json, motion_gender

# HOME, not the checkout: work/ (downloads, frames, marks) and exports/ are local staging, and
# the marks are stored on the Drive beside the captures cut from them.
ROOT = HOME
WORK = os.path.join(ROOT, "work")
EXPORTS = os.path.join(ROOT, "exports")
FRAME_HEIGHT = 480  # display copies; extract re-reads the source video at full resolution
PLAYBACK = ("loop", "one-shot", "final-hold")  # extract's own --playback choices, not a second vocabulary
# ping-pong is offered beside them but is not one of them: it is `extract --pingpong`, a
# separate flag on the same command. It rides in the clip as its own field.
PINGPONG = "ping-pong"
CHOICES = PLAYBACK + (PINGPONG,)
CAPTURE_FPS = 12  # CAG's editor default; the skill keeps fps fixed across a library
VIDEO_LOCK = threading.Lock()


def slug(s):
    """A name safe to use as a directory: lowercase, dashes, nothing else."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def probe_fps(path):
    """Exact source fps as a float. ffprobe reports it as a rational ('30000/1001')."""
    num, den = source_rate(path).split("/")
    return float(num) / float(den)


def probe_video(url):
    """The video's id, title and creator, without downloading it.

    All three have to be known before the download: the title and creator name the
    directory the download goes into, and the id says whether that directory is already
    this video's. Asking yt-dlp twice costs one metadata request and buys the guarantee
    that the same video pasted again, under any of its URL spellings, reopens the frames
    already split rather than fetching a second copy.
    """
    r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--skip-download",
                        "--print", "%(id)s\t%(title)s\t%(uploader)s", url],
                       capture_output=True, text=True, check=True)
    parts = r.stdout.strip().splitlines()[-1].split("\t")
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def video_dir(key):
    if not key or key in (".", "..") or os.path.basename(key) != key:
        raise ValueError("video key must be one directory name")
    return os.path.join(WORK, key)


def video_key(creator, title, vid, source_kind="youtube"):
    """The directory a video works in: `<creator>-<title>`, slugged.

    A readable name is worth having -- an id says nothing about which video it is -- but
    it is not an identifier: a creator can post two videos under one title, and a
    re-upload shares both. The id decides. A directory whose meta.json names a different
    video is not this video's, so this video takes the next free `-2`, `-3` … rather than
    opening the other one's frames and reading its marks against them.

    An empty title and creator leave nothing to name a directory after, which only the
    id can answer.
    """
    identity = vid if source_kind != "youtube" else "youtube:" + vid
    # Content identity survives an import under another title or from another file path.
    for existing in sorted(os.listdir(WORK)) if os.path.isdir(WORK) else []:
        m = read_meta(existing) if os.path.isdir(video_dir(existing)) else {}
        if m.get("identity") == identity or (source_kind == "youtube" and m.get("id") == vid):
            return existing
    base = slug(f"{creator} {title}") or (vid if source_kind == "youtube" else slug(vid))
    key, n = base, 1
    while True:
        m = read_meta(key)
        if not os.path.exists(video_dir(key)):
            return key
        n += 1
        key = f"{base}-{n}"


def source_of(vdir):
    for f in sorted(os.listdir(vdir)):
        if f == "source.mp4" or f.startswith("source-"):
            return os.path.join(vdir, f)
    return None


def read_meta(key):
    p = os.path.join(video_dir(key), "meta.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def clips_path(key):
    """The marks live with the frames they were read off, never with the exports.

    Frame numbers mean nothing away from their video, and a genre holds clips from many
    videos -- so a genre-keyed clips file is one video overwriting another's marks.
    """
    return os.path.join(video_dir(key), "clips.json")


def push_clips(key):
    err = publish(clips_path(key), f"captures/{key}/clips.json")
    if err: print(f"clipper: clips.json not on the Drive yet — {err}", file=sys.stderr)


def read_clips(key):
    p = clips_path(key)
    clips = json.load(open(p))["clips"] if os.path.exists(p) else []
    for c in clips: motion_gender(c.get("gender"), p)
    return clips


def videos():
    """Every video already split, for the picker: 'Creator - Title'."""
    if not os.path.isdir(WORK):
        return []
    out = []
    for key in sorted(os.listdir(WORK)):
        if not os.path.isdir(os.path.join(video_dir(key), "frames")):
            continue
        m = read_meta(key)
        out.append({"key": key, "id": m.get("id", ""), "title": m.get("title") or key,
                    "creator": m.get("creator", ""), "genre": m.get("genre", ""),
                    "source_kind": m.get("source_kind", "youtube"), "identity": m.get("identity", ""),
                    "clips": len(read_clips(key))})
    return sorted(out, key=lambda v: (v["creator"].lower(), v["title"].lower()))


def genres():
    """Genres already in use, so the box offers them rather than inviting a near-miss."""
    seen = {v["genre"] for v in videos() if v["genre"]}
    if os.path.isdir(EXPORTS):
        seen |= {d for d in os.listdir(EXPORTS) if os.path.isdir(os.path.join(EXPORTS, d))}
    return sorted(seen)


def open_video(url, key, genre, source=None, title=None):
    with VIDEO_LOCK:
        return import_video(url, key, genre, source, title)


def delete_video(key, confirmed=False):
    """Remove a video from the marker, retaining its local files in recoverable trash."""
    if confirmed is not True: raise ValueError("confirm video deletion first")
    with VIDEO_LOCK:
        path = outside_repo(video_dir(key))
        if os.path.islink(video_dir(key)):
            raise ValueError("cannot delete a linked video directory")
        if not os.path.isdir(path) or not read_meta(key):
            raise ValueError("video is no longer available; refresh the list")
        trash = outside_repo(os.path.join(os.path.dirname(WORK), "trash", "videos"))
        os.makedirs(trash, exist_ok=True)
        destination = tempfile.mkdtemp(prefix=key + "-", dir=trash)
        try:
            shutil.move(path, os.path.join(destination, key))
        except Exception:
            if not os.listdir(destination): os.rmdir(destination)
            raise
        return {"deleted": key, "trash": os.path.join(destination, key),
                "videos": videos(), "genres": genres()}


def open_upload(stream, length, filename, genre, title=None):
    """Stage browser-selected footage locally, then use the same content identity as a path import."""
    if not slug(genre): raise ValueError("enter a motion genre before opening the video")
    if length <= 0: raise ValueError("choose a non-empty video file")
    filename = os.path.basename(filename.replace("\\", "/"))
    if not filename or filename in (".", ".."): raise ValueError("choose a video file")
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=outside_repo(WORK)) as temp:
        source = os.path.join(temp, filename)
        with open(source, "wb") as target:
            remaining = length
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block: raise ValueError("video upload was interrupted; choose the file again")
                target.write(block); remaining -= len(block)
        probe_fps(source)
        return open_video("", "", genre, source, title)


def import_video(url, key, genre, source=None, title=None):
    """Download (once) and split (once). Returns the viewer's state for this video.

    Opening by key reads back a video already here; opening by URL resolves the video
    first and `video_key` turns it into a directory, so pasting the URL of a video already
    downloaded reopens it rather than fetching a second copy. Nothing needs confirming and
    nothing is ever replaced: the name comes from the video, not from something typed, and
    a name already taken by a different video goes to the next free suffix.
    """
    if source and (url or key): raise ValueError("pass a local source, a URL, or a video key")
    incoming = {}
    if source:
        source = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(source): raise ValueError(f"no local source at {source}")
        incoming = local_source(source, title)
        creator = "Local AI" if incoming["source_kind"] == "ai-generated" else "Local"
        title = incoming["title"]
        key = video_key(creator, title, incoming["identity"], incoming["source_kind"])
        incoming.update(creator=creator, url="")
    elif not key:
        if not url:
            raise ValueError("pick a video, or paste a YouTube URL")
        vid, title, creator = probe_video(url)
        key = video_key(creator, title, vid)
        incoming = dict(id=vid, url=url, title=title, creator=creator,
                        source_kind="youtube", identity="youtube:" + vid)
    else:
        m = read_meta(key)
        if not m:
            raise ValueError(f"no video here called {key!r}")
        url = m.get("url", url)

    vdir = outside_repo(video_dir(key))
    meta = read_meta(key)
    genre = slug(genre) or meta.get("genre", "")
    if not genre:
        raise ValueError("name the genre this video's motions belong to")
    # A clip that has been cut carries a motion number allocated inside its genre, and a
    # bundle exported under it. Refiling the video elsewhere would leave both pointing at a
    # genre it no longer belongs to, so a cut clip has to go first -- deliberately, not as
    # a side effect of retyping the box. Clips only marked, never cut, have no number yet
    # and move freely.
    cut = [c for c in read_clips(key) if c.get("motion")]
    if meta.get("genre") and meta["genre"] != genre and cut:
        raise ValueError(f"this video's {len(cut)} cut clip(s) are numbered in "
                         f"{meta['genre']} ({', '.join(c['motion'] for c in cut)}); "
                         f"delete them before moving it to {genre}")

    os.makedirs(vdir, exist_ok=True)
    src = source_of(vdir)
    if not src:
        if source:
            src = outside_repo(os.path.join(vdir, "source.mp4"))
            shutil.copyfile(source, src)
        elif not url:
            raise ValueError("no video downloaded yet, and no URL given")
        else: src, _ = fetch(url, vdir)
    expected = meta.get("source_sha256") or incoming.get("source_sha256")
    if expected and sha256(src) != expected:
        raise ValueError("source video changed since import; refusing to reopen its marks")

    frames_dir = outside_repo(os.path.join(vdir, "frames"))
    if not os.path.isdir(frames_dir) or not os.listdir(frames_dir):
        temp = outside_repo(os.path.join(vdir, "frames-building"))
        if os.path.isdir(temp): shutil.rmtree(temp)
        os.makedirs(temp)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src,
                        "-vf", f"scale=-2:{FRAME_HEIGHT}", "-q:v", "4",
                        os.path.join(temp, "%06d.jpg")], check=True)
        if os.path.isdir(frames_dir): os.rmdir(frames_dir)
        os.replace(temp, frames_dir)

    # the id rides in meta.json, not in the directory name: it is what decides whether the
    # next URL pasted is this video, and the name is only what makes the directory legible.
    meta = {**incoming, **meta, "key": key, "genre": genre}
    if not meta.get("source_kind"):
        meta.update(source_kind="youtube", identity="youtube:" + meta["id"])
    atomic_json(os.path.join(vdir, "meta.json"), meta)

    count = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    return {**meta, "frames": count, "fps": probe_fps(src), "clips": read_clips(key),
            "videos": videos(), "genres": genres()}


def write_clips(key, meta, fps, clips):
    """Each clip records frames and the seconds extract needs, plus where it is captured.

    The capture's frame count is derived, never typed: `frames = fps * window`, and the
    window is the span the marks already fixed. Picking a frame count first and hunting a
    window to fit it is the mistake the skill calls invisible in the output.

    Clips are numbered under their own video -- `clip-01`, `clip-02` -- which is just their
    place in this array and names the directory each is captured into. That is *not* the
    motion number: motions are numbered across every video in a genre, so this video's
    first clip may be the genre's fourth motion. `extract --genre` allocates that one the
    first time a clip is cut and writes it back here as `motion`, which is why nothing
    writes it at marking time and a clip that has one keeps it.
    """
    genre = meta["genre"]
    prior = read_clips(key)
    by_capture = {c["capture"]: c for c in prior if c.get("capture")}
    out, seen = [], set()
    for i, c in enumerate(clips, 1):
        a, b = int(c["in"]), int(c["out"])
        name = f"clip-{i:02d}"
        previous = by_capture.get(c.get("capture"), {})
        if not previous:
            matches = [p for p in prior if (p["in_frame"], p["out_frame"]) == (a, b)]
            previous = matches[0] if len(matches) == 1 else {}
        motion = c.get("motion") or previous.get("motion") or ""
        gender = motion_gender(c.get("gender", previous.get("gender")), f"{key}/{name}")
        if motion and motion in seen:  # both would be written to the same export directory
            raise ValueError(f"two clips are numbered {motion!r}; they would share "
                             f"exports/{genre}/{motion}")
        if motion:
            seen.add(motion)
        pb = c.get("playback") or "loop"
        pingpong = pb == PINGPONG
        if pingpong:
            pb = "loop"  # the capture is a loop; out-and-back is how the sheet walks it
        if pb not in PLAYBACK:  # this file is a command line; a bad value would reach extract as one
            raise ValueError(f"unknown playback {pb!r}, expected one of {', '.join(CHOICES)}")
        cap_fps = c.get("fps")                          # `or` would read a 0 fps as the default
        cap_fps = CAPTURE_FPS if cap_fps is None else int(cap_fps)
        if not 1 <= cap_fps <= 60:
            raise ValueError(f"capture fps {cap_fps} is outside 1..60")
        window = (b - a + 1) / fps                      # seconds of source the marks span
        cap_frames = max(1, round(window * cap_fps))
        if pingpong and cap_frames <= 2:
            # the sheet's player bounces only above 2 frames and says nothing when it does not
            raise ValueError(f"{name}: ping-pong needs more than 2 captured frames, this clip has "
                             f"{cap_frames} — widen the marks or raise its fps")
        clip = {"in_frame": a, "out_frame": b, "frames": b - a + 1,
                "start": round((a - 1) / fps, 3), "end": round(b / fps, 3),
                "playback": pb, "pingpong": pingpong, "gender": gender,
                "capture_fps": cap_fps, "capture_frames": cap_frames,
                # span / (frames/fps). extract recomputes it; 1.0 here means the marks
                # already land on a whole frame at this fps, so no rounding is hiding.
                "speed_factor": round(window / (cap_frames / cap_fps), 3),
                # the capture stays with its video; the bundle it exports to is named by
                # `motion`, which does not exist until extract allocates it.
                "capture": f"work/{key}/{name}"}
        if motion:
            clip["motion"] = motion
        out.append(clip)
    os.makedirs(video_dir(key), exist_ok=True)
    outside_repo(clips_path(key))
    atomic_json(clips_path(key), {"video": key, **({"video_id": meta["id"]} if meta.get("id") else {}), "url": meta.get("url", ""),
                   "title": meta.get("title", ""), "creator": meta.get("creator", ""),
                   **{k: meta[k] for k in ("source_kind", "identity", "source_sha256", "generation") if k in meta},
                   "genre": genre, "source_fps": round(fps, 4), "clips": out})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/videos":
            return self.send(200, json.dumps({"videos": videos(), "genres": genres()}))
        if u.path == "/api/frame":
            key, n = q.get("video", [""])[0], int(q.get("n", ["1"])[0])
            # the key names a directory and arrives over the wire; keep it to one path element
            p = os.path.join(WORK, os.path.basename(key), "frames", f"{n:06d}.jpg")
            if not os.path.exists(p):
                return self.send(404, b"", "image/jpeg")
            self.send(200, open(p, "rb").read(), "image/jpeg")
            return
        self.send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/upload":
                q = urllib.parse.parse_qs(u.query)
                state = open_upload(self.rfile, int(self.headers.get("Content-Length", 0)),
                                    q.get("filename", [""])[0], q.get("genre", [""])[0],
                                    q.get("title", [None])[0])
                return self.send(200, json.dumps(state))
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/delete-video":
                return self.send(200, json.dumps(delete_video(body.get("video", ""),
                                                               body.get("confirmed", False))))
            if self.path == "/api/open":
                return self.send(200, json.dumps(open_video(body.get("url", "").strip(),
                                                            os.path.basename(body.get("video", "")),
                                                            body.get("genre", ""), body.get("source"),
                                                            body.get("title"))))
            if self.path == "/api/clips":
                key = os.path.basename(body.get("video", ""))
                with VIDEO_LOCK:
                    meta = read_meta(key)
                    if not meta:
                        raise ValueError("open a video first")
                    saved = write_clips(key, meta, float(body["fps"]), body.get("clips", []))
                # off the request thread: a Drive round trip is seconds, and a save is every edit
                threading.Thread(target=push_clips, args=(key,), daemon=True).start()
                return self.send(200, json.dumps({"clips": saved, "path": clips_path(key)}))
        except subprocess.CalledProcessError as e:
            return self.send(500, json.dumps({"error": (e.stderr or str(e))[-400:]}))
        except Exception as e:
            return self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))
        self.send(404, json.dumps({"error": "not found"}))


PAGE = r"""<!doctype html><meta charset=utf-8><title>clipper</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#15161a;color:#e8e8ea;font:14px/1.5 ui-sans-serif,system-ui,sans-serif}
 .wrap{max-width:900px;margin:0 auto;padding:20px 16px 60px}
 h1{font-size:16px;letter-spacing:.08em;text-transform:uppercase;color:#8b8b94;margin:0 0 16px}
 input,button,select{font:inherit;background:#222329;color:#e8e8ea;border:1px solid #34353d;border-radius:6px;padding:7px 10px}
 button{cursor:pointer}button:hover{background:#2c2d35}
 button.go{background:#4a6cf7;border-color:#4a6cf7;color:#fff}button.go:hover{background:#5b79f8}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .row+.row{margin-top:8px}
 .stage{margin:16px 0;background:#0d0e11;border:1px solid #26272e;border-radius:8px;
        display:flex;align-items:center;justify-content:center;min-height:420px}
 .stage img{max-height:520px;max-width:100%;display:block}
 .meta{display:flex;gap:18px;font-variant-numeric:tabular-nums;color:#a0a0aa;margin:8px 0}
 .meta b{color:#e8e8ea;font-weight:600}
 .track{position:relative;height:28px;margin:10px 0;cursor:pointer;touch-action:none}
 .track::before{content:"";position:absolute;left:0;right:0;top:12px;height:4px;border-radius:2px;background:#2c2d35}
 .band{position:absolute;top:12px;height:4px;border-radius:2px;background:#7dd3a0;opacity:.35;display:none}
 .head{position:absolute;top:5px;width:2px;height:18px;margin-left:-1px;background:#e8e8ea}
 .handle{position:absolute;top:2px;width:9px;height:24px;margin-left:-4px;border-radius:3px;
         background:#7dd3a0;border:1px solid #15161a;cursor:ew-resize;display:none}
 .handle:hover{background:#9ae4bb}
 .marks{color:#7f8694}.marks b{color:#7dd3a0}
 .warn{color:#e0b341}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-variant-numeric:tabular-nums}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #26272e}
 th{color:#8b8b94;font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
 td.n{color:#a0a0aa}
 tr.editing{background:#1d2333}
 .hint{color:#6f707a;font-size:12px;margin-top:10px}
 .err{color:#ff8b8b;margin:8px 0;white-space:pre-wrap}
 .ok{color:#7dd3a0}
 kbd{background:#26272e;border:1px solid #3a3b44;border-radius:4px;padding:1px 5px;font-size:11px}
</style>
<div class=wrap>
<h1>clipper</h1>

<div class=row>
  <select id=vid><option value="">— new video —</option></select>
  <input id=url placeholder="YouTube URL (new video only)" size=34>
  <button id=browse>Browse local video</button>
  <input id=file type=file accept="video/*,.m4v,.mkv,.avi,.mov,.mp4,.webm" hidden>
  <span id=filename></span>
  <input id=local aria-label="Local video path" placeholder="or paste a local video path" size=34>
  <input id=title placeholder="local title (optional)" size=18>
  <label>Motion genre <input id=genre placeholder="e.g. actions, emotes, hiphop" size=22 list=genres></label>
  <button class=go id=load>Open</button>
  <button id=deletevideo disabled>Delete video</button>
  <span id=status></span>
</div>
<datalist id=genres></datalist>
<div class=err id=err></div>

<div id=editor hidden>
  <div class=stage><img id=img></div>
  <div class=track id=track>
    <div class=band id=band></div>
    <div class=head id=head></div>
    <div class=handle id=hin></div>
    <div class=handle id=hout></div>
  </div>
  <div class=meta>
    <span>frame <b id=fnum>1</b> / <span id=ftot>0</span></span>
    <span><b id=fsec>0.000</b>s</span>
    <span class=marks>in <b id=min>–</b> · out <b id=mout>–</b> · <b id=mlen>–</b> frames</span>
    <span id=cap></span>
  </div>
  <div class=row>
    <button id=bin>Mark in (I)</button>
    <button id=bout>Mark out (O)</button>
    <button id=play>Play (space)</button>
    <button id=pcap title="play the capture: the frames extract will keep, at the capture's own fps">Capture (C)</button>
    <label title="capture fps. The frame count is derived from it: frames = fps x window">
      fps <input id=capfps type=number min=1 max=60 value=12 style=width:4.5em></label>
    <select id=pmode title="how the capture plays back. ping-pong is extract's --pingpong, a
separate flag: the capture is a loop walked out and back. It reaches the manifest and motion.json;
the straight seam is still measured, because a consumer that ignores the flag plays it.">
      <option value=loop>loop</option>
      <option value=one-shot>one-shot</option>
      <option value=final-hold>final-hold</option>
      <option value=ping-pong>ping-pong</option>
    </select>
    <label>Gender <select id=gender title="Which characters this motion suits; choose explicitly">
      <option value="">unclassified</option>
      <option value=male>male</option>
      <option value=female>female</option>
      <option value=any>any</option>
    </select></label>
    <button class=go id=add>Add clip</button>
    <button id=cancel hidden>Cancel edit</button>
  </div>
  <div class=hint><kbd>←</kbd><kbd>→</kbd> step · <kbd>shift</kbd>+arrows ×10 · <kbd>I</kbd> in ·
    <kbd>O</kbd> out · <kbd>space</kbd> play footage · <kbd>C</kbd> play capture · <kbd>enter</kbd> add clip ·
    drag the green marks on the track to move in and out</div>

  <table><thead><tr><th>clip</th><th>motion</th><th>in</th><th>out</th><th>frames</th><th>seconds</th><th>capture</th><th>playback</th><th>gender</th><th></th></tr></thead>
  <tbody id=list></tbody></table>
  <div class=hint id=saved></div>
</div>
</div>
<script>
const $ = id => document.getElementById(id);
let S = {video:'', genre:'', url:'', fps:30, frames:0, n:1, in:null, out:null,
         clips:[], editing:-1, timer:null};

const setText = (id,v) => $(id).textContent = v;
const err = m => $('err').textContent = m || '';
// creator first, the way the directory is named, so the label and the folder read alike
const label = v => `${v.creator ? v.creator + ' - ' : ''}${v.title}` +
                   `${v.genre ? '  [' + v.genre + ']' : ''}${v.clips ? '  ' + v.clips + ' clips' : ''}`;

// The picker is over videos already downloaded here. Its value is the directory, not
// anything typed, so it cannot name a video that is not the one on screen.
function fillVideos(vs, gs, keep){
  $('vid').replaceChildren(new Option('— new video —', ''),
    ...vs.map(v=>new Option(label(v), v.key)));
  $('vid').value = keep || '';
  $('genres').replaceChildren(...gs.map(g=>new Option(g, g)));
}
// The URL and genre boxes only ever describe what is selected, so any change empties both:
// "new video" starts from an empty box rather than the last video's URL — which Open would
// have read as that video again — and its genre, which would have filed a different video
// under it. An existing video needs neither typed: it is opened by name, and an empty genre
// box means the one in its meta.json, which Open fills back in.
// The player only ever shows the video Open last returned. Any other choice empties it, so
// the last video's frames, marks, clips and running playback are never mistaken for the next's.
function clearPlayer(){
  stopPlay();
  S = {...S, video:'', url:'', frames:0, n:1, in:null, out:null, clips:[], editing:-1};
  $('editor').hidden = true; $('img').removeAttribute('src');
  $('deletevideo').disabled = true;
  setText('status', ''); $('saved').innerHTML = ''; err('');
}
$('vid').onchange = () => {
  clearPlayer();
  $('url').value = ''; $('genre').value = '';
  $('local').value = ''; $('title').value = '';
  $('file').value = ''; setText('filename', '');
  $('url').disabled = !!$('vid').value;
  $('local').disabled = $('title').disabled = !!$('vid').value;
  if (!$('vid').value) $('url').focus();
};
$('browse').onclick = () => $('file').click();
$('file').onchange = () => {
  const file = $('file').files[0];
  if (!file) return;
  clearPlayer(); $('vid').value = ''; $('url').value = ''; $('local').value = '';
  $('url').disabled = $('local').disabled = $('title').disabled = false;
  setText('filename', file.name);
};
for (const id of ['url', 'local']) $(id).oninput = () => {
  $('file').value = ''; setText('filename', '');
  $(id === 'url' ? 'local' : 'url').value = '';
};

function show(n){
  S.n = Math.min(Math.max(1, n), S.frames);
  $('img').src = `/api/frame?video=${encodeURIComponent(S.video)}&n=${S.n}`;
  setText('fnum', S.n);
  setText('fsec', ((S.n-1)/S.fps).toFixed(3));
  place();
}

// The track is one coordinate system: frame 1 sits at 0%, the last frame at 100%.
const pct = n => ((n-1) / Math.max(1, S.frames-1)) * 100 + '%';
const frameAt = e => {
  const r = $('track').getBoundingClientRect();
  return clamp(1 + Math.round((e.clientX - r.left) / r.width * (S.frames-1)));
};
const clamp = n => Math.min(Math.max(1, n), S.frames);

function place(){
  $('head').style.left = pct(S.n);
  for (const [id, n] of [['hin', S.in], ['hout', S.out]]){
    $(id).style.display = n ? 'block' : 'none';
    if (n) $(id).style.left = pct(n);
  }
  const span = S.in && S.out && S.out >= S.in;
  $('band').style.display = span ? 'block' : 'none';
  if (span){
    $('band').style.left = pct(S.in);
    $('band').style.width = `calc(${pct(S.out)} - ${pct(S.in)})`;
  }
}

// One handler for all three: grabbing a handle drags that mark, anywhere else scrubs.
// A dragged mark stops at its neighbour rather than pushing past it.
$('track').onpointerdown = e => {
  const mark = {hin:'in', hout:'out'}[e.target.id] || null;
  const move = ev => {
    const n = frameAt(ev);
    if (mark === 'in')  S.in  = Math.min(n, S.out || S.frames);
    if (mark === 'out') S.out = Math.max(n, S.in || 1);
    if (mark) marks();
    show(mark ? S[mark] : n);
  };
  $('track').setPointerCapture(e.pointerId);
  $('track').onpointermove = move;
  $('track').onpointerup = () => $('track').onpointermove = $('track').onpointerup = null;
  move(e);
  e.preventDefault();
};
// frames = fps x window, and the window is whatever the marks already span. The count is
// derived here and in write_clips the same way; nothing types a frame count.
const capFps = () => Math.min(Math.max(1, +$('capfps').value || 1), 60);
const windowOf = (a, b) => (b - a + 1) / S.fps;
const capFrames = (a, b, fps) => Math.max(1, Math.round(windowOf(a, b) * fps));

function marks(){
  setText('min', S.in ?? '–'); setText('mout', S.out ?? '–');
  setText('mlen', (S.in && S.out && S.out>=S.in) ? (S.out-S.in+1) : '–');
  const span = S.in && S.out && S.out >= S.in;
  const el = $('cap');
  if (!span){ el.textContent = ''; el.className = ''; }
  else {
    const w = windowOf(S.in, S.out), n = capFrames(S.in, S.out, capFps()), factor = w / (n / capFps());
    // off 1.000 means the marks do not land on a whole frame at this fps, so the bundle
    // would play fast or slow. The marks are draggable: nudge one until it reads exact.
    const off = Math.abs(factor - 1) > 0.0005;
    el.textContent = `→ ${n} frames @ ${capFps()} fps · ${(n/capFps()).toFixed(3)}s` +
      ($('pmode').value === 'ping-pong' ? ` · out and back over ${2*n-2}` : '') +
      (off ? ` · plays ${factor > 1 ? 'fast' : 'slow'} ${factor.toFixed(3)}x` : ' · exact');
    el.className = off ? 'warn' : 'ok';
  }
  place();
}

$('capfps').oninput = $('pmode').onchange = () => marks();  // rows carry their own
// Clips are numbered under their video, which is only their place in the list. The motion
// number is allocated by `extract --genre` and shown once it exists; clipper never picks it.
const clipName = i => `clip-${String(i+1).padStart(2,'0')}`;
function editLabel(){
  const on = S.editing >= 0;
  setText('add', on ? `Replace ${clipName(S.editing)}` : `Add clip (→ ${clipName(S.clips.length)})`);
  $('cancel').hidden = !on;
}
function endEdit(){ S.editing = -1; $('gender').value = ''; editLabel(); rows(); }
$('cancel').onclick = () => { S.in = S.out = null; marks(); endEdit(); };

function rows(){
  $('list').innerHTML = S.clips.map((c,i)=>`<tr class="${i===S.editing?'editing':''}">
    <td>${clipName(i)}</td><td class=n>${c.motion || '—'}</td>
    <td class=n>${c.in}</td><td class=n>${c.out}</td>
    <td class=n>${c.out-c.in+1}</td>
    <td class=n>${((c.in-1)/S.fps).toFixed(2)}–${(c.out/S.fps).toFixed(2)}</td>
    <td class=n>${capFrames(c.in, c.out, c.fps)}f @ ${c.fps}</td>
    <td class=n>${c.playback}</td>
    <td>${c.gender || 'unclassified'}</td>
    <td><button data-go=${i}>edit</button> <button data-del=${i}>×</button></td></tr>`).join('');
}
$('list').onclick = e => {
  const del = e.target.dataset.del, go = e.target.dataset.go;
  if (del !== undefined){
    const c = S.clips[+del];
    if (!confirm(`Delete ${clipName(+del)}?` + (c.motion
        ? `\n\nIt was cut as ${c.motion}, and that bundle stays in exports/ — this only drops `
          + `the marks. Every clip after it shifts up one, so their capture directories change.`
        : `\n\nEvery clip after it shifts up one.`))) return;
    S.clips.splice(+del,1); if (S.editing === +del) S.editing = -1;
    else if (S.editing > +del) S.editing--;
    rows(); editLabel(); save();
  }
  if (go !== undefined){ const c = S.clips[+go];
    S.in = c.in; S.out = c.out; $('pmode').value = c.playback; $('capfps').value = c.fps;
    $('gender').value = c.gender || '';
    S.editing = +go;            // re-cut: Add replaces this motion and keeps its number
    marks(); show(S.in); editLabel(); rows(); }
};

async function post(path, body){
  const r = await fetch(path, {method:'POST', body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

$('deletevideo').onclick = async () => {
  const key = S.video;
  if (!key) return;
  stopPlay();
  if (!confirm(`Delete “${S.title}” from clipper?\n\nIts local video, frames, saved marks and captures will move to local trash. Published Drive bundles and Drive copies are kept.\n\nContinue?`)) return;
  $('deletevideo').disabled = $('load').disabled = true;
  try {
    const j = await post('/api/delete-video', {video:key, confirmed:true});
    clearPlayer(); fillVideos(j.videos, j.genres);
    $('vid').onchange();
    history.replaceState(null, '', location.pathname);
    setText('status', 'Video deleted from clipper. Recovery copy: ' + j.trash);
  } catch(e){ err(e.message); }
  finally { $('load').disabled = false; $('deletevideo').disabled = !S.video; }
};

$('load').onclick = async () => {
  if (!$('genre').value.trim() && !$('vid').value){
    err('Enter a motion genre, then click Open.'); $('genre').focus(); return;
  }
  clearPlayer(); $('load').disabled = $('browse').disabled = true;
  const file = $('file').files[0];
  setText('status', file ? 'opening local video and splitting frames…' :
          $('local').value ? 'opening local video and splitting frames…' : 'opening video and splitting frames…');
  try {
    let j;
    if (file){
      const query = new URLSearchParams({filename:file.name, genre:$('genre').value, title:$('title').value});
      const r = await fetch('/api/upload?' + query, {method:'POST', body:file});
      j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
    } else j = await post('/api/open', {video: $('vid').value, url: $('url').value,
                                       source: $('local').value || null, title: $('title').value || null,
                                       genre: $('genre').value});
    S = {...S, video:j.key, title:j.title, genre:j.genre, url:j.url, fps:j.fps, frames:j.frames,
         in:null, out:null, editing:-1,
        clips: j.clips.map(c=>({motion:c.motion || '', in:c.in_frame, out:c.out_frame,
                                capture:c.capture, gender:c.gender ?? null,
                                playback: c.pingpong ? 'ping-pong' : (c.playback || 'loop'),
                                fps: c.capture_fps || 12}))};
    fillVideos(j.videos, j.genres, j.key);
    $('url').value = j.url; $('url').disabled = true; $('genre').value = j.genre;
    $('local').value = ''; $('title').value = '';
    $('file').value = ''; setText('filename', '');
    setText('ftot', j.frames);
    setText('status', `${j.creator ? j.creator + ' - ' : ''}${j.title} · ${j.frames} frames @ ${j.fps.toFixed(3)} fps`);
    $('gender').value = '';
    $('editor').hidden = false; marks(); rows(); editLabel(); show(1);
    $('deletevideo').disabled = false;
  } catch(e){ setText('status',''); err(e.message); }
  finally { $('load').disabled = $('browse').disabled = false; }
};

$('bin').onclick = () => { S.in = S.n; if (S.out && S.out < S.in) S.out = null; marks(); };
$('bout').onclick = () => { S.out = S.n; if (S.in && S.in > S.out) S.in = null; marks(); };
// Ping-pong is an order, not a different set of frames: the middle walks back, the way
// sheet.html walks the rendered cells. Both players share it, so both show the same seam.
function outAndBack(order){
  const n = order.length;
  if ($('pmode').value === 'ping-pong' && n > 2) for (let q = n - 2; q > 0; q--) order.push(order[q]);
  return order;
}
function stopPlay(){
  clearInterval(S.timer); S.timer = null;
  setText('play','Play (space)'); setText('pcap','Capture (C)');
}
// One timer, two orders: whichever button started it, the other is idle and the same stop
// clears both labels.
function run(order, fps, btn, label){
  if (S.timer) return stopPlay();
  setText(btn, label);
  let i = Math.max(0, order.indexOf(S.n));
  S.timer = setInterval(() => { show(order[i]); i = (i + 1) % order.length; }, 1000 / fps);
}

// The footage, at the rate it was filmed: every source frame between the marks. Judging
// whether a move is the move means watching the motion.
$('play').onclick = () => {
  const order = [];
  for (let k = S.in || 1; k <= (S.out || S.frames); k++) order.push(k);
  run(outAndBack(order), S.fps, 'play', 'Pause (space)');
};

// The capture, at the rate it will play: the n frames extract will sample across the marked
// window, inclusive of both marks, the way sample_times cuts them. Both boundaries you set
// are frames you get, so the preview and the bundle hold the same first and last pose.
$('pcap').onclick = () => {
  if (S.timer) return stopPlay();
  if (!S.in || !S.out || S.out < S.in) return err('mark an in and an out frame');
  err('');
  const n = capFrames(S.in, S.out, capFps()), step = (S.out - S.in) / Math.max(n - 1, 1);
  const order = [...Array(n).keys()].map(k => Math.min(S.out, S.in + Math.round(k * step)));
  run(outAndBack(order), capFps(), 'pcap', 'Pause (C)');
};

async function save(){
  try {
    const j = await post('/api/clips', {video:S.video, fps:S.fps, clips:S.clips});
    // read the list back so a motion number extract wrote in is not dropped on the next save
    S.clips = j.clips.map(c=>({motion:c.motion || '', in:c.in_frame, out:c.out_frame,
                               capture:c.capture, gender:c.gender ?? null,
                               playback: c.pingpong ? 'ping-pong' : c.playback,
                               fps: c.capture_fps}));
    $('saved').innerHTML = `<span class=ok>saved</span> ${j.path}`;
    return true;
  } catch(e){ err(e.message); return false; }
}

$('add').onclick = async () => {
  if (!S.in || !S.out) return err('mark an in and an out frame');
  if (S.out < S.in) return err('out frame is before in frame');
  err('');
  const i = S.editing;
  // a re-cut keeps the motion it was already cut as, so the re-export replaces that bundle
  const clip = {motion: i >= 0 ? (S.clips[i].motion || '') : '', in:S.in, out:S.out,
                capture: i >= 0 ? S.clips[i].capture : '',
                playback: $('pmode').value, fps: capFps(), gender: $('gender').value || null};
  const prev = i >= 0 ? S.clips[i] : null;
  if (i >= 0) S.clips[i] = clip; else S.clips.push(clip);
  // write_clips can refuse a clip the marks allow. Keep the list equal to the file:
  // put it back and leave the marks alone so the error can be acted on.
  if (!await save()){
    if (i >= 0) S.clips[i] = prev; else S.clips.pop();
    rows(); return;
  }
  S.in = S.out = null; marks(); endEdit();
};

addEventListener('keydown', e => {
  if ($('editor').hidden) return;
  if (e.target.tagName === 'SELECT') return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (typing && e.key !== 'Enter') return;
  const step = e.shiftKey ? 10 : 1;
  if (e.key === 'ArrowLeft'){ show(S.n - step); e.preventDefault(); }
  else if (e.key === 'ArrowRight'){ show(S.n + step); e.preventDefault(); }
  else if (e.key === ' '){ $('play').click(); e.preventDefault(); }
  else if (!typing && (e.key === 'c' || e.key === 'C')) $('pcap').click();
  else if (e.key === 'Enter'){ $('add').click(); e.preventDefault(); }
  else if (!typing && (e.key === 'i' || e.key === 'I')) $('bin').click();
  else if (!typing && (e.key === 'o' || e.key === 'O')) $('bout').click();
});

fetch('/api/videos').then(r=>r.json()).then(j => {
  const query = new URLSearchParams(location.search), key = query.get('video') || '';
  fillVideos(j.videos, j.genres, key);
  if (key) $('load').click();
  else if (query.get('source')){
    $('local').value = query.get('source'); $('title').value = query.get('title') || '';
    $('genre').value = query.get('genre') || ''; $('genre').focus();
  }
});
</script>
"""


def selftest():
    global WORK, EXPORTS
    import tempfile as _t
    assert slug("Club Male #1") == "club-male-1"
    assert slug("  --Easy  1--  ") == "easy-1"
    assert slug("") == ""

    with _t.TemporaryDirectory() as d:
        WORK, EXPORTS = os.path.join(d, "work"), os.path.join(d, "exports")
        os.makedirs(WORK); os.makedirs(EXPORTS)

        def video(vid, title, creator, genre):
            key = video_key(creator, title, vid)
            os.makedirs(os.path.join(WORK, key, "frames"))
            json.dump({"key": key, "id": vid, "url": f"http://x/{vid}", "title": title,
                       "creator": creator, "genre": genre},
                      open(os.path.join(WORK, key, "meta.json"), "w"))
            return key, read_meta(key)

        # the directory reads as <creator>-<title>, and the id is kept beside it
        ka, a = video("P4QeqpsY8v8", "Toxic", "Britney Spears", "hiphop")
        kb, b = video("vfGlCkOBuvY", "Kata", "Dojo", "hiphop")
        assert ka == "britney-spears-toxic", ka
        assert a["id"] == "P4QeqpsY8v8", a
        # A creator can post two videos under one title, and a re-upload shares both, so the
        # name is not an identifier. The id decides: the second video gets its own directory
        # rather than opening the first one's frames and reading its marks against them.
        kdup, dup = video("ZZZdifferent", "Toxic", "Britney Spears", "hiphop")
        assert kdup == "britney-spears-toxic-2", kdup
        # and the same video asked again comes back to its own directory, not a third one
        assert video_key("Britney Spears", "Toxic", "P4QeqpsY8v8") == "britney-spears-toxic"
        assert video_key("Britney Spears", "Toxic", "ZZZdifferent") == "britney-spears-toxic-2"
        # nothing to name a directory after leaves only the id to do it
        assert video_key("", "", "qQq123") == "qQq123"

        # A clip is numbered under its own video -- its place in that array -- and nothing
        # more. No motion number is written: it does not exist until extract allocates one.
        got_a = write_clips(ka, a, 30.0, [{"in": 1, "out": 30}, {"in": 60, "out": 90}])
        got = write_clips(kb, b, 30.0, [{"in": 1, "out": 30}])
        assert [c["capture"] for c in got_a] == ["work/britney-spears-toxic/clip-01",
                                                 "work/britney-spears-toxic/clip-02"], got_a
        assert not any("motion" in c for c in got_a), got_a
        assert not any("export" in c for c in got_a), got_a
        # both videos' first clips are clip-01: the number is under the video, not the genre
        assert got[0]["capture"] == "work/dojo-kata/clip-01", got[0]
        # the marks live with their own video's frames, never in a shared genre file
        assert clips_path(ka) == os.path.join(WORK, "britney-spears-toxic", "clips.json")
        # the clips file names both: the directory it lives in and the video it was read off
        saved_a = json.load(open(clips_path(ka)))
        assert saved_a["video"] == "britney-spears-toxic", saved_a
        assert saved_a["video_id"] == "P4QeqpsY8v8" and saved_a["genre"] == "hiphop", saved_a
        # a motion extract allocated survives the next save, so a re-cut still replaces its
        # bundle rather than being handed a second number
        kept = write_clips(kb, b, 30.0, [{"motion": "hiphop-03", "in": 1, "out": 30},
                                         {"in": 120, "out": 150}])
        assert kept[0]["motion"] == "hiphop-03" and kept[0]["capture"] == "work/dojo-kata/clip-01"
        assert "motion" not in kept[1], kept[1]       # the new clip has not been cut yet
        # An open marker may not know the number extraction just allocated. Saving its
        # playback or replacing its marks must retain that number from the current file.
        stale = write_clips(kb, b, 30.0, [{"motion": "", "in": 1, "out": 30},
                                         {"in": 120, "out": 150}])
        assert stale[0]["motion"] == "hiphop-03"
        stale = write_clips(kb, b, 30.0, [{"in": 2, "out": 31, "capture": kept[0]["capture"]},
                                         {"in": 120, "out": 150}])
        assert stale[0]["motion"] == "hiphop-03"
        write_clips(kb, b, 30.0, [{"motion": "hiphop-03", "in": 1, "out": 30},
                                 {"in": 120, "out": 150}])
        # a clip's place shifts when one before it is deleted, and its motion rides along
        shifted = write_clips(kb, b, 30.0, [{"in": 120, "out": 150},
                                            {"motion": "hiphop-03", "in": 1, "out": 30}])
        assert shifted[1]["motion"] == "hiphop-03", shifted
        assert shifted[1]["capture"] == "work/dojo-kata/clip-02", shifted
        kc, c = video("y7vEQPySw12", "Kick", "Sensei", "karate")
        write_clips(kc, c, 30.0, [{"in": 1, "out": 30}])
        write_clips(kdup, dup, 30.0, [{"in": 1, "out": 30}])
        # the picker reads creator and title back, keyed by directory, and lists creator-first
        keys = {v["key"]: v for v in videos()}
        assert keys[ka]["title"] == "Toxic" and keys[ka]["creator"] == "Britney Spears"
        assert keys[ka]["id"] == "P4QeqpsY8v8" and keys[kdup]["id"] == "ZZZdifferent"
        assert keys[ka]["clips"] == 2 and keys[kb]["clips"] == 2
        assert [v["key"] for v in videos()][0] == ka, videos()   # Britney sorts before Dojo
        assert genres() == ["hiphop", "karate"], genres()
        # A video whose clips are only marked can still be refiled -- nothing is numbered
        # yet. `ka`'s two clips have no motion, so its genre is not pinned.
        assert not any(c.get("motion") for c in read_clips(ka)), read_clips(ka)
        # but `kb` has a cut clip, and refiling it would strand that number and its bundle
        try:
            open_video("", kb, "karate")
        except ValueError as e:
            assert "hiphop-03" in str(e) and "karate" in str(e), e
        else:
            raise AssertionError("a video with a cut clip changed genre")
        kn, _ = video("9NcA759yZY1", "Untagged", "Nobody", "")   # no genre recorded yet
        try:
            open_video("", kn, "")   # and none typed, so there is nothing to file under
        except ValueError as e:
            assert "genre" in str(e), e
        else:
            raise AssertionError("opened a video with no genre")
        try:
            open_video("", "", "hiphop")        # neither a video nor a URL
        except ValueError as e:
            assert "URL" in str(e), e
        else:
            raise AssertionError("opened nothing")
        try:
            open_video("", "no-such-video", "hiphop")
        except ValueError as e:
            assert "no-such-video" in str(e), e
        else:
            raise AssertionError("opened a video that is not here")
        # two clips cannot be written into one export directory, whatever the UI did
        try:
            write_clips(ka, a, 30.0, [{"motion": "hiphop-01", "in": 1, "out": 9},
                                      {"motion": "hiphop-01", "in": 20, "out": 29}])
        except ValueError as e:
            assert "hiphop-01" in str(e), e
        else:
            raise AssertionError("two clips sharing an export directory accepted")

        # The seam: `extract --genre` finds a clip by the `capture` path written here. If the
        # two ever spell it differently the allocation fails, and only a real run would say
        # so -- so the allocator is run against a clips.json this module wrote. hiphop-03 is
        # taken by dojo-kata and 09 is an exported directory, so ka's clips get 10 and 11.
        from motion_artist import allocate_motion
        os.makedirs(os.path.join(EXPORTS, "hiphop", "hiphop-09"), exist_ok=True)
        assert allocate_motion("hiphop", os.path.join(WORK, ka, "clip-01")) == "hiphop-10"
        assert allocate_motion("hiphop", os.path.join(WORK, ka, "clip-02")) == "hiphop-11"
        # asked again it reads back, from the clip this module wrote
        assert allocate_motion("hiphop", os.path.join(WORK, ka, "clip-01")) == "hiphop-10"
        assert [c.get("motion") for c in read_clips(ka)] == ["hiphop-10", "hiphop-11"]
        # and the next save keeps what extract wrote rather than dropping it
        again = write_clips(ka, a, 30.0, [{"motion": "hiphop-10", "in": 1, "out": 9},
                                          {"motion": "hiphop-11", "in": 20, "out": 29}])
        assert [c["motion"] for c in again] == ["hiphop-10", "hiphop-11"], again

    # frame 1 is t=0; the out frame's end is the boundary after it, so a 1-frame
    # clip at 30fps spans 0.000-0.033 and extract sees a non-empty window.
    with _t.TemporaryDirectory() as d:
        WORK, EXPORTS = os.path.join(d, "work"), os.path.join(d, "exports")
        meta = {"id": "demo", "url": "http://x", "title": "T", "creator": "C", "genre": "demo"}
        out = write_clips("demo", meta, 30.0,
                          [{"in": 1, "out": 1},
                           {"in": 31, "out": 60, "playback": "one-shot"},
                           {"in": 1, "out": 43}])
        assert out[0] == {"in_frame": 1, "out_frame": 1, "frames": 1,
                          "start": 0.0, "end": 0.033, "playback": "loop", "pingpong": False, "gender": None,
                          "capture_fps": 12, "capture_frames": 1, "speed_factor": 0.4,
                          "capture": "work/demo/clip-01"}, out[0]
        assert out[1]["playback"] == "one-shot", out[1]
        # 30 source frames at 30 fps is a 1.000 s window; at 12 fps that is 12 frames and
        # 12/12 == 1.000 s, so the capture plays at the speed it was danced.
        assert out[1]["capture_frames"] == 12 and out[1]["speed_factor"] == 1.0, out[1]
        # 43 source frames is 1.4333 s, which is not a whole frame at 12 fps. The count is
        # rounded and speed_factor says so rather than the distortion going unrecorded.
        assert out[2]["capture_frames"] == 17 and out[2]["speed_factor"] != 1.0, out[2]
        # fps is per clip: the same window at a different fps is a different frame count,
        # and the seconds the marks fixed do not move
        two = write_clips("demo", meta, 30.0,
                          [{"in": 31, "out": 60, "fps": 8},
                           {"in": 31, "out": 60, "fps": 24}])
        assert [c["capture_frames"] for c in two] == [8, 24], two
        assert [c["capture_fps"] for c in two] == [8, 24], two
        assert two[0]["start"] == two[1]["start"] and two[0]["end"] == two[1]["end"], two
        # ping-pong is render's flag, not a --playback value: it leaves as a loop that is
        # walked out and back, so nothing hands extract a --playback it would reject
        pp = write_clips("demo", meta, 30.0,
                         [{"in": 31, "out": 60, "playback": "ping-pong"}])[0]
        assert pp["playback"] == "loop" and pp["pingpong"] is True, pp
        assert pp["capture_frames"] == 12, pp
        # the sheet's player bounces only above 2 frames and is silent when it does not, so a
        # clip too short to show it must not be written as though it would
        try:
            write_clips("demo", meta, 30.0, [{"in": 1, "out": 1, "playback": "ping-pong"}])
        except ValueError as e:
            assert "ping-pong" in str(e), e
        else:
            raise AssertionError("ping-pong accepted on a 1-frame capture")
        for bad in (0, 61):
            try:
                write_clips("demo", meta, 30.0, [{"in": 1, "out": 2, "fps": bad}])
            except ValueError:
                pass
            else:
                raise AssertionError(f"capture fps {bad} accepted")
        # a typo must not reach extract as --playback
        try:
            write_clips("demo", meta, 30.0, [{"in": 1, "out": 2, "playback": "looop"}])
        except ValueError as e:
            assert "looop" in str(e), e
        else:
            raise AssertionError("bad playback accepted")
        assert out[1]["start"] == 1.0 and out[1]["end"] == 2.0 and out[1]["frames"] == 30, out[1]
        # the file holds the last write, and says which video and genre it belongs to
        saved = json.load(open(os.path.join(WORK, "demo", "clips.json")))
        assert saved["source_fps"] == 30.0 and "capture_fps" not in saved, saved
        assert saved["video"] == "demo" and saved["genre"] == "demo", saved
        assert saved["title"] == "T" and saved["creator"] == "C", saved
        assert saved["clips"][0]["capture_fps"] == 12, saved
        assert saved["clips"][0]["pingpong"] is True, saved
        # a clip carries no motion and no export path until extract allocates one
        assert "motion" not in saved["clips"][0] and "export" not in saved["clips"][0], saved

        # Classification survives an older client omitting the field and a re-cut moving marks.
        from motion_artist import capture_gender, remember_gender
        classified = write_clips("demo", meta, 30.0,
                                 [{"in": 31, "out": 60, "gender": "female"}])[0]
        capture = os.path.join(d, classified["capture"])
        assert capture_gender(capture) == "female"
        kept = write_clips("demo", meta, 30.0,
                           [{"in": 32, "out": 60, "capture": classified["capture"]}])[0]
        assert kept["gender"] == "female" and kept["capture_frames"] == 12
        remember_gender(capture, "male")
        assert read_clips("demo")[0]["gender"] == "male"
        for value in ("any", None):
            cleared = write_clips("demo", meta, 30.0,
                                  [{"in": 32, "out": 60, "gender": value}])[0]
            assert cleared["gender"] == value and capture_gender(capture) == value
        prior_bytes = open(clips_path("demo"), "rb").read()
        for invalid in ("unclassified", "unknown", "", 0, False, [], {}):
            try: write_clips("demo", meta, 30.0, [{"in": 32, "out": 60, "gender": invalid}])
            except ValueError: pass
            else: raise AssertionError(f"accepted invalid gender {invalid!r}")
            assert open(clips_path("demo"), "rb").read() == prior_bytes

        # Local content identity reopens marks even when its filename and title change.
        from motion_artist import source_metadata, bundle_manifest
        ordinary = os.path.join(d, "ordinary.mp4")
        generated_dir = os.path.join(d, "generated"); os.makedirs(generated_dir)
        generated = os.path.join(generated_dir, "source.mp4")
        for p, color in ((ordinary, "red"), (generated, "blue")):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=96x128:r=24",
                            "-frames:v", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p", p], check=True)
        first = open_video("", "", "emote", ordinary, "Victory")
        assert first["creator"] == "Local" and "id" not in first
        lc = write_clips(first["key"], first, 24, [{"in": 1, "out": 24, "fps": 12, "playback": "one-shot"}])
        import io
        payload = open(ordinary, "rb").read()
        uploaded = open_upload(io.BytesIO(payload), len(payload), "different-name.m4v", "emote")
        assert uploaded["key"] == first["key"] and uploaded["clips"] == lc
        before = sorted(os.listdir(WORK))
        for length, genre in ((0, "emote"), (len(payload), ""), (len(payload) + 1, "emote")):
            try: open_upload(io.BytesIO(payload), length, "interrupted.mp4", genre)
            except ValueError: pass
            else: raise AssertionError("invalid or interrupted upload was imported")
            assert sorted(os.listdir(WORK)) == before
        invalid = b"not a video"
        try: open_upload(io.BytesIO(invalid), len(invalid), "invalid.mp4", "emote")
        except (ValueError, subprocess.CalledProcessError): pass
        else: raise AssertionError("invalid footage was imported")
        assert sorted(os.listdir(WORK)) == before
        copy = os.path.join(d, "another-name.mp4"); shutil.copyfile(ordinary, copy)
        again = open_video("", "", "emote", copy, "Another title")
        assert again["key"] == first["key"] and again["clips"] == lc
        assert open_video("", first["key"], "actions")["genre"] == "actions"
        payload = open(generated, "rb").read()
        collision = open_upload(io.BytesIO(payload), len(payload), "../../Victory.mp4", "emote")
        assert collision["key"] == "local-victory-2"
        assert collision["title"] == "Victory" and collision["source_sha256"] == sha256(generated)
        assert open_video("", "", "emote", generated, "Victory")["key"] == collision["key"]
        # A generated source carries verified provenance through reopening and saving marks.
        ai_dir = os.path.join(d, "ai"); os.makedirs(ai_dir)
        ai = os.path.join(ai_dir, "source.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=green:s=96x128:r=24",
                        "-frames:v", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p", ai], check=True)
        generation = dict(status="complete", source_sha256=sha256(ai), prompt_id="job-test",
                          settings=dict(title="Victory", source_frames=121, requested_prompt="fist pump"))
        atomic_json(os.path.join(ai_dir, "generation.json"), generation)
        state = open_video("", "", "emote", ai)
        assert state["creator"] == "Local AI" and state["key"] == "local-ai-victory" and "id" not in state
        clips = write_clips(state["key"], state, 24, [{"in": 1, "out": 24, "fps": 12}])
        assert clips[0]["capture_frames"] == 12 and clips[0]["speed_factor"] == 1
        marks = json.load(open(clips_path(state["key"])))
        assert marks["generation"] == generation and "video_id" not in marks
        reopened = open_video("", state["key"], "emote")
        assert reopened["generation"] == generation and reopened["clips"] == clips
        payload = open(ai, "rb").read()
        browser_again = open_upload(io.BytesIO(payload), len(payload), "renamed.mp4", "emote")
        assert browser_again["generation"] == generation and browser_again["clips"] == clips
        assert browser_again["source_kind"] == "ai-generated"
        src = source_of(video_dir(state["key"]))
        provenance = source_metadata(src)
        assert provenance["generation"] == generation and provenance["url"] == ""
        clips[0]["motion"] = "emote-01"
        atomic_json(clips_path(state["key"]), {**marks, "clips": clips})
        try: open_video("", state["key"], "actions")
        except ValueError as e: assert "emote-01" in str(e)
        else: raise AssertionError("generated cut clip changed genre")
        with open(src, "ab") as fh: fh.write(b"changed")
        try: source_metadata(src)
        except ValueError: pass
        else: raise AssertionError("changed source passed provenance verification")
        try: open_video("", state["key"], "emote")
        except ValueError: pass
        else: raise AssertionError("changed source reopened its marks")
        assert source_metadata(os.path.join(d, "ordinary.mp4"), "https://youtube.com/watch?v=abc")["source_kind"] == "youtube"

        # Deletion requires confirmation, retains recoverable files, and leaves bundles alone.
        from pathlib import Path
        EXPORTS = os.path.join(d, "exports")
        bundle = Path(EXPORTS) / "test-01"; bundle.mkdir(parents=True)
        (bundle / "manifest.json").write_text('{}')
        doomed, _ = video("delete-test", "Delete me", "Local", "test")
        original = Path(video_dir(doomed))
        (original / "source.mp4").write_bytes(b"original footage")
        before = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob('*') if p.is_file()}
        for key, confirmation in ((doomed, False), (doomed, "true"), ("..", True),
                                   ("../outside", True), ("", True)):
            try: delete_video(key, confirmation)
            except ValueError: pass
            else: raise AssertionError("unconfirmed or invalid deletion succeeded")
            assert original.exists()
        linked = Path(WORK) / "linked-video"; linked.symlink_to(original, target_is_directory=True)
        try: delete_video(linked.name, True)
        except ValueError: pass
        else: raise AssertionError("deleted through a linked directory")
        linked.unlink()
        deleted = delete_video(doomed, True)
        retained = Path(deleted["trash"])
        assert not original.exists() and doomed not in {v["key"] for v in deleted["videos"]}
        assert before == {str(p.relative_to(retained)): p.read_bytes() for p in retained.rglob('*') if p.is_file()}
        assert (bundle / "manifest.json").read_text() == '{}'
        assert read_meta(first["key"]) and os.path.isfile(ordinary)
        try: delete_video(doomed, True)
        except ValueError: pass
        else: raise AssertionError("deleted an already removed video")

    # The custom track replaced the range input. Nothing here runs the page, but a
    # half-finished refactor leaves a dead $('scrub') that only throws in a browser.
    for part in ("id=track", "id=band", "id=head", "id=hin", "id=hout", "id=pmode", "id=capfps",
                 "id=pcap", "id=vid", "id=genre", "id=cancel", "onpointerdown", "getBoundingClientRect"):
        assert part in PAGE, part
    # the select must offer exactly what write_clips accepts, or a choice the user
    # can make is a choice this file rejects
    playback_select = re.search(r"<select id=pmode\b.*?</select>", PAGE, re.S)[0]
    bare = playback_select.count("<option value=") - playback_select.count('<option value="')
    assert bare == len(CHOICES), bare
    for part in CHOICES:
        assert f"<option value={part}>" in PAGE, part
    assert "'scrub'" not in PAGE and "type=range" not in PAGE
    # the clip name is gone: a clip is its place under its video, and a stale name box would
    # be a second identity for the same clip
    assert "'cname'" not in PAGE and "clip name" not in PAGE
    # the page names clips clip-NN and never guesses a motion number -- only extract allocates
    assert "clipName" in PAGE and "S.next" not in PAGE
    assert "<th>clip</th><th>motion</th>" in PAGE
    # the page keys frames and saves by the video's directory, never by a typed set name,
    # and the picker reads creator-first so the label and the folder read alike
    assert "/api/frame?video=" in PAGE and "set:" not in PAGE
    assert "v.creator + ' - '" in PAGE and "v.key" in PAGE
    # Play shows the footage, and ping-pong walks those frames out and back. A capture
    # preview left half-wired, or a lost return leg, shows up only in a browser.
    assert "captureFrame" not in PAGE
    assert "order.push(order[q])" in PAGE
    assert 'type=file' in PAGE and "$('file').files[0]" in PAGE and "/api/upload?" in PAGE
    print("ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; pass 0.0.0.0 to reach it from other machines on the LAN")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--source", help="approved local video to import or reopen")
    p.add_argument("--genre", help="genre for --source")
    p.add_argument("--title", help="local video title (default generation title or filename)")
    a = p.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    outside_repo(WORK); os.makedirs(WORK, exist_ok=True)
    if a.source and not a.genre: p.error("--source needs --genre")
    if not a.source and (a.genre or a.title): p.error("--genre and --title need --source")
    # Bind before announcing: printing the URL first claimed success and then traced back on a
    # port already held by an earlier clipper — whose page was serving fine all along.
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE: raise
        if a.source:
            req = urllib.request.Request(f"http://127.0.0.1:{a.port}/api/open",
                    data=json.dumps(dict(source=os.path.abspath(os.path.expanduser(a.source)), genre=a.genre, title=a.title)).encode(),
                    headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r: state = json.load(r)
            url = f"http://localhost:{a.port}/?video=" + urllib.parse.quote(state["key"])
            print(url); webbrowser.open(url); sys.exit(0)
        sys.exit(f"port {a.port} is already in use — an earlier clipper is likely still serving "
                 f"http://localhost:{a.port}. Open it, or pass --port.")
    shown = "localhost" if a.host in ("127.0.0.1", "localhost") else a.host
    print(f"clipper on http://{shown}:{a.port}  (ctrl-c to stop)")
    initial = open_video("", "", a.genre, a.source, a.title) if a.source else None
    url = f"http://localhost:{a.port}/" + ("?video=" + urllib.parse.quote(initial["key"]) if initial else "")
    webbrowser.open(url)
    srv.serve_forever()
