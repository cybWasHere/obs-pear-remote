#!/usr/bin/env python3
"""Serve the widgets like `python3 -m http.server`, plus /mpris.json: the YouTube video playing
in your browser, read from MPRIS (Linux only; elsewhere it always answers {}). The card shows it
when Pear has nothing playing.

    python3 extras/serve.py 9870 --bind 127.0.0.1 --directory .

/mpris.json is readable by pages this server serves, and by nobody else. To let another local page
read it (a start page served on its own port), name its origin: --allow-origin http://127.0.0.1:9875

It also plays internet radio for the remote's Radio tab, if mpv is installed: GET /radio.json is
the station list (radios.json) and what is on, POST /radio starts or stops a station. One mpv, owned
by this server, so every open remote shows and controls the same radio.
"""
import argparse, functools, json, os, re, shutil, signal, socket, subprocess, sys, tempfile, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WATCH = re.compile(r"^https://(www\.|m\.)?youtube\.com/(watch|shorts/|live/)|^https://youtu\.be/")
_cache = (0.0, b"{}")
ALLOWED = set()    # extra origins that may read /mpris.json, from --allow-origin


def busctl(*args):
    out = subprocess.run(["busctl", "--user", "--json=short", *args],
                         capture_output=True, text=True, timeout=2)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout else None


def video_id(url):
    u = urlparse(url)
    if u.hostname == "youtu.be":
        return u.path.strip("/")
    if "v" in parse_qs(u.query):
        return parse_qs(u.query)["v"][0]
    parts = u.path.strip("/").split("/")
    return parts[1] if len(parts) > 1 else ""


def youtube_now():
    """The best playing YouTube tab across MPRIS players. Plasma browser integration gives the
    clean title and the channel; Firefox's own entry only has "<title> - YouTube"."""
    try:
        names = [p["name"] for p in busctl("list") or [] if p["name"].startswith("org.mpris.MediaPlayer2.")]
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    best = None
    for name in names:
        try:
            props = busctl("call", name, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties",
                           "GetAll", "s", "org.mpris.MediaPlayer2.Player")
        except (subprocess.SubprocessError, ValueError):
            continue
        if not props:
            continue
        p = {k: v["data"] for k, v in props["data"][0].items()}
        meta = {k: v["data"] for k, v in p.get("Metadata", {}).items()}
        url = meta.get("xesam:url", "")
        if p.get("PlaybackStatus") != "Playing" or not WATCH.match(url):
            continue
        artist = ", ".join(a for a in meta.get("xesam:artist", []) if a)
        vid = video_id(url)
        cand = {"id": vid, "url": url, "author": artist,
                "title": re.sub(r" - YouTube$", "", meta.get("xesam:title", "")),
                "cover": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""}
        if best is None or (artist and not best["author"]):
            best = cand
    return best or {}


class Radio:
    """One mpv playing one station. Stations come from radios.json and are named by id, so a page
    can never hand mpv a URL of its own."""

    def __init__(self, path):
        self.path = Path(path)
        self.sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                                 f"obs-pear-remote-radio-{os.getpid()}.sock")
        self.lock = threading.Lock()
        self.proc = None
        self.playing = None
        self.volume = 100
        self.muted = False

    def stations(self):
        try:    # read every time: the file is small, and editing it needs no restart
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def ipc(self, *command):
        """mpv's JSON IPC; None when mpv isn't there or (Windows) the socket is a named pipe."""
        if not hasattr(socket, "AF_UNIX"):
            return None
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(0.5)
                s.connect(self.sock)
                s.sendall(json.dumps({"command": command, "request_id": 1}).encode() + b"\n")
                buf = b""
                while True:    # events share the socket; ours is the line with the request id
                    while b"\n" not in buf:
                        chunk = s.recv(4096)
                        if not chunk:
                            return None
                        buf += chunk
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line)
                    if msg.get("request_id") == 1:
                        return msg.get("data") if msg.get("error") == "success" else None
        except (OSError, ValueError):
            return None

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = self.playing = None

    def stop(self):
        with self.lock:
            self._stop()

    def play(self, station_id):
        url = next((s["url"] for s in self.stations() if s["id"] == station_id), None)
        if not url or not shutil.which("mpv"):
            return False
        with self.lock:
            self._stop()
            self.proc = subprocess.Popen(
                ["mpv", "--no-config", "--no-video", "--no-terminal", "--audio-client-name=Pear Remote Radio",
                 f"--volume={self.volume}", f"--mute={'yes' if self.muted else 'no'}",
                 f"--input-ipc-server={self.sock}", "--", url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.playing = station_id
        return True

    def set(self, volume=None, muted=None):
        if volume is not None:
            self.volume = max(0, min(100, int(volume)))
            self.ipc("set_property", "volume", self.volume)
        if muted is not None:
            self.muted = bool(muted)
            self.ipc("set_property", "mute", self.muted)

    def state(self):
        with self.lock:
            if self.proc and self.proc.poll() is not None:    # the stream ended or mpv died
                self.proc = self.playing = None
            playing = self.playing
        return {"available": bool(shutil.which("mpv")), "playing": playing,
                "title": (self.ipc("get_property", "metadata/by-key/icy-title") or "") if playing else "",
                "volume": self.volume, "muted": self.muted,
                "stations": [{k: s.get(k, "") for k in ("id", "group", "name", "note")} for s in self.stations()]}


RADIO = None    # set in main


class Handler(SimpleHTTPRequestHandler):
    def local(self):
        """The Host values that are this server. What you are watching or listening to is not for
        other websites: no wildcard CORS, and on loopback refuse a Host that is not ours (a
        foreign name pointed at 127.0.0.1 would be same-origin)."""
        host, port = self.server.server_address[:2]
        local = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        if host in ("127.0.0.1", "::1") and self.headers.get("Host") not in local:
            self.send_error(403)
            return None
        return local

    def send_json(self, body, local):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if self.headers.get("Origin") in {"http://" + h for h in local} | ALLOWED:    # localhost vs 127.0.0.1
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/mpris.json", "/radio.json"):
            return super().do_GET()
        local = self.local()
        if local is None:
            return
        if path == "/radio.json":
            return self.send_json(json.dumps(RADIO.state()).encode(), local)
        global _cache
        if time.monotonic() - _cache[0] > 0.8:    # the card polls every second
            _cache = (time.monotonic(), json.dumps(youtube_now()).encode())
        self.send_json(_cache[1], local)

    def do_POST(self):
        """/radio, body {"play": id} | {"stop": true}, and/or {"volume": 0-100}, {"muted": bool}.
        Only for the pages this server serves: a JSON body can't be sent cross-origin without a
        preflight, which nothing here answers, and an Origin that isn't ours is refused outright."""
        if urlparse(self.path).path != "/radio":
            return self.send_error(404)
        local = self.local()
        if local is None:
            return
        origin = self.headers.get("Origin")
        if (origin is not None and origin not in {"http://" + h for h in local}) \
                or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.send_error(403)
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)))
            RADIO.set(req.get("volume"), req.get("muted"))
            if req.get("stop"):
                RADIO.stop()
            elif "play" in req and not RADIO.play(req["play"]):
                return self.send_error(404, "unknown station, or mpv is not installed")
        except (ValueError, TypeError, AttributeError):
            return self.send_error(400)
        self.send_json(json.dumps(RADIO.state()).encode(), local)

    def log_message(self, fmt, *args):
        if not getattr(self, "path", "").startswith(("/mpris.json", "/radio")):    # unset on a malformed request
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int, nargs="?", default=9870)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--directory", default=".")
    ap.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                    help="another origin that may read /mpris.json, e.g. http://127.0.0.1:9875 (repeatable)")
    ap.add_argument("--radios", default=Path(__file__).resolve().parent.parent / "radios.json",
                    help="the station list for the remote's Radio tab (default: radios.json next to the pages)")
    a = ap.parse_args()
    ALLOWED.update(o.rstrip("/") for o in a.allow_origin)
    RADIO = Radio(a.radios)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))    # so the finally below runs
    handler = functools.partial(Handler, directory=a.directory)
    with ThreadingHTTPServer((a.bind, a.port), handler) as httpd:
        print(f"serving {a.directory} on http://{a.bind}:{a.port}/ (+ /mpris.json, /radio.json)")
        try:
            httpd.serve_forever()
        finally:
            RADIO.stop()    # the radio doesn't outlive its only remote control
