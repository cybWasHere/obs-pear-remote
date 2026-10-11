// Pear Remote, YouTube and Twitch tabs: play the clicked video or stream in Firefox's
// Picture-in-Picture window, in the place of whatever that window was showing, instead of opening
// a new tab each time.
//
// This is Firefox autoconfig code: it runs privileged, in the parent process, at startup. Append it
// to your mozilla.cfg (see the README, "YouTube"); edits need a full Firefox restart.
//
// How: it waits on extras/serve.py (GET /youtube-open.json, a long poll) for the id of the video the
// remote asked for. Then it takes the tab the Picture-in-Picture window came from, or else a tab
// that is already on a YouTube video, or else a new background tab, and moves it to that video the
// way a click on YouTube does. YouTube keeps its <video> element across such a move, so an open
// Picture-in-Picture window just carries on with the new video; when there is none yet, one is
// opened as Ctrl+Shift+] would, the moment the tab has a video to show. While nothing waits on that endpoint, serve.py opens a tab as before.
//
// A Twitch channel comes down the same wait and is played the same way, in a tab that is on Twitch.
// What is new there: serve.py opens Chatterino next to the stream, so it is told (POST /twitch)
// when the stream is closed: its tab closed or gone elsewhere, or its Picture-in-Picture window
// closed with the stream paused, which is what the window's ✕ does.
//
// The same wait also brings the order to close the window: serve.py sends it when music starts
// while a video or a stream plays, having paused it itself.
{
  const SERVER = "http://127.0.0.1:9870"; // where extras/serve.py listens
  const { setTimeout } = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");
  const system = Services.scriptSecurityManager.getSystemPrincipal();
  // the autoconfig sandbox has no DOM globals; borrow fetch from one that does
  const { fetch } = Cu.Sandbox(system, { wantGlobalProperties: ["fetch"] });
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const pipWindows = () => [...Services.wm.getEnumerator("Toolkit:PictureInPicture")].filter(w => !w.closed);
  const videoOf = browser => {
    try {
      const uri = browser.currentURI;
      if (/(^|\.)youtube\.com$/.test(uri.host) && uri.filePath == "/watch") {
        return /(?:^|&)v=([^&]+)/.exec(uri.query)?.[1] ?? "";
      }
    } catch (e) {} // about:, file: and friends have no host
    return null; // not on a YouTube video
  };
  const NOT_CHANNELS = ["directory", "videos", "settings", "search", "downloads", "subscriptions", "inventory",
                        "drops", "wallet", "friends", "turbo", "jobs", "store", "prime", "login", "signup", "following"];
  // The extension "Alternate Player for Twitch.tv" sends a channel's tab to a page of its own,
  // moz-extension://<id>/player.html?channel=<name>. Its address without the channel, or null:
  const playerOf = browser => {
    try {
      const uri = browser.currentURI;
      if (uri.schemeIs("moz-extension") && uri.filePath == "/player.html" && /(?:^|&)channel=\w/.test(uri.query)) {
        return uri.prePath + uri.filePath;
      }
    } catch (e) {}
    return null;
  };
  const channelOf = browser => {
    try {
      const uri = browser.currentURI;
      if (playerOf(browser)) return /(?:^|&)channel=(\w{2,25})(?:&|$)/.exec(uri.query)?.[1].toLowerCase() ?? null;
      const name = /^\/(\w{2,25})\/?$/.exec(uri.filePath)?.[1].toLowerCase();
      if (/(^|\.)twitch\.tv$/.test(uri.host) && name && !NOT_CHANNELS.includes(name)) return name;
    } catch (e) {}
    return null; // not on a Twitch channel
  };
  // what a tab is on, per site, and the address of a video or a channel there
  const SITES = {
    youtube: { at: videoOf, url: id => "https://www.youtube.com/watch?v=" + id },
    twitch: { at: channelOf, url: name => "https://www.twitch.tv/" + name },
  };

  // The tab behind a Picture-in-Picture window. Firefox keeps that in a module whose address has
  // moved between versions; without it, the tab is found by what is playing instead.
  let pipModule = null;
  for (const url of ["moz-src:///toolkit/components/pictureinpicture/PictureInPicture.sys.mjs",
                     "resource://gre/modules/PictureInPicture.sys.mjs"]) {
    try { pipModule = ChromeUtils.importESModule(url).PictureInPicture; break; } catch (e) {}
  }
  const pipBrowser = win => { try { return pipModule.weakWinToBrowser.get(win) ?? null; } catch (e) { return null; } };

  const allTabs = () => {
    const tabs = [];
    for (const win of Services.wm.getEnumerator("navigator:browser")) {
      if (!win.closed && win.gBrowser) tabs.push(...win.gBrowser.tabs);
    }
    return tabs;
  };

  function pickTab(site) {
    const from = pipWindows().map(pipBrowser).filter(Boolean);
    const there = allTabs().filter(t => site.at(t.linkedBrowser) !== null);
    return there.find(t => from.includes(t.linkedBrowser)) || there.find(t => t.soundPlaying) || there[0] || null;
  }

  // Runs in the tab's process. With `go`, hand the site's own router the video or the channel, as
  // a click on a thumbnail does: the page changes without a load, so the <video> element (and a
  // PiP window on it) survives. Then put that element in Picture-in-Picture as soon as there is
  // one to put, by the same call as the keyboard shortcut.
  const inPage = (kind, id, go, latest, ext, asked) => {
    const { setTimeout } = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");
    const twitch = kind == "twitch";
    // the click was on the remote, not in this page: without it Firefox lets a new tab play muted at best
    try { content.document.notifyUserGestureActivation(); } catch (e) {}
    try {
      const app = go && !twitch && content.document.querySelector("ytd-app");
      if (app) {
        const detail = Cu.cloneInto({ endpoint: {
          commandMetadata: { webCommandMetadata: { url: "/watch?v=" + id, webPageType: "WEB_PAGE_TYPE_WATCH", rootVe: 3832 } },
          watchEndpoint: { videoId: id } } }, content);
        app.dispatchEvent(new content.CustomEvent("yt-navigate", { detail, bubbles: true, composed: true }));
      }
      if (go && twitch) { // Twitch's router follows the address bar
        content.history.pushState(null, "", "/" + id);
        content.dispatchEvent(new content.PopStateEvent("popstate", { state: null }));
      }
    } catch (e) { Cu.reportError(e); }
    const here = () => {
      const at = content.location;
      if (!twitch) return at.search.includes("v=" + id);
      if (at.protocol == "moz-extension:") return new RegExp("[?&]channel=" + id + "(&|$)", "i").test(at.search); // the extension's player
      return at.pathname.replace(/\/$/, "").toLowerCase() == "/" + id;
    };
    // a stream can take long to show: the Twitch extension sits out an ad (half a minute, at
    // times more) with no picture at all
    const until = Date.now() + (twitch ? 90000 : 20000);
    let stuck = 0;
    // YouTube may load a video's page twice (the second time with &themeRefresh=1), and the window
    // goes with the first document: the work is done per document, and watched until the end
    let doc = content.document, shown = null; // shown: the document whose video has had its window
    const pip = () => {
      if (!latest()) return; // run again for this tab: two of these would open two windows
      try {
        if (content.document !== doc) {
          doc = content.document;
          try { doc.notifyUserGestureActivation(); } catch (e) {}
          asked(1); // the window asked for a moment ago was the last document's
        }
        // The extension's player gives up on some channels ("this broadcast is encrypted") and
        // shows its report page. Twitch's own player, then, at the address the extension leaves alone
        if (twitch && content.location.protocol == "moz-extension:" && content.document.querySelector('iframe[src="report.html"]')) {
          return content.location.replace("https://www.twitch.tv/" + id + "?twitch5=0");
        }
        if (here()) {
          const actor = content.windowGlobalChild.getActor("PictureInPictureLauncher");
          const video = actor.findVideoToPiP(content.document);
          // The extension's player, stopped (its page says so: states 7 and 8): by quiet() when
          // something else took over, or by itself when Firefox refused its first try to play.
          // Its button starts it again, live. Not pressed twice within its start
          if (video && video.paused && content.location.protocol == "moz-extension:"
              && ["7", "8"].includes(content.document.body.getAttribute(ext[1])) && Date.now() - stuck > 3000) {
            stuck = Date.now();
            content.document.getElementById(ext[0])?.click();
          }
          // the element is there before its picture is; the window would open with no shape (seen:
          // 360 px wide, 16384 tall, and black)
          if (video && (video.videoWidth && video.videoHeight || video.isCloningElementVisually)) {
            // the window takes a moment to open: asking twice in that moment would open two
            if (!video.isCloningElementVisually) {
              // the window takes a moment to open: asking twice in that moment would open two.
              // And one asked for while a stream changes channel may not come: ask again then.
              // Not for a video that has had its window: that one was closed by hand
              if ((twitch || shown !== doc) && Date.now() - asked() > 3000) {
                asked(Date.now());
                actor.togglePictureInPicture({ video, reason: "Shortcut" });
              }
            } else if (!twitch) {
              if (shown !== doc && video.paused && !go) try { video.play(); } catch (e) {} // the one it was on, paused: go on
              shown = doc;
            } else {
              // a stream that was paused in this tab stays paused, on the next channel too: when
              // Twitch's own button has said "paused" for a while, press it
              if (!video.paused) return;
              if (content.location.protocol == "moz-extension:") { // see above; nothing of Twitch's to press here
                if (Date.now() < until) setTimeout(pip, 100);
                return;
              }
              const button = content.document.querySelector('[data-a-target="player-play-pause-button"]');
              if (button && button.getAttribute("data-a-player-state") != "paused") stuck = 0;
              else if (!stuck) stuck = Date.now();
              else if (Date.now() - stuck > 1500) return button ? button.click() : video.play();
            }
          }
        }
      } catch (e) {} // between two documents
      if (Date.now() < until) setTimeout(pip, 100);
    };
    pip();
  };
  const frame = (browser, code) => browser.messageManager.loadFrameScript(
    "data:application/javascript;charset=utf-8," + encodeURIComponent(code), false, true); // true: in the tab's one global
  let runs = 0; // frame scripts of one tab share their global: the latest run leaves its number there
  const run = (browser, kind, id, go) => frame(browser, `this.pearRemoteRun = ${++runs};
    (${inPage})(${JSON.stringify(kind)}, ${JSON.stringify(id)}, ${go}, () => this.pearRemoteRun === ${runs},
      ${JSON.stringify([EXT_STOP, EXT_STATE])},
      at => at ? this.pearRemoteAsked = at : this.pearRemoteAsked || 0)`); // when a window was last asked for, here
  // Pause what plays in a tab. The Twitch extension's player starts its video again when the
  // element is paused; it stops by its own button (the one Space presses), which is pressed
  // unless its page says it has stopped already (states 7 and 8). After that a pause holds.
  const EXT_STOP = "переключитьтрансляцию", EXT_STATE = "data-состояние"; // the button's id, <body>'s attribute
  const quiet = browser => frame(browser, `{
    const pause = () => { for (const v of content.document.querySelectorAll("video")) v.pause(); };
    const button = content.location.protocol == "moz-extension:" && content.document.getElementById(${JSON.stringify(EXT_STOP)});
    if (button && !["7", "8"].includes(content.document.body.getAttribute(${JSON.stringify(EXT_STATE)}))) {
      button.click();
      ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs").setTimeout(pause, 800);
    }
    pause();
  }`);
  // Close a Picture-in-Picture window as its own ✕ does: the video pauses and goes back to its tab.
  const shut = win => {
    const browser = pipBrowser(win);
    if (browser) quiet(browser);
    win.close();
  };
  // does a video play in this tab?
  const playing = browser => new Promise(resolve => {
    const mm = browser.messageManager, name = "PearRemote:playing:" + Math.random();
    const done = msg => { mm.removeMessageListener(name, done); resolve(!!msg?.data); };
    mm.addMessageListener(name, done);
    frame(browser, `sendAsyncMessage(${JSON.stringify(name)}, [...content.document.querySelectorAll("video")].some(v => !v.paused && !v.ended))`);
    setTimeout(done, 2000);
  });

  const tell = body => fetch(`${SERVER}/twitch`, { method: "POST", body: JSON.stringify(body),
    headers: { "X-Pear-Remote": "firefox", "Content-Type": "application/json" } }).catch(() => {});

  // Say when the stream of a channel is closed, for the Chatterino that serve.py opened with it.
  let watch = 0; // one stream is watched at a time
  async function watchStream(tab, name) {
    const mine = ++watch, browser = tab.linkedBrowser, since = Date.now();
    const inPip = () => pipWindows().some(w => (pipBrowser(w) ?? browser) === browser);
    for (let had = false, seen = false; ; ) {
      await sleep(500);
      if (mine !== watch) return; // on another channel now, which serve.py knows
      if (tab.closing || !tab.isConnected) break;
      if (channelOf(browser) === name) seen = true;
      else if (seen || Date.now() - since > 20000) break; // it left; or never got there
      if (inPip()) had = true;
      else if (had) {
        // the window is gone. Its ✕ pauses the stream: closed. Its "back to tab" lets it play on: not closed
        had = false;
        await sleep(800);
        if (mine !== watch) return;
        if (!tab.closing && tab.isConnected && !inPip() && !(await playing(browser))) break;
      }
    }
    if (mine === watch) tell({ closed: name });
  }

  let job = 0; // a newer click takes over from one still in flight
  async function open(kind, id) {
    const mine = ++job, site = SITES[kind], url = site.url(id);
    const arrived = async tries => {
      for (let i = 0; i < tries && site.at(browser) !== id; i++) await sleep(100);
      return site.at(browser) === id;
    };
    let tab = pickTab(site), browser = tab?.linkedBrowser, fresh = !tab;
    if (fresh) {
      const win = Services.wm.getMostRecentWindow("navigator:browser");
      if (!win) return;
      tab = win.gBrowser.addTab(url, { triggeringPrincipal: system, inBackground: true });
      browser = tab.linkedBrowser;
    }
    for (const w of pipWindows()) if (pipBrowser(w) && pipBrowser(w) !== browser) shut(w); // one window
    for (const t of allTabs()) { // and one voice: the other site's video gives way
      if (t.soundPlaying && t.linkedBrowser !== browser && Object.values(SITES).some(s => s.at(t.linkedBrowser) !== null)) quiet(t.linkedBrowser);
    }
    if (kind == "twitch") watchStream(tab, id);
    // Twitch starts no player in a tab that isn't looked at. Firefox counts a tab as looked at
    // while its video is in Picture-in-Picture; say so a moment early
    const show = () => { if (kind == "twitch" && !tab.selected) try { browser.docShellIsActive = true; } catch (e) {} };
    show();
    let spec = null; // the address the page was at when the work in it was last started
    const start = go => { spec = browser.currentURI.spec; run(browser, kind, id, go); };
    let there = false;
    const player = kind == "twitch" && !fresh && site.at(browser) !== id && playerOf(browser);
    if (player) {
      // the extension's player takes its channel from its address, and from nowhere else. Not
      // given that address directly (every other time, its page then stayed empty): the
      // channel's own, from which the extension brings the tab back to its player
      browser.fixupAndLoadURIString(url, { triggeringPrincipal: system });
    } else if (!fresh) {
      const title = browser.contentTitle, moving = site.at(browser) !== id;
      start(moving);
      there = await arrived(30);
      // Twitch's address moves at once, whether its router followed or not; its title tells
      if (there && moving && kind == "twitch") {
        for (let i = 0; i < 40 && !(there = browser.contentTitle !== title); i++) await sleep(100);
      }
      if (mine !== job) return;
      if (!there) browser.fixupAndLoadURIString(url, { triggeringPrincipal: system }); // the site didn't take it: a plain load
    }
    if (!there) {
      if (!(await arrived(150)) || mine !== job) return;
      show(); // again: a new address can be a new process
      start(false);
    }
    // a tab that was never in front holds its video back: a new one, or one Firefox brought back
    // with the last session. Let it go once Firefox has caught it
    // Meanwhile a channel's tab may be sent on to the extension's player: another address, in
    // another process, where what was started in the page is gone. Start it again there
    let held = true, again = -1;
    for (let i = 0; i < (fresh || kind == "twitch" ? 150 : 50) && mine === job && !tab.closing; i++) {
      if (held && tab.activeMediaBlocked) {
        tab.resumeDelayedMedia();
        held = false;
        if (kind != "twitch") return;
      }
      if (kind == "twitch" && browser.currentURI.spec !== spec && site.at(browser) === id) {
        // a window opened on the page the tab just left, for the other kind of page, is left
        // behind showing nothing: close it, or there would be two
        if (spec && spec.split(":")[0] != browser.currentURI.scheme) {
          for (const w of pipWindows()) if (pipBrowser(w) === browser) w.close();
        }
        show();
        start(false);
        again = i + 8;
      }
      // and once more a moment later: what is sent to a tab while it changes process can be lost
      if (i === again && site.at(browser) === id) start(false);
      await sleep(100);
    }
  }

  // Close the Picture-in-Picture windows that show a YouTube video or a Twitch stream (and those
  // whose tab can't be told). It goes back to its tab, paused as serve.py left it.
  function closePip() {
    for (const w of pipWindows()) {
      const browser = pipBrowser(w);
      if (!browser || Object.values(SITES).some(s => s.at(browser) !== null)) shut(w);
    }
  }

  (async () => {
    let seq = null;
    for (;;) {
      try {
        const r = await fetch(`${SERVER}/youtube-open.json?after=${seq ?? -1}`,
                              { headers: { "X-Pear-Remote": "firefox" }, cache: "no-store" });
        if (!r.ok) throw new Error(r.status);
        const d = await r.json();
        if (seq === null) tell({ closed: true }); // Firefox has just started: no stream is open
        else if (d.seq !== seq) {
          if (d.close) closePip();
          else if (/^\w{2,25}$/.test(d.twitch || "")) open("twitch", d.twitch.toLowerCase()).catch(Cu.reportError);
          else if (/^[\w-]{11}$/.test(d.id || "")) open("youtube", d.id).catch(Cu.reportError);
        }
        seq = d.seq;
      } catch (e) { await sleep(5000); } // the server isn't running; it may come later
    }
  })();
}
