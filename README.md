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
that are too long, dims while paused and hides itself when nothing is playing.

<img src="screenshots/now-playing.png" width="560" alt="Now Playing card">

## Requirements

- Pear Desktop (tested with 3.12.0)
- OBS Studio 30 or newer (custom browser docks need the browser plugin, included in the official builds)
- Python 3, only to serve the files

## 1. Set up Pear

In Pear, open **Plugins** and enable:

- **API Server**: the remote needs it. Keep the defaults: port `26538` and authorization
  *"Authorize at first request"*.
- **Amuse**: the card needs it. The remote uses it as a fallback for the current song until Pear
  sends its first player event.

## 2. Serve the files

```sh
git clone https://github.com/<you>/obs-pear-remote
python3 -m http.server 9870 --bind 127.0.0.1 --directory obs-pear-remote
```

Keep the `--bind 127.0.0.1`. The API server can control your playback, so the page that holds its
token should only be reachable from your own machine. Serving over `http://127.0.0.1` rather than
opening the files as `file://` also avoids the cross-origin and local-network restrictions of OBS's
browser.

To start the server at login, use `extras/obs-pear-remote.service` (a systemd user unit; the comment
at its top explains how).

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
  `touch pear-remote.html` triggers the reload-on-change check (or restart OBS).
- **The card never appears**: *Plugins › Amuse* is off, or nothing is playing. Open
  `http://127.0.0.1:9863/query` in a browser: it should return JSON.
- **The queue is empty or the song is missing right after Pear starts**: Pear only reports the player
  state after its first player event. Press play/pause once.
- **Nothing loads**: check that the server is running (`http://127.0.0.1:9870/pear-remote.html`
  opens in a normal browser too) and that the dock URL starts with `http://`, not `file://`.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with YouTube, Google or the Pear Desktop project.
