#!/usr/bin/env python3
"""clipper: mark clip in/out points on a video, frame by frame, in a browser.

  motion_artist/scripts/clipper.py [--port 8765]

Open http://localhost:8765, name an animation set, paste a video URL. The tool
downloads it into work/<set>/, splits every source frame into work/<set>/frames/,
and serves a frame-by-frame viewer. Mark in and out, drag either mark along the frame
track to adjust it, name the clip, add it to the list. The list is saved to exports/<set>/clips.json, where each clip carries the
exact frame numbers and the seconds they correspond to.

That file is the handoff: `motion_artist.py extract <url> --start S --end S ...`
takes the seconds straight from it. This tool does not run the pipeline; it only
settles which frames the pipeline should be pointed at.
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


def source_of(set_dir):
    for f in sorted(os.listdir(set_dir)):
        if f.startswith("source-"):
            return os.path.join(set_dir, f)
    return None


def load_set(name, url):
    """Download (once) and split (once). Returns the viewer's state for this set."""
    set_dir = os.path.join(WORK, name)
    os.makedirs(set_dir, exist_ok=True)
    src = source_of(set_dir)
    if not src:
        if not url:
            raise ValueError("no video downloaded for this set yet, and no URL given")
        src, _ = fetch(url, set_dir)
        with open(os.path.join(set_dir, "source-url.txt"), "w") as fh:
            fh.write(url + "\n")

    frames_dir = os.path.join(set_dir, "frames")
    if not os.path.isdir(frames_dir) or not os.listdir(frames_dir):
        os.makedirs(frames_dir, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src,
                        "-vf", f"scale=-2:{FRAME_HEIGHT}", "-q:v", "4",
                        os.path.join(frames_dir, "%06d.jpg")], check=True)

    count = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    url_file = os.path.join(set_dir, "source-url.txt")
    return {"set": name, "frames": count, "fps": probe_fps(src),
            "url": open(url_file).read().strip() if os.path.exists(url_file) else (url or ""),
            "clips": read_clips(name)}


def clips_path(name):
    return os.path.join(EXPORTS, name, "clips.json")


def read_clips(name):
    p = clips_path(name)
    return json.load(open(p))["clips"] if os.path.exists(p) else []


def write_clips(name, url, fps, clips):
    """Each clip records frames and the seconds extract needs, plus where it will land.

    The capture's frame count is derived, never typed: `frames = fps * window`, and the
    window is the span the marks already fixed. Picking a frame count first and hunting a
    window to fit it is the mistake the skill calls invisible in the output.
    """
    out = []
    for c in clips:
        a, b = int(c["in"]), int(c["out"])
        cn = slug(c["name"]) or f"clip-{len(out) + 1}"
        pb = c.get("playback") or "loop"
        if pb not in PLAYBACK:  # this file is a command line; a bad value would reach extract as one
            raise ValueError(f"unknown playback {pb!r}, expected one of {', '.join(PLAYBACK)}")
        cap_fps = c.get("fps")                          # `or` would read a 0 fps as the default
        cap_fps = CAPTURE_FPS if cap_fps is None else int(cap_fps)
        if not 1 <= cap_fps <= 60:
            raise ValueError(f"capture fps {cap_fps} is outside 1..60")
        window = (b - a + 1) / fps                      # seconds of source the marks span
        cap_frames = max(1, round(window * cap_fps))
        out.append({"name": cn, "in_frame": a, "out_frame": b, "frames": b - a + 1,
                    "start": round((a - 1) / fps, 3), "end": round(b / fps, 3),
                    "playback": pb, "capture_fps": cap_fps, "capture_frames": cap_frames,
                    # span / (frames/fps). extract recomputes it; 1.0 here means the marks
                    # already land on a whole frame at this fps, so no rounding is hiding.
                    "speed_factor": round(window / (cap_frames / cap_fps), 3),
                    "export": f"exports/{name}/{cn}"})
    os.makedirs(os.path.dirname(clips_path(name)), exist_ok=True)
    with open(clips_path(name), "w") as fh:
        json.dump({"set": name, "url": url, "source_fps": round(fps, 4), "clips": out}, fh, indent=2)
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
        if u.path == "/api/sets":
            sets = sorted(d for d in os.listdir(WORK) if os.path.isdir(os.path.join(WORK, d, "frames"))) \
                if os.path.isdir(WORK) else []
            return self.send(200, json.dumps(sets))
        if u.path == "/api/frame":
            name, n = slug(q.get("set", [""])[0]), int(q.get("n", ["1"])[0])
            p = os.path.join(WORK, name, "frames", f"{n:06d}.jpg")
            if not os.path.exists(p):
                return self.send(404, b"", "image/jpeg")
            self.send(200, open(p, "rb").read(), "image/jpeg")
            return
        self.send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        name = slug(body.get("set", ""))
        if not name:
            return self.send(400, json.dumps({"error": "name the animation set first"}))
        try:
            if self.path == "/api/load":
                return self.send(200, json.dumps(load_set(name, body.get("url", "").strip())))
            if self.path == "/api/clips":
                saved = write_clips(name, body.get("url", ""), float(body["fps"]), body.get("clips", []))
                return self.send(200, json.dumps({"clips": saved, "path": clips_path(name)}))
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
 .hint{color:#6f707a;font-size:12px;margin-top:10px}
 .err{color:#ff8b8b;margin:8px 0;white-space:pre-wrap}
 .ok{color:#7dd3a0}
 kbd{background:#26272e;border:1px solid #3a3b44;border-radius:4px;padding:1px 5px;font-size:11px}
</style>
<div class=wrap>
<h1>clipper</h1>

<div class=row>
  <input id=set placeholder="animation set name" size=18>
  <input id=url placeholder="YouTube URL (first time only)" size=38>
  <button class=go id=load>Load</button>
  <span id=status></span>
</div>
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
    <input id=cname placeholder="clip name" size=20>
    <label title="capture fps. The frame count is derived from it: frames = fps x window">
      fps <input id=capfps type=number min=1 max=60 value=12 style=width:4.5em></label>
    <select id=pmode title="how the capture plays back: extract's --playback">
      <option value=loop>loop</option>
      <option value=one-shot>one-shot</option>
      <option value=final-hold>final-hold</option>
    </select>
    <button class=go id=add>Add clip</button>
  </div>
  <div class=hint><kbd>←</kbd><kbd>→</kbd> step · <kbd>shift</kbd>+arrows ×10 · <kbd>I</kbd> in ·
    <kbd>O</kbd> out · <kbd>space</kbd> play · <kbd>enter</kbd> add clip ·
    drag the green marks on the track to move in and out</div>

  <table><thead><tr><th>clip</th><th>in</th><th>out</th><th>frames</th><th>seconds</th><th>capture</th><th>playback</th><th></th></tr></thead>
  <tbody id=list></tbody></table>
  <div class=hint id=saved></div>
</div>
</div>
<script>
const $ = id => document.getElementById(id);
let S = {set:'', url:'', fps:30, frames:0, n:1, in:null, out:null, clips:[], timer:null};

const setText = (id,v) => $(id).textContent = v;
const err = m => $('err').textContent = m || '';

function show(n){
  S.n = Math.min(Math.max(1, n), S.frames);
  $('img').src = `/api/frame?set=${encodeURIComponent(S.set)}&n=${S.n}`;
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

// The frames extract will pick: n samples across the marked window. A loop's samples are
// exclusive of the end, so the last->first cut is one step like every other.
function captureFrame(i, n, loop){
  const step = (S.out - S.in + (loop ? 1 : 0)) / (loop ? n : Math.max(n - 1, 1));
  return Math.min(S.out, S.in + Math.round(i * step));
}

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
      (off ? ` · plays ${factor > 1 ? 'fast' : 'slow'} ${factor.toFixed(3)}x` : ' · exact');
    el.className = off ? 'warn' : 'ok';
  }
  place();
}

$('capfps').oninput = () => marks();  // rows carry their own fps
function rows(){
  $('list').innerHTML = S.clips.map((c,i)=>`<tr>
    <td>${c.name}</td><td class=n>${c.in}</td><td class=n>${c.out}</td>
    <td class=n>${c.out-c.in+1}</td>
    <td class=n>${((c.in-1)/S.fps).toFixed(2)}–${(c.out/S.fps).toFixed(2)}</td>
    <td class=n>${capFrames(c.in, c.out, c.fps)}f @ ${c.fps}</td>
    <td class=n>${c.playback}</td>
    <td><button data-go=${i}>go</button> <button data-del=${i}>×</button></td></tr>`).join('');
}
$('list').onclick = e => {
  const del = e.target.dataset.del, go = e.target.dataset.go;
  if (del !== undefined){ S.clips.splice(+del,1); rows(); save(); }
  if (go !== undefined){ const c = S.clips[+go];
    S.in = c.in; S.out = c.out; $('pmode').value = c.playback; $('capfps').value = c.fps;
    marks(); show(S.in); }
};

async function post(path, body){
  const r = await fetch(path, {method:'POST', body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

$('load').onclick = async () => {
  err(''); setText('status', 'downloading and splitting frames…');
  try {
    const j = await post('/api/load', {set: $('set').value, url: $('url').value});
    S = {...S, set:j.set, url:j.url, fps:j.fps, frames:j.frames, in:null, out:null,
         clips: j.clips.map(c=>({name:c.name, in:c.in_frame, out:c.out_frame,
                                playback: c.playback || 'loop', fps: c.capture_fps || 12}))};
    $('set').value = j.set; $('url').value = j.url;
    setText('ftot', j.frames);
    setText('status', `${j.frames} frames @ ${j.fps.toFixed(3)} fps`);
    $('editor').hidden = false; marks(); rows(); show(1);
  } catch(e){ setText('status',''); err(e.message); }
};

$('bin').onclick = () => { S.in = S.n; if (S.out && S.out < S.in) S.out = null; marks(); };
$('bout').onclick = () => { S.out = S.n; if (S.in && S.in > S.out) S.in = null; marks(); };
$('play').onclick = () => {
  if (S.timer){ clearInterval(S.timer); S.timer = null; setText('play','Play (space)'); return; }
  setText('play','Pause (space)');
  // With a window marked, play what the capture will be: its own frames at its own rate.
  // Stepping every source frame at the capture's fps would only be slow motion.
  const span = S.in && S.out && S.out >= S.in;
  const n = span ? capFrames(S.in, S.out, capFps()) : 0, loop = $('pmode').value === 'loop';
  let i = 0;
  S.timer = setInterval(() => {
    if (span) show(captureFrame(i++ % n, n, loop));
    else show(S.n >= S.frames ? 1 : S.n + 1);
  }, 1000 / (span ? capFps() : S.fps));
};

async function save(){
  try {
    const j = await post('/api/clips', {set:S.set, url:S.url, fps:S.fps, clips:S.clips});
    $('saved').innerHTML = `<span class=ok>saved</span> ${j.path}`;
  } catch(e){ err(e.message); }
}

$('add').onclick = () => {
  const name = $('cname').value.trim();
  if (!name) return err('name the clip');
  if (!S.in || !S.out) return err('mark an in and an out frame');
  if (S.out < S.in) return err('out frame is before in frame');
  err(''); S.clips.push({name, in:S.in, out:S.out, playback: $('pmode').value, fps: capFps()});
  $('cname').value = ''; S.in = S.out = null; marks(); rows(); save();
};

addEventListener('keydown', e => {
  if ($('editor').hidden) return;
  const typing = /^(INPUT|TEXTAREA)$/.test(e.target.tagName);
  if (typing && e.key !== 'Enter') return;
  const step = e.shiftKey ? 10 : 1;
  if (e.key === 'ArrowLeft'){ show(S.n - step); e.preventDefault(); }
  else if (e.key === 'ArrowRight'){ show(S.n + step); e.preventDefault(); }
  else if (e.key === ' '){ $('play').click(); e.preventDefault(); }
  else if (e.key === 'Enter'){ $('add').click(); e.preventDefault(); }
  else if (!typing && (e.key === 'i' || e.key === 'I')) $('bin').click();
  else if (!typing && (e.key === 'o' || e.key === 'O')) $('bout').click();
});

fetch('/api/sets').then(r=>r.json()).then(s => { if (s.length) $('set').setAttribute('list','sets'),
  document.body.insertAdjacentHTML('beforeend',
    `<datalist id=sets>${s.map(x=>`<option value="${x}">`).join('')}</datalist>`); });
</script>
"""


def selftest():
    assert slug("Club Male #1") == "club-male-1"
    assert slug("  --Easy  1--  ") == "easy-1"
    assert slug("") == ""
    # frame 1 is t=0; the out frame's end is the boundary after it, so a 1-frame
    # clip at 30fps spans 0.000-0.033 and extract sees a non-empty window.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        global EXPORTS
        EXPORTS = d
        out = write_clips("demo", "http://x", 30.0,
                          [{"name": "Step Touch", "in": 1, "out": 1},
                           {"name": "turn", "in": 31, "out": 60, "playback": "one-shot"},
                           {"name": "ragged", "in": 1, "out": 43}])
        assert out[0] == {"name": "step-touch", "in_frame": 1, "out_frame": 1, "frames": 1,
                          "start": 0.0, "end": 0.033, "playback": "loop",
                          "capture_fps": 12, "capture_frames": 1, "speed_factor": 0.4,
                          "export": "exports/demo/step-touch"}, out[0]
        assert out[1]["playback"] == "one-shot", out[1]
        # 30 source frames at 30 fps is a 1.000 s window; at 12 fps that is 12 frames and
        # 12/12 == 1.000 s, so the capture plays at the speed it was danced.
        assert out[1]["capture_frames"] == 12 and out[1]["speed_factor"] == 1.0, out[1]
        # 43 source frames is 1.4333 s, which is not a whole frame at 12 fps. The count is
        # rounded and speed_factor says so rather than the distortion going unrecorded.
        assert out[2]["capture_frames"] == 17 and out[2]["speed_factor"] != 1.0, out[2]
        # fps is per clip: the same window at a different fps is a different frame count,
        # and the seconds the marks fixed do not move
        two = write_clips("demo", "http://x", 30.0,
                          [{"name": "slow", "in": 31, "out": 60, "fps": 8},
                           {"name": "fast", "in": 31, "out": 60, "fps": 24}])
        assert [c["capture_frames"] for c in two] == [8, 24], two
        assert two[0]["start"] == two[1]["start"] and two[0]["end"] == two[1]["end"], two
        for bad in (0, 61):
            try:
                write_clips("demo", "http://x", 30.0, [{"name": "x", "in": 1, "out": 2, "fps": bad}])
            except ValueError:
                pass
            else:
                raise AssertionError(f"capture fps {bad} accepted")
        # a typo must not reach extract as --playback
        try:
            write_clips("demo", "http://x", 30.0, [{"name": "x", "in": 1, "out": 2, "playback": "looop"}])
        except ValueError as e:
            assert "looop" in str(e), e
        else:
            raise AssertionError("bad playback accepted")
        assert out[1]["start"] == 1.0 and out[1]["end"] == 2.0 and out[1]["frames"] == 30, out[1]
        saved = json.load(open(os.path.join(d, "demo", "clips.json")))
        assert saved["source_fps"] == 30.0 and "capture_fps" not in saved, saved
        assert [c["capture_fps"] for c in saved["clips"]] == [8, 24], saved
    # The custom track replaced the range input. Nothing here runs the page, but a
    # half-finished refactor leaves a dead $('scrub') that only throws in a browser.
    for part in ("id=track", "id=band", "id=head", "id=hin", "id=hout", "id=pmode", "id=capfps",
                 "onpointerdown", "getBoundingClientRect", "captureFrame"):
        assert part in PAGE, part
    # the select must offer exactly what write_clips accepts, or a choice the user
    # can make is a choice this file rejects
    bare = PAGE.count("<option value=") - PAGE.count('<option value="')  # the set list is quoted
    assert bare == len(PLAYBACK), bare
    for part in PLAYBACK:
        assert f"<option value={part}>" in PAGE, part
    assert "'scrub'" not in PAGE and "type=range" not in PAGE
    print("ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    os.makedirs(WORK, exist_ok=True)
    # Bind before announcing: printing the URL first claimed success and then traced back on a
    # port already held by an earlier clipper — whose page was serving fine all along.
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE: raise
        sys.exit(f"port {a.port} is already in use — an earlier clipper is likely still serving "
                 f"http://localhost:{a.port}. Open it, or pass --port.")
    print(f"clipper on http://localhost:{a.port}  (ctrl-c to stop)")
    webbrowser.open(f"http://localhost:{a.port}")
    srv.serve_forever()
