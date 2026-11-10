# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""
A small page for a phone on the home network: lights off / on, brightness, color, presets, animations.

Off unless switched on in Settings. The server answers only

    - clients with a private (home network) or loopback address, and
    - requests that carry the token: the page is opened as http://<pc>:8765/?k=<token>, and every call the page
      makes repeats it. Without the right token the answer is 403, whatever was asked.

    GET  /?k=TOKEN            the page
    GET  /api/state?k=TOKEN   {"master", "dark", "presets", "preset", "animation", "animations", "devices"}
    POST /api/do?k=TOKEN      body: one Director command ("toggle", "brightness 60", "preset Night",
                              "animate wave", "color ff8800"); answers {"reply": "ok ..."}

The HTTP threads never touch devices: `call(text)` is supplied by the application and runs the command on the
GUI thread (see app.py), returning its reply.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("rgbdeck.remote")

DEFAULT_PORT = 8765
MAX_BODY = 512
VERBS = ("toggle", "on", "off", "brightness", "preset", "next-preset", "animate", "color")  # what the page may ask

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>RGB Deck</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:#14161c;color:#e8e9ee;font:16px/1.4 system-ui,sans-serif;max-width:560px;margin:auto}
h1{font-size:20px;margin:4px 0 14px}
h2{font-size:12px;letter-spacing:.08em;color:#7d8398;margin:20px 0 8px;text-transform:uppercase}
.card{background:#1b1d24;border-radius:14px;padding:14px}
button{font:inherit;color:inherit;background:#262a35;border:0;border-radius:10px;padding:12px 14px;min-height:46px}
button.on{background:#7c6cff}
button:active{filter:brightness(1.3)}
#power{width:100%;font-size:18px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.sw{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}
.sw button{min-height:38px;padding:0}
input[type=range]{width:100%;height:36px;accent-color:#7c6cff}
input[type=color]{width:100%;height:46px;border:0;border-radius:10px;background:#262a35;padding:4px}
#msg{color:#7d8398;font-size:13px;min-height:1.4em;margin-top:12px;text-align:center}
</style></head><body>
<h1>RGB Deck</h1>
<div class="card"><button id="power">…</button></div>
<h2>Brightness <span id="bv"></span></h2>
<div class="card"><input id="bright" type="range" min="5" max="100" step="5"></div>
<h2>Color</h2>
<div class="card"><div class="sw" id="sw"></div><div style="height:8px"></div><input id="pick" type="color" value="#7c6cff"></div>
<h2>Presets</h2><div class="grid" id="presets"></div>
<h2>Animations</h2><div class="grid" id="anims"></div>
<div id="msg"></div>
<script>
const K = new URLSearchParams(location.search).get("k") || "";
const $ = id => document.getElementById(id);
let busy = false;
async function state(){
  try{
    const r = await fetch("api/state?k=" + encodeURIComponent(K));
    if(!r.ok) throw new Error(r.status == 403 ? "This link is not valid any more. Open the one shown in RGB Deck's settings." : "RGB Deck answered " + r.status);
    show(await r.json());
  }catch(e){ $("msg").textContent = e.message || "RGB Deck is not reachable"; }
}
async function act(cmd){
  try{
    const r = await fetch("api/do?k=" + encodeURIComponent(K), {method:"POST", body:cmd});
    const j = await r.json();
    $("msg").textContent = j.reply && j.reply.startsWith("ok") ? "" : (j.reply || "failed");
  }catch(e){ $("msg").textContent = "RGB Deck is not reachable"; }
  state();
}
function chips(el, items, current, cmd){
  el.textContent = "";
  for(const [key, label] of items){
    const b = document.createElement("button");
    b.textContent = label;
    if(key === current) b.className = "on";
    b.onclick = () => act(cmd(key));
    el.appendChild(b);
  }
}
function show(s){
  $("power").textContent = s.dark ? "Lights are off — turn on" : "Turn lights off";
  $("power").className = s.dark ? "" : "on";
  if(!busy){ $("bright").value = s.master; }
  $("bv").textContent = s.master + " %";
  chips($("presets"), s.presets.map(p => [p, p]), s.preset, p => "preset " + p);
  if(!s.presets.length) $("presets").textContent = "No presets saved yet";
  chips($("anims"), [[null, "Off"]].concat(s.animations.map(a => [a.kind, a.name])), s.animation, k => "animate " + (k || "off"));
  $("msg").textContent = "";
}
$("power").onclick = () => act("toggle");
$("bright").oninput = () => { busy = true; $("bv").textContent = $("bright").value + " %"; };
$("bright").onchange = () => { busy = false; act("brightness " + $("bright").value); };
$("pick").onchange = () => act("color " + $("pick").value.slice(1));
for(const c of ["ff2d55","ff6a00","ffc400","7dff00","00ff6a","00ffd5","00b4ff","7c6cff","ff2dff","ffffff","ffb38a","ff0000","00ff00","0000ff","8000ff","ff8fb1"]){
  const b = document.createElement("button"); b.style.background = "#" + c; b.onclick = () => act("color " + c); $("sw").appendChild(b);
}
state(); setInterval(state, 4000);
</script></body></html>
"""


class _Server(ThreadingHTTPServer):
    # On Windows "reuse address" would let a second program take the port over while we hold it.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def allowed_client(address: str) -> bool:
    """Only the home network may talk to the page: private, link-local or loopback addresses."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def new_token() -> str:
    return secrets.token_urlsafe(12)


def lan_address() -> str:
    """This PC's address on the home network, for the link shown to the user."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent: this only asks the OS which address it would use
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


class Remote:
    def __init__(self, call: Callable[[str], str], token: str, port: int = DEFAULT_PORT, host: str = "0.0.0.0"):
        self.call, self.token, self.port, self.host = call, token, port, host
        self._server: Optional[ThreadingHTTPServer] = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._server is not None

    def url(self) -> str:
        return f"http://{lan_address()}:{self.port}/?k={self.token}"

    def start(self) -> bool:
        if self._server is not None:
            return True
        remote = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RGBDeck"
            sys_version = ""

            def log_message(self, fmt, *args):
                log.debug("remote %s: " + fmt, self.client_address[0], *args)

            def _answer(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj):
                self._answer(code, json.dumps(obj).encode(), "application/json")

            def _checked(self):
                """The request's path if the caller may ask at all, else None (403 already sent)."""
                url = urlparse(self.path)
                key = (parse_qs(url.query).get("k") or [""])[0]
                ok = allowed_client(self.client_address[0]) and hmac.compare_digest(key.encode(), remote.token.encode())
                if not ok:
                    self._answer(403, b"forbidden", "text/plain")
                    return None
                return url.path

            def do_GET(self):  # noqa: N802
                path = self._checked()
                if path is None:
                    return
                if path in ("/", "/index.html"):
                    self._answer(200, PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    reply = remote.call("state")
                    if reply.startswith("ok "):
                        self._answer(200, reply[3:].encode(), "application/json")
                    else:
                        self._json(503, {"reply": reply})
                else:
                    self._answer(404, b"not found", "text/plain")

            def do_POST(self):  # noqa: N802
                path = self._checked()
                if path is None:
                    return
                if path != "/api/do":
                    self._answer(404, b"not found", "text/plain")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 < length <= MAX_BODY:
                    self._json(400, {"reply": "no command"})
                    return
                command = self.rfile.read(length).decode("utf-8", "replace").strip()
                if "\n" in command or "\r" in command or command.split(" ")[0] not in VERBS:
                    self._json(400, {"reply": "no such command"})
                    return
                self._json(200, {"reply": remote.call(command)})

        try:
            self._server = _Server((self.host, self.port), Handler)
        except OSError as e:
            self.error = str(e)
            log.warning("phone page not started on port %d: %s", self.port, e)
            return False
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="rgbdeck-remote", daemon=True).start()
        log.info("phone page on port %d", self.port)
        return True

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
