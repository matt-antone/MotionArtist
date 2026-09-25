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
import argparse, errno, json, os, re, subprocess, sys, urllib.parse, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from motion_artist import fetch  # reuse the 720-capped yt-dlp download

ROOT = os.getcwd()
WORK = os.path.join(ROOT, "work")
EXPORTS = os.path.join(ROOT, "exports")
FRAME_HEIGHT = 480  # display copies; extract re-reads the source video at full resolution
PLAYBACK = ("loop", "one-shot", "final-hold")  # extract's own --playback choices, not a second vocabulary
# ping-pong is offered beside them but is not one of them: it is `extract --pingpong`, a
# separate flag on the same command. It rides in the clip as its own field.
PINGPONG = "ping-pong"
CHOICES = PLAYBACK + (PINGPONG,)
CAPTURE_FPS = 12  # CAG's editor default; the skill keeps fps fixed across a library


def slug(s):
    """A name safe to use as a directory: lowercase, dashes, nothing else."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def probe_fps(path):
    """Exact source fps as a float. ffprobe reports it as a rational ('30000/1001')."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    num, _, den = r.stdout.strip().partition("/")
    return float(num) / float(den or 1)


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
    return os.path.join(WORK, key)


def video_key(creator, title, vid):
    """The directory a video works in: `<creator>-<title>`, slugged.

    A readable name is worth having -- an id says nothing about which video it is -- but
    it is not an identifier: a creator can post two videos under one title, and a
    re-upload shares both. The id decides. A directory whose meta.json names a different
    video is not this video's, so this video takes the next free `-2`, `-3` … rather than
    opening the other one's frames and reading its marks against them.

    An empty title and creator leave nothing to name a directory after, which only the
    id can answer.
    """
    base = slug(f"{creator} {title}") or vid
    key, n = base, 1
    while True:
        m = read_meta(key)
        if not m or m.get("id") == vid:
            return key
        n += 1
        key = f"{base}-{n}"


def source_of(vdir):
    for f in sorted(os.listdir(vdir)):
        if f.startswith("source-"):
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


def read_clips(key):
    p = clips_path(key)
    return json.load(open(p))["clips"] if os.path.exists(p) else []


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
                    "clips": len(read_clips(key))})
    return sorted(out, key=lambda v: (v["creator"].lower(), v["title"].lower()))


def genres():
    """Genres already in use, so the box offers them rather than inviting a near-miss."""
    seen = {v["genre"] for v in videos() if v["genre"]}
    if os.path.isdir(EXPORTS):
        seen |= {d for d in os.listdir(EXPORTS) if os.path.isdir(os.path.join(EXPORTS, d))}
    return sorted(seen)


def open_video(url, key, genre):
    """Download (once) and split (once). Returns the viewer's state for this video.

    Opening by key reads back a video already here; opening by URL resolves the video
    first and `video_key` turns it into a directory, so pasting the URL of a video already
    downloaded reopens it rather than fetching a second copy. Nothing needs confirming and
    nothing is ever replaced: the name comes from the video, not from something typed, and
    a name already taken by a different video goes to the next free suffix.
    """
    if not key:
        if not url:
            raise ValueError("pick a video, or paste a YouTube URL")
        vid, title, creator = probe_video(url)
        key = video_key(creator, title, vid)
    else:
        m = read_meta(key)
        if not m:
            raise ValueError(f"no video here called {key!r}")
        vid, title, creator = m.get("id", ""), m.get("title", key), m.get("creator", "")
        url = m.get("url", url)

    vdir = video_dir(key)
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
        if not url:
            raise ValueError("no video downloaded yet, and no URL given")
        src, _ = fetch(url, vdir)

    frames_dir = os.path.join(vdir, "frames")
    if not os.path.isdir(frames_dir) or not os.listdir(frames_dir):
        os.makedirs(frames_dir, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src,
                        "-vf", f"scale=-2:{FRAME_HEIGHT}", "-q:v", "4",
                        os.path.join(frames_dir, "%06d.jpg")], check=True)

    # the id rides in meta.json, not in the directory name: it is what decides whether the
    # next URL pasted is this video, and the name is only what makes the directory legible.
    meta = {"key": key, "id": vid, "url": url, "title": title, "creator": creator,
            "genre": genre}
    with open(os.path.join(vdir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

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
    out, seen = [], set()
    for i, c in enumerate(clips, 1):
        a, b = int(c["in"]), int(c["out"])
        name = f"clip-{i:02d}"
        motion = c.get("motion") or ""
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
                "playback": pb, "pingpong": pingpong,
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
    with open(clips_path(key), "w") as fh:
        json.dump({"video": key, "video_id": meta.get("id", ""), "url": meta.get("url", ""),
                   "title": meta.get("title", ""), "creator": meta.get("creator", ""),
                   "genre": genre, "source_fps": round(fps, 4), "clips": out}, fh, indent=2)
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
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/api/open":
                return self.send(200, json.dumps(open_video(body.get("url", "").strip(),
                                                            os.path.basename(body.get("video", "")),
                                                            body.get("genre", ""))))
            if self.path == "/api/clips":
                key = os.path.basename(body.get("video", ""))
                meta = read_meta(key)
                if not meta:
                    raise ValueError("open a video first")
                saved = write_clips(key, meta, float(body["fps"]), body.get("clips", []))
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
  <input id=genre placeholder="genre (hiphop, karate)" size=18 list=genres>
  <button class=go id=load>Open</button>
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
    <button class=go id=add>Add clip</button>
    <button id=cancel hidden>Cancel edit</button>
  </div>
  <div class=hint><kbd>←</kbd><kbd>→</kbd> step · <kbd>shift</kbd>+arrows ×10 · <kbd>I</kbd> in ·
    <kbd>O</kbd> out · <kbd>space</kbd> play footage · <kbd>C</kbd> play capture · <kbd>enter</kbd> add clip ·
    drag the green marks on the track to move in and out</div>

  <table><thead><tr><th>clip</th><th>motion</th><th>in</th><th>out</th><th>frames</th><th>seconds</th><th>capture</th><th>playback</th><th></th></tr></thead>
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
  $('vid').innerHTML = '<option value="">— new video —</option>' +
    vs.map(v=>`<option value="${v.key}">${label(v)}</option>`).join('');
  $('vid').value = keep || '';
  $('genres').innerHTML = gs.map(g=>`<option value="${g}">`).join('');
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
  setText('status', ''); $('saved').innerHTML = ''; err('');
}
$('vid').onchange = () => {
  clearPlayer();
  $('url').value = ''; $('genre').value = '';
  $('url').disabled = !!$('vid').value;
  if (!$('vid').value) $('url').focus();
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
function endEdit(){ S.editing = -1; editLabel(); rows(); }
$('cancel').onclick = () => { S.in = S.out = null; marks(); endEdit(); };

function rows(){
  $('list').innerHTML = S.clips.map((c,i)=>`<tr class="${i===S.editing?'editing':''}">
    <td>${clipName(i)}</td><td class=n>${c.motion || '—'}</td>
    <td class=n>${c.in}</td><td class=n>${c.out}</td>
    <td class=n>${c.out-c.in+1}</td>
    <td class=n>${((c.in-1)/S.fps).toFixed(2)}–${(c.out/S.fps).toFixed(2)}</td>
    <td class=n>${capFrames(c.in, c.out, c.fps)}f @ ${c.fps}</td>
    <td class=n>${c.playback}</td>
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
    S.editing = +go;            // re-cut: Add replaces this motion and keeps its number
    marks(); show(S.in); editLabel(); rows(); }
};

async function post(path, body){
  const r = await fetch(path, {method:'POST', body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

$('load').onclick = async () => {
  clearPlayer(); setText('status', 'downloading and splitting frames…');
  try {
    const j = await post('/api/open', {video: $('vid').value, url: $('url').value,
                                       genre: $('genre').value});
    S = {...S, video:j.key, genre:j.genre, url:j.url, fps:j.fps, frames:j.frames,
         in:null, out:null, editing:-1,
         clips: j.clips.map(c=>({motion:c.motion || '', in:c.in_frame, out:c.out_frame,
                                playback: c.pingpong ? 'ping-pong' : (c.playback || 'loop'),
                                fps: c.capture_fps || 12}))};
    fillVideos(j.videos, j.genres, j.key);
    $('url').value = j.url; $('url').disabled = true; $('genre').value = j.genre;
    setText('ftot', j.frames);
    setText('status', `${j.creator ? j.creator + ' - ' : ''}${j.title} · ${j.frames} frames @ ${j.fps.toFixed(3)} fps`);
    $('editor').hidden = false; marks(); rows(); editLabel(); show(1);
  } catch(e){ setText('status',''); err(e.message); }
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
                playback: $('pmode').value, fps: capFps()};
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
  const typing = /^(INPUT|TEXTAREA)$/.test(e.target.tagName);
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

fetch('/api/videos').then(r=>r.json()).then(j => fillVideos(j.videos, j.genres));
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
                          "start": 0.0, "end": 0.033, "playback": "loop", "pingpong": False,
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

    # The custom track replaced the range input. Nothing here runs the page, but a
    # half-finished refactor leaves a dead $('scrub') that only throws in a browser.
    for part in ("id=track", "id=band", "id=head", "id=hin", "id=hout", "id=pmode", "id=capfps",
                 "id=pcap", "id=vid", "id=genre", "id=cancel", "onpointerdown", "getBoundingClientRect"):
        assert part in PAGE, part
    # the select must offer exactly what write_clips accepts, or a choice the user
    # can make is a choice this file rejects
    bare = PAGE.count("<option value=") - PAGE.count('<option value="')  # the lists are quoted
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
    print("ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; pass 0.0.0.0 to reach it from other machines on the LAN")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    os.makedirs(WORK, exist_ok=True)
    # Bind before announcing: printing the URL first claimed success and then traced back on a
    # port already held by an earlier clipper — whose page was serving fine all along.
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE: raise
        sys.exit(f"port {a.port} is already in use — an earlier clipper is likely still serving "
                 f"http://localhost:{a.port}. Open it, or pass --port.")
    shown = "localhost" if a.host in ("127.0.0.1", "localhost") else a.host
    print(f"clipper on http://{shown}:{a.port}  (ctrl-c to stop)")
    webbrowser.open(f"http://localhost:{a.port}")
    srv.serve_forever()
