"""`sightline inspect`: a local page to build locators and assets against the live target.

- live view of the target screen (auto-refresh), with zoom-to-fit;
- type a locator (`text:Valider`, `image:save || text:Enregistrer`, `exact:OK[2]`...) and
  see every match boxed, the one a click would use, and the diagnostics when nothing
  matches ("best 0.86 at ..." / "closest OCR text ...");
- OCR overlay: every phrase OCR reads, with its box;
- drag a rectangle on the screen and save it as an image asset (flat crops refused),
  then copy the locator `image:<name>`;
- "test click" on the current locator.

Security: binds to 127.0.0.1 only and every request needs the random token in the URL.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

from .errors import SightlineError
from .geometry import Rect
from .locators import FindContext
from .spec import parse_locator
from .vision.match import Template

PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>sightline inspector</title>
<style>
:root{--bg:#f6f6f4;--panel:#fff;--fg:#1b1d21;--mute:#666b73;--line:#d9d9d4;--accent:#1e64c8;
--ok:#2e8b57;--other:#e8890c;--bad:#c62828;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--panel:#1b1e23;--fg:#e7e7e3;--mute:#9aa0a8;
--line:#30343b;--accent:#6aa3ff;--ok:#5cc28a;--other:#ffb04a;--bad:#ff7a7a}}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:8px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);
background:var(--panel);flex-wrap:wrap}
header b{margin-right:8px}input[type=text]{flex:1;min-width:240px;font:14px ui-monospace,monospace;
padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button,label.t{padding:7px 12px;border:1px solid var(--line);border-radius:6px;background:var(--bg);
color:var(--fg);cursor:pointer;font:inherit}button.p{background:var(--accent);border-color:var(--accent);color:#fff}
main{display:grid;grid-template-columns:1fr 340px;gap:0;height:calc(100vh - 56px)}
@media (max-width:900px){main{grid-template-columns:1fr;height:auto}}
#view{overflow:auto;position:relative;padding:12px}#wrap{position:relative;display:inline-block}
#shot{display:block;max-width:100%;border:1px solid var(--line);cursor:crosshair;user-select:none}
#ov{position:absolute;left:0;top:0;pointer-events:none}
aside{border-left:1px solid var(--line);background:var(--panel);padding:12px 14px;overflow:auto}
aside h3{margin:14px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
pre{white-space:pre-wrap;font:12px/1.45 ui-monospace,monospace;margin:0}
.row{display:flex;gap:6px;align-items:center;margin:6px 0}.mute{color:var(--mute)}
code{font:12px ui-monospace,monospace;background:var(--bg);padding:2px 5px;border-radius:4px;border:1px solid var(--line)}
</style>
<header><b>sightline inspector</b>
<input id="loc" type="text" placeholder="locator, e.g.  text:Valider   image:save || text:Save   exact:OK[2]" autofocus>
<button class="p" id="find">Find</button><button id="click">Test click</button>
<label class="t"><input type="checkbox" id="ocr"> OCR</label>
<label class="t"><input type="checkbox" id="live" checked> Live</label></header>
<main><section id="view"><div id="wrap"><img id="shot" alt="target screen"><canvas id="ov"></canvas></div></section>
<aside><h3>Result</h3><pre id="out" class="mute">Type a locator and press Enter.</pre>
<h3>New image asset</h3><p class="mute">Drag a rectangle on the screen.</p>
<div class="row"><input id="name" type="text" placeholder="name, e.g. toolbar/save"><button id="save">Save</button></div>
<pre id="saved"></pre></aside></main>
<script>
const T=new URLSearchParams(location.search).get("token");const $=id=>document.getElementById(id);
const shot=$("shot"),ov=$("ov"),ctx=ov.getContext("2d");let boxes=[],ocrBoxes=[],sel=null,drag=null,info={ox:0,oy:0},amb=false;
function api(p,body){return fetch(p+(p.includes("?")?"&":"?")+"token="+T,{method:body?"POST":"GET",
 headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined}).then(r=>r.json())}
function refresh(){const i=new Image();i.onload=()=>{shot.src=i.src;sizeOv()};i.src="/shot.png?token="+T+"&t="+Date.now();
 api("/info").then(j=>info=j)}
function sizeOv(){ov.width=shot.clientWidth;ov.height=shot.clientHeight;draw()}
function sc(){return shot.clientWidth/(shot.naturalWidth||1)}
function rect(r,c,w,label){const s=sc();ctx.strokeStyle=c;ctx.lineWidth=w;
 ctx.strokeRect((r[0]-info.ox)*s,(r[1]-info.oy)*s,r[2]*s,r[3]*s);if(label){ctx.fillStyle=c;ctx.font="11px system-ui";
 ctx.fillText(label,(r[0]-info.ox)*s,(r[1]-info.oy)*s-3)}}
function draw(){ctx.clearRect(0,0,ov.width,ov.height);const css=getComputedStyle(document.documentElement);
 ocrBoxes.forEach(b=>rect(b.rect,css.getPropertyValue("--mute"),1,b.text));
 boxes.forEach((b,i)=>rect(b.rect,css.getPropertyValue(amb?"--bad":(i==0?"--ok":"--other")),i==0?2:1,
  amb?"#"+(i+1)+" ambiguous":(i==0?"click target":"")));
 if(sel){const s=sc();ctx.setLineDash([5,3]);ctx.strokeStyle=css.getPropertyValue("--accent");ctx.lineWidth=2;
 ctx.strokeRect(sel[0]*s,sel[1]*s,sel[2]*s,sel[3]*s);ctx.setLineDash([])}}
function find(){const l=$("loc").value.trim();if(!l)return;api("/find",{locator:l}).then(j=>{boxes=j.matches||[];amb=!!j.ambiguous;
 $("out").className="";$("out").textContent=j.error?j.error:(j.matches.length+" match(es) in "+j.ms+" ms\n"+
 j.matches.map((m,i)=>(i+1)+". "+m.text).join("\n")+(j.diag.length?"\n\n"+j.diag.join("\n"):"")+(j.verdict?"\n\n"+j.verdict:""));draw()})}
$("find").onclick=find;$("loc").onkeydown=e=>{if(e.key=="Enter")find()};
$("click").onclick=()=>{const l=$("loc").value.trim();if(l)api("/click",{locator:l}).then(j=>{$("out").textContent=j.error||j.done;setTimeout(refresh,300)})};
$("ocr").onchange=e=>{if(!e.target.checked){ocrBoxes=[];draw();return}api("/ocr").then(j=>{ocrBoxes=j.phrases;draw()})};
shot.onmousedown=e=>{const s=sc(),r=shot.getBoundingClientRect();drag=[(e.clientX-r.left)/s,(e.clientY-r.top)/s];e.preventDefault()};
window.onmousemove=e=>{if(!drag)return;const s=sc(),r=shot.getBoundingClientRect();const x=(e.clientX-r.left)/s,y=(e.clientY-r.top)/s;
 sel=[Math.min(x,drag[0]),Math.min(y,drag[1]),Math.abs(x-drag[0]),Math.abs(y-drag[1])].map(Math.round);draw()};
window.onmouseup=()=>{drag=null};
$("save").onclick=()=>{if(!sel||sel[2]<4){$("saved").textContent="Drag a rectangle first.";return}
 api("/crop",{name:$("name").value.trim(),rect:sel}).then(j=>{$("saved").textContent=j.error||("saved "+j.path+"\nlocator: "+j.locator);
 if(j.locator){$("loc").value=j.locator;find()}})};
window.onresize=sizeOv;refresh();setInterval(()=>{if($("live").checked&&!drag)refresh()},1500);
</script></html>"""


class Inspector:
    def __init__(self, session, assets_dir: str = "assets"):
        self.s = session
        self.assets = Path(assets_dir)
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.frame = None

    # ---- operations (also usable without HTTP, for tests)
    def shot(self) -> bytes:
        with self.lock:
            self.frame = self.s.capture()
            ok, buf = cv2.imencode(".png", self.frame.pixels)
        return buf.tobytes()

    def info(self) -> dict:
        f = self.frame or self.s.capture()
        return {"ox": f.origin_x, "oy": f.origin_y, "w": f.width, "h": f.height}

    def find(self, locator: str) -> dict:
        import time
        with self.lock:
            loc = parse_locator(locator)
            frame = self.frame or self.s.capture()
            ctx = FindContext(frame, self.s)
            t = time.perf_counter()
            ms = sorted(loc.find(ctx), key=lambda m: -m.score)
            dt = round(1000 * (time.perf_counter() - t))
            verdict = ""
            if len(ms) > 1:
                try:
                    self.s.pick(ms, loc)
                    verdict = "A click would use match 1 (clearly the best)."
                except SightlineError as e:
                    verdict = "AMBIGUOUS - a click would fail: " + str(e).splitlines()[-1]
        return {"matches": [{"rect": [m.rect.x, m.rect.y, m.rect.w, m.rect.h], "text": str(m)}
                            for m in ms], "diag": ctx.diag, "ms": dt, "verdict": verdict,
                "ambiguous": verdict.startswith("AMBIGUOUS")}

    def click(self, locator: str) -> dict:
        with self.lock:
            m = self.s.click(parse_locator(locator), timeout=2)
        return {"done": f"clicked {m}"}

    def ocr(self) -> dict:
        with self.lock:
            return {"phrases": [{"text": t, "rect": [r.x, r.y, r.w, r.h]}
                                for t, r in self.s.read_words()]}

    def crop(self, name: str, rect: list) -> dict:
        if not name or ".." in name or name.startswith(("/", "\\")):
            raise SightlineError("give a relative name like toolbar/save")
        frame = self.frame or self.s.capture()
        x, y, w, h = (int(v) for v in rect)
        r = Rect(frame.origin_x + x, frame.origin_y + y, w, h)
        piece = frame.crop(r).pixels
        Template.from_array(piece, name)                       # refuses flat / tiny crops
        path = self.assets / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), piece)
        self.s.assets._cache.pop(name, None)
        return {"path": str(path), "locator": f"image:{name}"}

    # ---- HTTP
    def handler(self):
        insp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok_token(self, q) -> bool:
                if q.get("token", [""])[0] != insp.token:
                    self.send_error(403, "bad token")
                    return False
                return True

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._ok_token(q):
                    return
                try:
                    if u.path == "/":
                        body = PAGE.encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                    elif u.path == "/shot.png":
                        body = insp.shot()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Cache-Control", "no-store")
                    elif u.path == "/info":
                        return self._json(insp.info())
                    elif u.path == "/ocr":
                        return self._json(insp.ocr())
                    else:
                        return self.send_error(404)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except SightlineError as e:
                    self._json({"error": str(e)}, 200)

            def do_POST(self):
                u = urlparse(self.path)
                if not self._ok_token(parse_qs(u.query)):
                    return
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                    if u.path == "/find":
                        return self._json(insp.find(data["locator"]))
                    if u.path == "/click":
                        return self._json(insp.click(data["locator"]))
                    if u.path == "/crop":
                        return self._json(insp.crop(data.get("name", ""), data["rect"]))
                    return self.send_error(404)
                except (SightlineError, ValueError, KeyError) as e:
                    self._json({"error": str(e)})
        return H

    def serve(self, port: int = 0) -> tuple[HTTPServer, str]:
        server = HTTPServer(("127.0.0.1", port), self.handler())
        url = f"http://127.0.0.1:{server.server_address[1]}/?token={self.token}"
        return server, url
