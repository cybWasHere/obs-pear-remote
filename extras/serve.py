#!/usr/bin/env python3
"""Serve the widgets like `python3 -m http.server`, plus /mpris.json: the YouTube video playing
in your browser, read from MPRIS (Linux only; elsewhere it always answers {}). The card shows it
when Pear has nothing playing.

    python3 extras/serve.py 9870 --bind 127.0.0.1 --directory .
"""
import argparse, functools, json, re, subprocess, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WATCH = re.compile(r"^https://(www\.|m\.)?youtube\.com/(watch|shorts/|live/)|^https://youtu\.be/")
_cache = (0.0, b"{}")


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


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/mpris.json":
            return super().do_GET()
        global _cache
        if time.monotonic() - _cache[0] > 0.8:    # the card polls every second
            _cache = (time.monotonic(), json.dumps(youtube_now()).encode())
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(_cache[1])))
        self.end_headers()
        self.wfile.write(_cache[1])

    def log_message(self, fmt, *args):
        if not self.path.startswith("/mpris.json"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int, nargs="?", default=9870)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--directory", default=".")
    a = ap.parse_args()
    handler = functools.partial(Handler, directory=a.directory)
    with ThreadingHTTPServer((a.bind, a.port), handler) as httpd:
        print(f"serving {a.directory} on http://{a.bind}:{a.port}/ (+ /mpris.json)")
        httpd.serve_forever()
