# obs-pear-remote

An OBS dock that remote-controls [Pear Desktop](https://github.com/pear-devs/pear-desktop)
(the YouTube Music desktop app), plus a "now playing" card for your scene. Both are plain HTML
files with no build step and nothing to install besides Python's built-in web server.

**Pear Remote** (`pear-remote.html`, a custom browser dock) gives you play/pause, previous/next,
seek, volume and mute, like/dislike, shuffle and repeat, the live queue (click a song to jump to it,
✕ to remove it), and a search box whose results you can play now, play next or add to the end of
the queue. When the dock is wider than it is tall, it switches to a side-by-side layout.

<p>
  <img src="screenshots/remote-narrow.png" width="300" alt="Pear Remote docked narrow, Ferra theme">
  <img src="screenshots/remote-wide-midnight.png" width="520" alt="Pear Remote docked wide, midnight theme">
</p>

**Now Playing** (`now-playing.html`, a browser source) shows cover, title and artist, scrolls titles
that are too long, dims while paused and hides itself when nothing is playing. On Linux, if you
serve the files with `extras/serve.py` instead of `http.server`, the card shows the YouTube video
playing in your browser ("Now watching", read from MPRIS) whenever Pear is idle or paused.

<img src="screenshots/now-playing.png" width="560" alt="Now Playing card">

## Requirements

- Pear Desktop (tested with 3.12.0)
- OBS Studio 30 or newer (tested on Linux with 32.2.2 and on Windows 11 with 32.2.1); custom browser docks need the browser plugin, which the official builds include
- Python 3, only to serve the files (on Windows: install it from python.org, which adds the `py` launcher)

## 1. Set up Pear

In Pear, open **Plugins** and enable:

- **API Server**: the remote needs it. Keep the defaults: port `26538` and authorization
  *"Authorize at first request"*.
- **Amuse**: the card needs it. The remote uses it as a fallback for the current song until Pear
  sends its first player event.

## 2. Serve the files

Get the files with `git clone https://github.com/<you>/obs-pear-remote`, or *Code › Download ZIP*
on GitHub and unzip it.

**Linux / macOS**

```sh
python3 -m http.server 9870 --bind 127.0.0.1 --directory obs-pear-remote
```

**Windows**: double-click `extras\start-server.cmd`, or run

```bat
py -m http.server 9870 --bind 127.0.0.1 --directory C:\path\to\obs-pear-remote
```

Leave the window open while you stream (closing it stops the server).

Keep the `--bind 127.0.0.1`. The API server can control your playback, so the page that holds its
token should only be reachable from your own machine. Serving over `http://127.0.0.1` rather than
opening the files as `file://` also avoids the cross-origin and local-network restrictions of OBS's
browser.

To start the server at login: on Linux, use `extras/obs-pear-remote.service` (a systemd user unit;
the comment at its top explains how). On Windows, put a shortcut to `extras\start-server.cmd` in
the Startup folder (Win+R, `shell:startup`) and set the shortcut to *Run: Minimized*.

## 3. Add them to OBS

- **Remote**: *Docks › Custom Browser Docks…*, name `Pear`, URL
  `http://127.0.0.1:9870/pear-remote.html`. The first time the dock connects, Pear asks whether to
  allow `obs-pear-remote`. Click **Allow**; Pear remembers the answer.
- **Card**: add a *Browser* source, URL `http://127.0.0.1:9870/now-playing.html`, width 800,
  height 300. The background is already transparent.

The remote reloads itself when you edit `pear-remote.html` (docks have no reload button). Turn that
off with `?reload=0`.

## Options

Add options to the URL, for example `pear-remote.html?theme=midnight&amuse=0`.

**pear-remote.html**

| option   | default                        | what it does |
|----------|--------------------------------|--------------|
| `api`    | `http://127.0.0.1:26538`       | Pear API Server address |
| `client` | `obs-pear-remote`              | client name shown in Pear's authorize prompt |
| `amuse`  | `http://127.0.0.1:9863/query`  | Amuse fallback; `0` turns it off |
| `theme`  | `ferra`                        | `ferra` or `midnight` |
| `reload` | `1`                            | `0` stops the reload-on-change check |

**now-playing.html**

| option   | default                                   | what it does |
|----------|-------------------------------------------|--------------|
| `amuse`  | `127.0.0.1:9863`, then `localhost:9863`  | Amuse endpoint to poll |
| `video`  | `/mpris.json`                             | browser YouTube fallback from `extras/serve.py`; `0` turns it off |
| `theme`  | `purple`                                  | `purple` or `mono` |

### Themes

All colours are CSS variables in `:root[data-theme="…"]` blocks at the top of each file. To make
your own theme, copy a block, give it a new name, change the colours and select it with `?theme=`.

For the card, you can also leave the file alone and override the variables in the browser source's
**Custom CSS** field, e.g. `:root { --accent: #7fdbca; --panel: rgba(0,0,0,.6); }`. Docks don't have
that field, so for the remote use a theme block.

## Troubleshooting

- **"Pear API server not reachable"**: Pear isn't running, *Plugins › API Server* is off, or it
  listens on a different port (use `?api=`).
- **"Pear denied obs-pear-remote"**: someone clicked Deny in the authorize prompt. Pear doesn't
  remember a denial: reload the dock to be asked again. Docks have no reload button, but
  saving `pear-remote.html` again in any editor triggers the reload-on-change check (or restart OBS).
- **The card never appears**: *Plugins › Amuse* is off, or nothing is playing. Open
  `http://127.0.0.1:9863/query` in a browser: it should return JSON.
- **The queue is empty or the song is missing right after Pear starts**: Pear only reports the player
  state after its first player event. Press play/pause once.
- **"+ queue" does nothing**: on an endless radio/autoplay queue, Pear 3.12.0 accepts "add to the
  end" but the song never shows up (the same happens when you call Pear's API directly). Use
  "▶ next" instead, which inserts right after the current song.
- **The repeat and mute buttons don't light up**: the commands work, but Pear 3.12.0 doesn't
  report the new repeat, mute or volume state over its websocket, so the dock never learns it.
- **Search finds nothing**: results are filtered on YouTube Music's "Song" / "Video" labels, so
  search only works when YouTube Music is set to English.
- **The card ignores your edits to `now-playing.html`**: OBS caches the page (Python's server sends no
  cache headers). Open the browser source's properties and click *Refresh cache of current page*.
- **Windows: the server window says "Python 3 was not found, or it does not start"**: `py` and
  `python` can point at a Python that was uninstalled. Install Python from python.org again.
- **Windows asks to let YouTube Music through the firewall**: that prompt comes from Pear (its Amuse
  plugin listens on every network interface). *Cancel* is fine: the widgets only talk to
  `127.0.0.1`, which the firewall doesn't block.
- **Nothing loads**: check that the server is running (`http://127.0.0.1:9870/pear-remote.html`
  opens in a normal browser too) and that the dock URL starts with `http://`, not `file://`.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with YouTube, Google or the Pear Desktop project.
