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

The Podcasts tab uses that mpv too: GET /podcast.json is the shows (podcasts.json) and the episode
that is on, GET /podcast-episodes.json?show=<id> a show's latest episodes from its feed, POST
/podcast plays, pauses and seeks.

GET /now.json is what that mpv is on, whichever tab started it: {} or {kind, id, title, author,
cover, paused}. POST /now with {"toggle": true} pauses it and goes on. For a program that shows
what is playing next to its own play button and needn't know a radio from a podcast.

And it is the remote's YouTube tab: GET /youtube.json is the video in your browser (playing or
paused, from MPRIS; with ?after=<its "rev"> the answer waits until that changes), POST /youtube
pauses, seeks or sets the volume of that video, or opens another one in your default browser. GET /youtube-home.json lists your YouTube home page; that needs yt-dlp
and your login, so it is off until you name the browser to borrow it from: --youtube-cookies firefox
GET /youtube-open.json is for extras/firefox-pip: Firefox waits there for the next video to open, and
plays it in its Picture-in-Picture window; while nobody waits, a video opens in a new tab.

The Twitch tab: GET /twitch.json is the channels you follow that are live, read with the login
Chatterino already has (off until you say --twitch). POST /twitch with {"open": channel} plays one
the way a YouTube video is played, and opens Chatterino on that channel's chat; Chatterino is closed
again when the stream is (extras/firefox-pip says when). A Chatterino you started yourself is left
alone, both ways. --no-twitch-chat keeps Chatterino out of it.

One thing plays at a time (Linux, where MPRIS says who is playing): start a song in Pear, a station
or an episode while a YouTube video or a Twitch stream plays in your browser, and the video is paused and its
Picture-in-Picture window closed (closing it is extras/firefox-pip's work); start the video again,
and the music is paused. --let-both-play turns that off.
"""
import argparse, functools, json, os, re, shutil, signal, socket, subprocess, sys, tempfile, threading, time
import hashlib, urllib.request, webbrowser
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MPRIS_PATH, MPRIS_PLAYER = "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player"
WATCH = re.compile(r"^https://(www\.|m\.)?youtube\.com/(watch|shorts/|live/)|^https://youtu\.be/")
CHANNEL = re.compile(r"^https://(?:www\.|m\.)?twitch\.tv/(\w{2,25})/?(?:[?#]|$)")
PLAYER = re.compile(r"^moz-extension://[^/]+/player\.html\?(?:[^#]*&)?channel=(\w{2,25})(?:[&#]|$)")
NOT_CHANNELS = {"directory", "videos", "settings", "search", "downloads", "subscriptions", "inventory", "drops",
                "wallet", "friends", "turbo", "jobs", "store", "prime", "login", "signup", "following"}
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


def channel(url):
    """The Twitch channel a page is on, lower case; None for any other address."""
    m = CHANNEL.match(url)
    if m:
        return m.group(1).lower() if m.group(1).lower() not in NOT_CHANNELS else None
    m = PLAYER.match(url)    # the page the extension "Alternate Player for Twitch.tv" plays a channel on
    return m.group(1).lower() if m else None


def players():
    """Every MPRIS player: (bus name, properties, metadata)."""
    try:
        names = [p["name"] for p in busctl("list") or [] if p["name"].startswith("org.mpris.MediaPlayer2.")]
    except (OSError, subprocess.SubprocessError, ValueError):
        return
    for name in names:
        try:
            props = busctl("call", name, MPRIS_PATH, "org.freedesktop.DBus.Properties",
                           "GetAll", "s", MPRIS_PLAYER)
        except (subprocess.SubprocessError, ValueError):
            continue
        if not props:
            continue
        p = {k: v["data"] for k, v in props["data"][0].items()}
        yield name, p, {k: v["data"] for k, v in p.get("Metadata", {}).items()}


def video_players():
    """Every MPRIS player that is on a YouTube video or a Twitch stream, playing or not: (bus name,
    properties, metadata, what the pages show of it)."""
    for name, p, meta in players():
        url = meta.get("xesam:url", "")
        author = ", ".join(a for a in meta.get("xesam:artist", []) if a)
        if WATCH.match(url):
            vid = video_id(url)
            yield name, p, meta, {"kind": "youtube", "id": vid, "url": url, "author": author,
                                  "title": re.sub(r" - YouTube$", "", meta.get("xesam:title", "")),
                                  "cover": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""}
        elif channel(url):
            live = TWITCH.known(channel(url)) if TWITCH else {}
            yield name, p, meta, {"kind": "twitch", "id": channel(url), "url": url,
                                  "author": live.get("name") or author or channel(url),
                                  "title": live.get("title") or re.sub(r" - Twitch$", "", meta.get("xesam:title", "")),
                                  "cover": live.get("thumb") or Twitch.thumb(channel(url))}


def youtube_players():
    return (v for v in video_players() if v[3]["kind"] == "youtube")


def youtube_now():
    """The best playing YouTube tab across MPRIS players. Plasma browser integration gives the
    clean title and the channel; Firefox's own entry only has "<title> - YouTube"."""
    best = None
    for _, p, _, cand in youtube_players():
        if p.get("PlaybackStatus") == "Playing" and (best is None or (cand["author"] and not best["author"])):
            best = cand
    return best or {}


class Tube:
    """YouTube in your browser, for the remote's YouTube tab: what it is on, a few controls over
    MPRIS, and your home page through yt-dlp. A page names a video by id and this builds the URL,
    so the browser is never handed an address of the page's choosing."""
    ID = re.compile(r"^[\w-]{11}$")

    def __init__(self, cookies):
        self.cookies = cookies    # yt-dlp's --cookies-from-browser value, or None: no home page
        self.lock = threading.Lock()
        self.home_at, self.home_items, self.home_note = 0.0, [], ""
        self.now_at, self.now = 0.0, (None, {})
        self.asked = threading.Condition()    # the video to open, for a browser that waits for it
        self.seq, self.asked_id = int(time.time() * 1000), None    # seq never repeats across restarts
        self.asked_twitch = None    # or the channel to open
        self.born = self.seq
        self.waiting, self.waited_at = 0, 0.0

    def current(self, fresh=False):
        """(bus name, state) of the video to show and control: a playing one before a paused one,
        then the player that knows the most (Plasma browser integration has the length, the position
        and the channel; Firefox's own entry has none of them)."""
        if not fresh and time.monotonic() - self.now_at < 0.8:    # the remote polls every other second
            return self.now
        best = None
        for name, p, meta, cand in video_players():
            playing = p.get("PlaybackStatus") == "Playing"
            length = meta.get("mpris:length") or 0
            if length > 1e15:    # a live stream: browsers give the largest number there is
                length = 0
            rank = (playing, bool(length), bool(cand["author"]))
            if best is None or rank > best[0]:
                best = (rank, name, dict(cand, playing=playing, length=length / 1e6,
                                         position=(p.get("Position") or 0) / 1e6,
                                         volume=round(100 * p["Volume"]) if "Volume" in p else None,
                                         trackid=meta.get("mpris:trackid")))
        self.now_at, self.now = time.monotonic(), (best[1], best[2]) if best else (None, {})
        return self.now

    def state(self, fresh=False):
        now = {k: v for k, v in self.current(fresh)[1].items() if k != "trackid"}
        return {"now": now, "home": bool(self.cookies and shutil.which("yt-dlp"))}

    def hush(self, names=None):
        """Pause every video and stream that plays (or the players named), and close the
        Picture-in-Picture window: music is starting."""
        if names is None:
            names = [name for name, p, _, _ in video_players() if p.get("PlaybackStatus") == "Playing"]
        for name in names:
            busctl("call", name, MPRIS_PATH, MPRIS_PLAYER, "Pause")
        if names:
            self.close_pip()

    def control(self, req):
        """{"toggle": true} | {"skip": ±seconds} | {"seek": seconds} | {"volume": 0-100} | {"hush": true}"""
        if req.get("hush"):
            self.hush()
        name, now = self.current(fresh=True)
        if not name:
            return
        call = lambda *a: busctl("call", name, MPRIS_PATH, MPRIS_PLAYER, *a)
        if req.get("toggle"):
            call("PlayPause")
        if "skip" in req:
            call("Seek", "x", str(int(float(req["skip"]) * 1e6)))
        if "seek" in req and now["trackid"]:
            call("SetPosition", "ox", now["trackid"], str(int(max(0.0, float(req["seek"])) * 1e6)))
        if "volume" in req:
            busctl("set-property", name, MPRIS_PATH, MPRIS_PLAYER, "Volume", "d",
                   str(max(0, min(100, int(req["volume"]))) / 100))

    def open(self, vid):
        if not isinstance(vid, str) or not self.ID.match(vid):
            return False
        return self.ask(vid, None) or self.browse(f"https://www.youtube.com/watch?v={vid}")

    def ask(self, vid, twitch):
        """Hand the waiting browser a video or a channel; False when no browser waits."""
        with self.asked:
            if self.waiting or time.monotonic() - self.waited_at < 8:    # between two waits counts
                self.seq, self.asked_id, self.asked_twitch = self.seq + 1, vid, twitch
                self.asked.notify_all()
                return True    # the browser puts it where the last video was
        return False

    def listening(self):
        """Does a browser wait for orders (or did, a moment ago)?"""
        with self.asked:
            return bool(self.waiting) or time.monotonic() - self.waited_at < 40

    def browse(self, url):
        name, now = self.current(fresh=True)
        if name and now["playing"]:    # the new video gets its own tab; don't leave two talking
            busctl("call", name, MPRIS_PATH, MPRIS_PLAYER, "Pause")
        return detached("xdg-open", url) or webbrowser.open(url)

    def close_pip(self):
        """Tell the waiting browser to close its Picture-in-Picture window: an order with no video."""
        with self.asked:
            self.seq, self.asked_id, self.asked_twitch = self.seq + 1, None, None
            self.asked.notify_all()

    def wait_open(self, after):
        """Long poll: the last video asked for, as soon as it isn't number `after` (25 s at most).
        "twitch" instead of an id: the channel to open. "close" instead of both: close the
        Picture-in-Picture window."""
        with self.asked:
            if not self.born <= after <= self.seq:
                # a number from before this server started: nothing was asked since, and least of
                # all to close the window of a video that plays
                self.waited_at = time.monotonic()
                return {"seq": self.seq, "id": None, "twitch": None, "close": False}
            self.waiting += 1
            try:
                self.asked.wait_for(lambda: self.seq != after, 25)
            finally:
                self.waiting -= 1
                self.waited_at = time.monotonic()
            return {"seq": self.seq, "id": self.asked_id, "twitch": self.asked_twitch,
                    "close": self.asked_id is None and self.asked_twitch is None}

    def home(self, fresh=False):
        """Your home page's videos, as yt-dlp's ":ytrec" reads them with the browser's login. Kept
        for ten minutes; fresh=True asks again, but not more than once every 15 s."""
        if not self.cookies:
            return {"items": [], "note": "to list your YouTube home page, start serve.py with --youtube-cookies firefox (or your browser)"}
        if not shutil.which("yt-dlp"):
            return {"items": [], "note": "the YouTube home page needs yt-dlp installed"}
        with self.lock:    # one yt-dlp at a time; whoever waited gets its result
            age = time.monotonic() - self.home_at
            if not self.home_at or age > 600 or (fresh and age > 15):
                self.home_items, self.home_note = self._fetch()
                self.home_at = time.monotonic()
            return {"items": self.home_items, "note": self.home_note}

    def _fetch(self):
        try:
            out = subprocess.run(["yt-dlp", "--ignore-config", "--cookies-from-browser", self.cookies, "--flat-playlist",
                                  "--playlist-end", "30", "-J", ":ytrec"],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
            entries = json.loads(out.stdout).get("entries") or []
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            entries, out = [], None
        items = [{"id": e["id"], "title": e.get("title") or "", "by": e.get("channel") or e.get("uploader") or "",
                  "length": e.get("duration") or 0, "live": e.get("live_status") == "is_live",
                  "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"}
                 for e in entries if isinstance(e, dict) and self.ID.match(str(e.get("id", "")))]
        if items:
            return items, ""
        err = [l for l in (out.stderr if out else "").splitlines() if l.startswith("ERROR")]
        return self.home_items, (err[-1][:200] if err else "yt-dlp returned no videos; are you signed in to YouTube in that browser?")


def detached(*cmd):
    """Start a program that must not be this service's child: stopping the service would close it.
    Not waited for: when no browser runs yet, this is the browser."""
    if not shutil.which("systemd-run") or not shutil.which(cmd[0]):
        return False
    try:
        subprocess.Popen(["systemd-run", "--user", "--scope", "--collect", "--quiet", *cmd],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


class Twitch:
    """The remote's Twitch tab: the channels you follow that are live, and Chatterino next to the
    one you watch. Twitch gives that list to a signed-in app only, and Chatterino is one: its login
    is read from its settings each time (it renews it by itself), and goes to Twitch and nowhere
    else. A page names a channel and this builds the address, as for a video."""
    NAME = re.compile(r"^\w{2,25}$")
    FLATPAK = "com.chatterino.chatterino"
    SETTINGS = (".var/app/com.chatterino.chatterino/data/chatterino/Settings/settings.json",
                ".local/share/chatterino/Settings/settings.json")

    def __init__(self, tube, on, chat=True):
        self.tube, self.on, self.chat = tube, on, chat
        self.lock = threading.Lock()
        self.at, self.items, self.note = 0.0, [], ""
        self.opened_at = 0.0
        self.started = time.monotonic()
        if on and chat:
            threading.Thread(target=self._mind, daemon=True).start()

    @staticmethod
    def thumb(name):
        return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{name}-320x180.jpg"

    def account(self):
        """(user id, client id, token) of the account Chatterino is on."""
        for path in self.SETTINGS:
            try:
                acc = json.loads((Path.home() / path).read_text(encoding="utf-8"))["accounts"]
                me = next(a for k, a in acc.items() if k.startswith("uid") and a["username"] == acc["current"])
                return me["userID"], me["clientID"], me["oauthToken"]
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                continue
        return None

    def known(self, name):
        """What the last list said of a channel, without asking again: {} when it wasn't on it."""
        return next((i for i in self.items if i["login"] == name), {})

    def live(self, fresh=False):
        """Kept for a minute; fresh=True asks again, but not more than once every 10 s."""
        if not self.on:
            return {"items": [], "note": "to list the channels you follow, start serve.py with --twitch (it borrows Chatterino's login)"}
        with self.lock:
            age = time.monotonic() - self.at
            if not self.at or age > 60 or (fresh and age > 10):
                self.items, self.note = self._fetch()
                self.at = time.monotonic()
            return {"items": self.items, "note": self.note}

    def _fetch(self):
        acc = self.account()
        if not acc:
            return [], "no Twitch login found: sign in to Chatterino first"
        req = urllib.request.Request(f"https://api.twitch.tv/helix/streams/followed?user_id={acc[0]}&first=100",
                                     headers={"Client-Id": acc[1], "Authorization": "Bearer " + acc[2]})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)["data"]
        except urllib.error.HTTPError as e:
            return self.items, ("Twitch refused Chatterino's login: sign in to Chatterino again"
                                if e.code == 401 else f"Twitch answered {e.code}")
        except (OSError, ValueError, KeyError) as e:
            return self.items, f"couldn't reach Twitch: {e}"[:200]
        return [{"login": s["user_login"], "name": s["user_name"], "game": s.get("game_name") or "",
                 "title": " ".join((s.get("title") or "").split()), "viewers": s.get("viewer_count") or 0,
                 "thumb": self.thumb(s["user_login"])}
                for s in data if self.NAME.match(s.get("user_login") or "")], ""

    def open(self, name):
        if not isinstance(name, str) or not self.NAME.match(name):
            return False
        name = name.lower()
        if self.tube.ask(None, name):
            # only with a browser that will say when the stream is closed: Chatterino is closed then
            self.opened_at = time.monotonic()
            threading.Thread(target=self.open_chat, args=(name,), daemon=True).start()
            return True
        return self.tube.browse(f"https://www.twitch.tv/{name}")

    # Chatterino. "Ours" is one that was started with --channels, which is how this starts it and
    # not how you do: so it is found again after a restart of this server, and yours is never closed.
    def chatterinos(self):
        """[(pid, the channel it was started on, or None for one you started)]"""
        out = []
        for d in Path("/proc").glob("[0-9]*"):
            try:
                if (d / "comm").read_text().strip() != "chatterino":
                    continue
                args = (d / "cmdline").read_bytes().decode(errors="replace").split("\0")
            except OSError:
                continue
            on = next((a[2:] for flag, a in zip(args, args[1:]) if flag == "--channels" and a.startswith("t:")), None)
            out.append((int(d.name), on))
        return out

    def open_chat(self, name):
        if not self.chat:
            return
        with self.lock:
            running = self.chatterinos()
            if any(on is None for _, on in running) or any(on == name for _, on in running):
                return    # yours is open, and you know where the chat is; or ours is there already
            self._close(running)
            if shutil.which("chatterino"):
                detached("chatterino", "--channels", "t:" + name)
            elif shutil.which("flatpak"):
                detached("flatpak", "run", self.FLATPAK, "--channels", "t:" + name)

    def close_chat(self, name=None):
        """The stream of this channel was closed (None: whichever it was)."""
        with self.lock:
            self._close([(pid, on) for pid, on in self.chatterinos() if name is None or on == name])

    def _close(self, running):
        """Ask ours to quit, and wait for them to: two of them must not overlap. Chatterino takes the
        request but doesn't always act on it, so after two seconds it isn't asked any more."""
        ours = {pid for pid, on in running if on is not None}
        for sig, patience in ((signal.SIGTERM, 2), (signal.SIGKILL, 1)):
            for pid in ours:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            until = time.monotonic() + patience
            while ours and time.monotonic() < until:
                time.sleep(0.1)
                ours &= {pid for pid, _ in self.chatterinos()}
            if not ours:
                return

    def _mind(self):
        """The browser says when a stream is closed; a browser that quit says nothing. So when
        nobody has waited for orders for a while, close the Chatterino that was opened for it."""
        while True:
            time.sleep(15)
            quiet = time.monotonic() - max(self.started, self.opened_at) > 60
            if quiet and not self.tube.listening() and any(on for _, on in self.chatterinos()):
                self.close_chat()


class Mpv:
    """The one mpv behind the Radio and Podcasts tabs: they share it, so one of them plays at a time
    and both have the same volume."""

    def __init__(self):
        self.sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                                 f"obs-pear-remote-mpv-{os.getpid()}.sock")
        self.lock = threading.RLock()
        self.proc = None
        self.what = None    # ("radio", station id) | ("podcast", show id, episode id)
        self.volume = 100
        self.muted = False
        self.before_stop = []    # called while the outgoing mpv can still be asked where it is
        self.held = None    # the station /now stopped for a pause, until it or anything else starts

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

    def playing(self, kind):
        """What is on, if it is of this kind and mpv is still running."""
        with self.lock:
            if self.proc and self.proc.poll() is not None:    # the stream ended or mpv died
                self.proc = self.what = None
            return self.what if self.what and self.what[0] == kind else None

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                for hook in self.before_stop:
                    hook()
                self.proc.terminate()
                try:
                    self.proc.wait(2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = self.what = None

    def start(self, url, what, name, *args):
        if not shutil.which("mpv"):
            return False
        with self.lock:
            self.stop()
            self.held = None
            self.proc = subprocess.Popen(
                ["mpv", "--no-config", "--no-video", "--no-terminal", f"--audio-client-name={name}",
                 f"--volume={self.volume}", f"--mute={'yes' if self.muted else 'no'}",
                 f"--input-ipc-server={self.sock}", *args, "--", url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.what = what
        return True

    def sounding(self):
        """Is a station or an episode on, and not paused?"""
        with self.lock:
            on = bool(self.what and self.proc and self.proc.poll() is None)
        return on and not self.ipc("get_property", "pause")

    def hush(self):
        """Make way for something else: an episode pauses where it is, a live station stops."""
        with self.lock:
            if self.what and self.what[0] == "podcast":
                self.ipc("set_property", "pause", True)
            else:
                self.stop()

    def set(self, volume=None, muted=None):
        if volume is not None:
            self.volume = max(0, min(100, int(volume)))
            self.ipc("set_property", "volume", self.volume)
        if muted is not None:
            self.muted = bool(muted)
            self.ipc("set_property", "mute", self.muted)


class Radio:
    """Stations come from radios.json and are named by id, so a page can never hand mpv a URL of
    its own."""

    def __init__(self, mpv, path):
        self.mpv = mpv
        self.path = Path(path)

    def stations(self):
        try:    # read every time: the file is small, and editing it needs no restart
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def stop(self):
        self.mpv.held = None
        if self.mpv.playing("radio"):
            self.mpv.stop()

    def play(self, station_id):
        url = next((s["url"] for s in self.stations() if s["id"] == station_id), None)
        return bool(url) and self.mpv.start(url, ("radio", station_id), "Pear Remote Radio")

    def set(self, volume=None, muted=None):
        self.mpv.set(volume, muted)

    def state(self):
        playing = (self.mpv.playing("radio") or (None, None))[1]
        return {"available": bool(shutil.which("mpv")), "playing": playing,
                "title": (self.mpv.ipc("get_property", "metadata/by-key/icy-title") or "") if playing else "",
                "volume": self.mpv.volume, "muted": self.mpv.muted,
                "stations": [{k: s.get(k, "") for k in ("id", "group", "name", "note")} for s in self.stations()]}


def seconds(text):
    """itunes:duration, which is "3725", "62:05" or "1:02:05"."""
    try:
        n = 0
        for part in (text or "").strip().split(":"):
            n = n * 60 + float(part)
        return int(n)
    except ValueError:
        return 0


class Podcasts:
    """Shows come from podcasts.json (id, group, name, note, feed, image); their episodes are read
    from the feed, kept for a quarter of an hour, and named by a hash of their guid, so here too a
    page asks for an episode and never hands mpv a URL. Where you stopped in each episode is kept
    in a small file, so an episode resumes, across restarts as well."""
    ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

    def __init__(self, mpv, path, memory):
        self.mpv = mpv
        self.path = Path(path)
        self.memory = Path(memory)
        self.lock = threading.Lock()
        self.feeds = {}    # show id -> (when, episodes, note)
        self.now = None    # the episode in mpv, with what the page shows of it
        try:
            self.heard = json.loads(self.memory.read_text(encoding="utf-8"))    # episode id -> [position, length, done]
        except (OSError, ValueError):
            self.heard = {}
        mpv.before_stop.append(self.remember)
        threading.Thread(target=self._keep, daemon=True).start()

    def shows(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def episodes(self, show_id):
        show = next((s for s in self.shows() if s["id"] == show_id), None)
        if not show:
            return None
        with self.lock:
            at, eps, note = self.feeds.get(show_id, (0, [], ""))
            if not at or time.monotonic() - at > 900:
                try:
                    eps, note = self._read(show["feed"]), ""
                except (OSError, ValueError, ET.ParseError) as e:
                    note = f"couldn't read the feed: {e}"[:200]    # and keep the episodes we had
                self.feeds[show_id] = (time.monotonic(), eps, note)
        return {"note": note, "episodes": [dict({k: e[k] for k in ("id", "title", "date", "length")},
                                                at=self.heard.get(e["id"], [0, 0, False])[0],
                                                done=self.heard.get(e["id"], [0, 0, False])[2]) for e in eps]}

    def _read(self, feed):
        if not str(feed).startswith(("https://", "http://")):
            raise ValueError("not a web address")
        req = urllib.request.Request(feed, headers={"User-Agent": "obs-pear-remote (podcast tab)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read(40 << 20))    # the big ones are 10 MB of show notes
        out = []
        for item in root.iter("item"):
            enc = item.find("enclosure")
            url = enc.get("url", "") if enc is not None else ""
            if not url.startswith(("https://", "http://")):
                continue
            try:
                date = int(parsedate_to_datetime(item.findtext("pubDate") or "").timestamp())
            except (TypeError, ValueError):
                date = 0
            guid = (item.findtext("guid") or url).strip()
            out.append({"id": hashlib.sha1(guid.encode()).hexdigest()[:12], "url": url, "date": date,
                        "title": " ".join((item.findtext("title") or "").split()),
                        "length": seconds(item.findtext(self.ITUNES + "duration"))})
            if len(out) == 40:    # feeds list the newest first
                break
        return out

    def play(self, show_id, episode_id):
        show = next((s for s in self.shows() if s["id"] == show_id), None)
        got = self.episodes(show_id)
        ep = got and next((e for e in self.feeds[show_id][1] if e["id"] == episode_id), None)
        if not ep:
            return False
        at, _, done = self.heard.get(episode_id, [0, 0, False])
        ok = self.mpv.start(ep["url"], ("podcast", show_id, episode_id), "Pear Remote Podcast",
                            *([f"--start={int(at)}"] if at > 10 and not done else []))
        if ok:
            self.now = {"show": show_id, "id": episode_id, "title": ep["title"], "name": show["name"],
                        "image": show.get("image", ""), "length": ep["length"]}
        return ok

    def stop(self):
        if self.mpv.playing("podcast"):
            self.mpv.stop()

    def where(self):
        """(position, length, paused) from mpv; None while it has nothing loaded yet."""
        pos = self.mpv.ipc("get_property", "time-pos")
        if pos is None:
            return None
        return pos, self.mpv.ipc("get_property", "duration") or 0, bool(self.mpv.ipc("get_property", "pause"))

    def remember(self):
        what, w = self.mpv.playing("podcast"), None
        if what:
            w = self.where()
        if w and w[0] > 0:
            done = bool(w[1]) and w[0] > w[1] - 30    # the last half minute is the outro
            self.heard[what[2]] = [0 if done else round(w[0]), round(w[1]), done or self.heard.get(what[2], [0, 0, False])[2]]

    def _keep(self):
        """Write down where the episode is every few seconds, so a crash or a power cut costs little."""
        saved = None
        while True:
            time.sleep(5)
            try:
                self.remember()
                now = json.dumps(self.heard)
                if now != saved:
                    self.memory.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.memory.with_suffix(".tmp")
                    tmp.write_text(now, encoding="utf-8")
                    tmp.replace(self.memory)
                    saved = now
            except OSError:
                pass

    def control(self, req):
        """{"toggle": true} | {"skip": ±seconds} | {"seek": seconds}, for the episode that is on."""
        if not self.mpv.playing("podcast"):
            return
        if req.get("toggle"):
            self.mpv.ipc("cycle", "pause")
        if "skip" in req:
            self.mpv.ipc("seek", float(req["skip"]), "relative")
        if "seek" in req:
            self.mpv.ipc("seek", max(0.0, float(req["seek"])), "absolute")

    def state(self):
        what = self.mpv.playing("podcast")
        now = {}
        if what and self.now and self.now["id"] == what[2]:
            w = self.where() or (self.heard.get(what[2], [0])[0], 0, False)
            now = dict(self.now, position=w[0], length=w[1] or self.now["length"], paused=w[2])
        return {"available": bool(shutil.which("mpv")), "now": now,
                "volume": self.mpv.volume, "muted": self.mpv.muted,
                "shows": [{k: s.get(k, "") for k in ("id", "group", "name", "note", "image")} for s in self.shows()]}


class Turns:
    """One thing plays at a time. Music is Pear (over MPRIS) or this server's mpv; the video is a
    YouTube one or a Twitch stream in the browser. Whichever of the two starts while the other plays, the other is
    paused. Only a start counts: what already played together when this began is left alone."""
    PEAR = re.compile(r"youtube-music|pear", re.I)    # Pear's MPRIS Identity is still com.github.th-ch.youtube-music

    def __init__(self, mpv, tube, act=True):
        self.mpv, self.tube, self.act = mpv, tube, act
        self.news = threading.Condition()    # for the pages that wait for the browser's video to change
        self.rev, self.said, self.heard = int(time.time() * 1000), None, False
        self.wake = threading.Event()
        self.was = None    # (music played, a video played) at the last look
        self.kind = {}     # bus name -> is it Pear
        self.monitor = None

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()

    def poke(self):
        """Look now: the radio or a podcast just changed, which MPRIS does not tell."""
        self.wake.set()

    def stop(self):
        if self.monitor and self.monitor.poll() is None:
            self.monitor.terminate()

    def _listen(self):
        """Wake the other thread whenever an MPRIS player says something changed."""
        while True:
            try:
                self.monitor = subprocess.Popen(
                    ["busctl", "--user", "--json=short", "monitor", "--match",
                     f"type='signal',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',path='{MPRIS_PATH}'",
                     "--match", f"type='signal',interface='{MPRIS_PLAYER}',member='Seeked',path='{MPRIS_PATH}'"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                for _ in self.monitor.stdout:
                    self.heard = True
                    self.wake.set()
            except OSError:
                pass
            time.sleep(5)    # the bus went away, or busctl can't monitor here; _run looks by itself meanwhile

    def _run(self):
        while True:
            # an mpv that ends by itself, and a bus that can't be listened to, are caught by looking anyway
            self.wake.wait(3 if self.was and self.was[0] or not self.monitor or self.monitor.poll() is not None else 30)
            time.sleep(0.15)    # players announce a change in several signals
            self.wake.clear()
            heard, self.heard = self.heard, False
            try:
                if self.act:
                    self.look()
                self.tell(heard)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                pass

    def tell(self, heard):
        """Let the waiting pages go when the video is another one, started, paused or was moved."""
        now = {k: v for k, v in self.tube.current(fresh=True)[1].items() if k != "position"}
        with self.news:
            if heard or now != self.said:
                self.rev, self.said = self.rev + 1, now
                self.news.notify_all()

    def wait(self, after, secs=10):
        """Long poll: returns the number of the last change, as soon as it isn't `after`."""
        with self.news:
            self.news.wait_for(lambda: str(self.rev) != after, secs)
            return self.rev

    def is_pear(self, name):
        if name not in self.kind:
            got = busctl("get-property", name, MPRIS_PATH, "org.mpris.MediaPlayer2", "Identity")
            if len(self.kind) > 50:
                self.kind.clear()
            self.kind[name] = bool(got and self.PEAR.search(str(got.get("data"))))
        return self.kind[name]

    def look(self):
        pear, videos = [], []
        for name, p, meta in players():
            if p.get("PlaybackStatus") != "Playing":
                continue
            if WATCH.match(meta.get("xesam:url", "")) or channel(meta.get("xesam:url", "")):
                videos.append(name)
            elif self.is_pear(name):
                pear.append(name)
        mpv = self.mpv.sounding()
        now = (bool(pear or mpv), bool(videos))
        was, self.was = self.was, now    # what was seen, not what is hoped for: a player slow to say it paused must not look like a start
        if was is None or not all(now):
            return
        if not was[0]:    # the music started
            self.tube.hush(videos)
        elif not was[1]:    # the video started
            for name in pear:
                busctl("call", name, MPRIS_PATH, MPRIS_PLAYER, "Pause")
            if mpv:
                self.mpv.hush()    # which announces nothing, so note it here, or mpv's next start would not be one
                self.was = (bool(pear or self.mpv.sounding()), True)


MPV = RADIO = TUBE = PODS = TURNS = TWITCH = None    # set in main


def now():
    """What the mpv is on, for /now.json. A station that /now paused still counts: a live stream
    can't wait, so it was stopped, and is started again by the next toggle."""
    radio = RADIO.state()
    st = next((s for s in radio["stations"] if s["id"] == (radio["playing"] or MPV.held)), None)
    if st:
        return {"kind": "radio", "id": st["id"], "title": radio["title"] or st["name"],
                "author": st["name"] if radio["title"] else "", "cover": "", "paused": not radio["playing"]}
    ep = PODS.state()["now"]
    if ep:
        return {"kind": "podcast", "id": ep["id"], "title": ep["title"], "author": ep["name"],
                "cover": ep["image"], "paused": ep["paused"]}
    return {}


def now_toggle():
    on = MPV.playing("radio")
    if on:
        RADIO.stop()
        MPV.held = on[1]
    elif MPV.held:
        RADIO.play(MPV.held)
    else:
        PODS.control({"toggle": True})


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
        if path not in ("/mpris.json", "/radio.json", "/youtube.json", "/youtube-home.json", "/youtube-open.json",
                        "/podcast.json", "/podcast-episodes.json", "/now.json", "/twitch.json"):
            return super().do_GET()
        local = self.local()
        if local is None:
            return
        if path == "/radio.json":
            return self.send_json(json.dumps(RADIO.state()).encode(), local)
        if path == "/now.json":
            return self.send_json(json.dumps(now()).encode(), local)
        if path == "/youtube.json":
            # ?after=<rev>: answer when the video changes (10 s at most), so a page needn't keep asking
            after = parse_qs(urlparse(self.path).query).get("after")
            if not TURNS:
                return self.send_json(json.dumps(TUBE.state()).encode(), local)
            rev = TURNS.wait(after[0] if after else None, 10) if after else TURNS.rev
            return self.send_json(json.dumps(dict(TUBE.state(fresh=bool(after)), rev=str(rev))).encode(), local)
        if path == "/podcast.json":
            return self.send_json(json.dumps(PODS.state()).encode(), local)
        if path == "/podcast-episodes.json":
            got = PODS.episodes(parse_qs(urlparse(self.path).query).get("show", [""])[0])
            return self.send_json(json.dumps(got).encode(), local) if got else self.send_error(404)
        if path == "/youtube-open.json":
            # a header no web page can add to a request to another origin: a site must not be able to
            # sit here and have the remote's videos go nowhere
            if self.headers.get("X-Pear-Remote") != "firefox":
                return self.send_error(403)
            try:
                after = int(parse_qs(urlparse(self.path).query).get("after", ["-1"])[0])
            except ValueError:
                return self.send_error(400)
            return self.send_json(json.dumps(TUBE.wait_open(after)).encode(), local)
        if path == "/twitch.json":
            return self.send_json(json.dumps(TWITCH.live("fresh" in parse_qs(urlparse(self.path).query))).encode(), local)
        if path == "/youtube-home.json":
            return self.send_json(json.dumps(TUBE.home("fresh" in parse_qs(urlparse(self.path).query))).encode(), local)
        global _cache
        if time.monotonic() - _cache[0] > 0.8:    # the card polls every second
            _cache = (time.monotonic(), json.dumps(youtube_now()).encode())
        self.send_json(_cache[1], local)

    def do_POST(self):
        try:
            self.post()
        finally:
            if TURNS:
                TURNS.poke()    # whatever this started, it is heard of at once

    def post(self):
        """/radio, body {"play": id} | {"stop": true}, and/or {"volume": 0-100}, {"muted": bool}.
        /youtube, body {"open": video id}, or what Tube.control takes.
        /podcast, body {"play": [show id, episode id]} | {"stop": true}, what Podcasts.control takes,
        and/or volume and muted as for the radio.
        /now, body {"toggle": true}.
        /twitch, body {"open": channel}; and from extras/firefox-pip, {"closed": channel}: its stream
        was closed (true: whatever was open is gone).
        Only for the pages this server serves: a JSON body can't be sent cross-origin without a
        preflight, which nothing here answers, and an Origin that isn't ours is refused outright."""
        path = urlparse(self.path).path
        if path not in ("/radio", "/youtube", "/podcast", "/now", "/twitch"):
            return self.send_error(404)
        local = self.local()
        if local is None:
            return
        origin = self.headers.get("Origin")
        browser = self.headers.get("X-Pear-Remote") == "firefox"    # as for /youtube-open.json: no web page can send it
        if (origin is not None and origin not in {"http://" + h for h in local} and not browser) \
                or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.send_error(403)
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)))
            if path == "/youtube":
                if "open" in req and not TUBE.open(req["open"]):
                    return self.send_error(404, "not a video id, or no browser to open it in")
                TUBE.control(req)
                return self.send_json(json.dumps(TUBE.state(fresh=True)).encode(), local)
            if path == "/twitch":
                if "closed" in req:
                    if not browser:
                        return self.send_error(403)
                    TWITCH.close_chat(None if req["closed"] is True else str(req["closed"]).lower())
                elif "open" in req and not TWITCH.open(req["open"]):
                    return self.send_error(404, "not a channel, or no browser to open it in")
                return self.send_json(json.dumps(TUBE.state(fresh=True)).encode(), local)
            if path == "/now":
                if req.get("toggle"):
                    now_toggle()
                return self.send_json(json.dumps(now()).encode(), local)
            if path == "/podcast":
                MPV.set(req.get("volume"), req.get("muted"))
                if req.get("stop"):
                    PODS.stop()
                elif "play" in req and not PODS.play(*map(str, req["play"][:2])):
                    return self.send_error(404, "unknown episode, or mpv is not installed")
                PODS.control(req)
                return self.send_json(json.dumps(PODS.state()).encode(), local)
            RADIO.set(req.get("volume"), req.get("muted"))
            if req.get("stop"):
                RADIO.stop()
            elif "play" in req and not RADIO.play(req["play"]):
                return self.send_error(404, "unknown station, or mpv is not installed")
        except (ValueError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
            return self.send_error(400)
        self.send_json(json.dumps(RADIO.state()).encode(), local)

    def log_message(self, fmt, *args):
        if not getattr(self, "path", "").startswith(("/mpris.json", "/radio", "/youtube", "/podcast", "/now.json", "/twitch")):    # unset on a malformed request
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
    ap.add_argument("--podcasts", default=Path(__file__).resolve().parent.parent / "podcasts.json",
                    help="the shows for the remote's Podcasts tab (default: podcasts.json next to the pages)")
    ap.add_argument("--youtube-cookies", metavar="BROWSER",
                    help="list your YouTube home page in the remote's YouTube tab, signed in as this browser is: "
                         "firefox, chrome, brave… (yt-dlp's --cookies-from-browser; needs yt-dlp)")
    ap.add_argument("--twitch", action="store_true",
                    help="list the live channels you follow in the remote's Twitch tab, signed in as Chatterino is")
    ap.add_argument("--no-twitch-chat", action="store_true",
                    help="don't open Chatterino on the channel when a stream is opened from the Twitch tab")
    ap.add_argument("--let-both-play", action="store_true",
                    help="don't pause the browser's video or stream when music starts, nor the music when the video starts")
    a = ap.parse_args()
    ALLOWED.update(o.rstrip("/") for o in a.allow_origin)
    MPV = Mpv()
    RADIO = Radio(MPV, a.radios)
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "obs-pear-remote/podcasts-heard.json"
    PODS = Podcasts(MPV, a.podcasts, state)
    TUBE = Tube(a.youtube_cookies)
    TWITCH = Twitch(TUBE, a.twitch, chat=not a.no_twitch_chat)
    if shutil.which("busctl"):
        TURNS = Turns(MPV, TUBE, act=not a.let_both_play)
        TURNS.start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))    # so the finally below runs
    handler = functools.partial(Handler, directory=a.directory)
    with ThreadingHTTPServer((a.bind, a.port), handler) as httpd:
        print(f"serving {a.directory} on http://{a.bind}:{a.port}/ (+ /mpris.json, /radio.json, /youtube.json, /podcast.json, /now.json, /twitch.json)")
        try:
            httpd.serve_forever()
        finally:
            MPV.stop()    # the radio and the podcast don't outlive their only remote control
            if TURNS:
                TURNS.stop()
