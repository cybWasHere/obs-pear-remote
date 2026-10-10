// Pear Remote, YouTube tab: play the clicked video in Firefox's Picture-in-Picture window, in the
// place of whatever that window was showing, instead of opening a new tab each time.
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
// The same wait also brings the order to close the window: serve.py sends it when music starts
// while a YouTube video plays, having paused the video itself.
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

  // The tab behind a Picture-in-Picture window. Firefox keeps that in a module whose address has
  // moved between versions; without it, the tab is found by what is playing instead.
  let pipModule = null;
  for (const url of ["moz-src:///toolkit/components/pictureinpicture/PictureInPicture.sys.mjs",
                     "resource://gre/modules/PictureInPicture.sys.mjs"]) {
    try { pipModule = ChromeUtils.importESModule(url).PictureInPicture; break; } catch (e) {}
  }
  const pipBrowser = win => { try { return pipModule.weakWinToBrowser.get(win) ?? null; } catch (e) { return null; } };

  function pickTab() {
    const tabs = [];
    for (const win of Services.wm.getEnumerator("navigator:browser")) {
      if (!win.closed && win.gBrowser) tabs.push(...win.gBrowser.tabs);
    }
    const from = pipWindows().map(pipBrowser).filter(Boolean);
    const youtube = tabs.filter(t => videoOf(t.linkedBrowser) !== null);
    return youtube.find(t => from.includes(t.linkedBrowser)) || youtube.find(t => t.soundPlaying) || youtube[0] || null;
  }

  // Runs in the tab's process. With `go`, hand YouTube's own router the video, as a click on a
  // thumbnail does: the page changes without a load, so the <video> element (and a PiP window on
  // it) survives. Then put that element in Picture-in-Picture as soon as there is one to put, by
  // the same call as the keyboard shortcut.
  const inPage = (id, go) => {
    const { setTimeout } = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");
    try {
      const app = go && content.document.querySelector("ytd-app");
      if (app) {
        const detail = Cu.cloneInto({ endpoint: {
          commandMetadata: { webCommandMetadata: { url: "/watch?v=" + id, webPageType: "WEB_PAGE_TYPE_WATCH", rootVe: 3832 } },
          watchEndpoint: { videoId: id } } }, content);
        app.dispatchEvent(new content.CustomEvent("yt-navigate", { detail, bubbles: true, composed: true }));
      }
    } catch (e) { Cu.reportError(e); }
    const until = Date.now() + 20000;
    const pip = () => {
      try {
        if (content.location.search.includes("v=" + id)) {
          const actor = content.windowGlobalChild.getActor("PictureInPictureLauncher");
          const video = actor.findVideoToPiP(content.document);
          if (video) {
            if (!video.isCloningElementVisually) actor.togglePictureInPicture({ video, reason: "Shortcut" });
            return;
          }
        }
      } catch (e) {} // between two documents
      if (Date.now() < until) setTimeout(pip, 100);
    };
    pip();
  };
  const run = (browser, id, go) => browser.messageManager.loadFrameScript(
    "data:application/javascript;charset=utf-8," + encodeURIComponent(`(${inPage})(${JSON.stringify(id)}, ${go})`), false);

  let job = 0; // a newer click takes over from one still in flight
  async function open(id) {
    const mine = ++job, url = "https://www.youtube.com/watch?v=" + id;
    const arrived = async tries => {
      for (let i = 0; i < tries && videoOf(browser) !== id; i++) await sleep(100);
      return videoOf(browser) === id;
    };
    let tab = pickTab(), browser = tab?.linkedBrowser, fresh = !tab;
    if (fresh) {
      const win = Services.wm.getMostRecentWindow("navigator:browser");
      if (!win) return;
      tab = win.gBrowser.addTab(url, { triggeringPrincipal: system, inBackground: true });
      browser = tab.linkedBrowser;
    }
    for (const w of pipWindows()) if (pipBrowser(w) && pipBrowser(w) !== browser) w.close(); // one window
    if (!fresh) {
      run(browser, id, videoOf(browser) !== id);
      if (await arrived(30) || mine !== job) return;
      browser.fixupAndLoadURIString(url, { triggeringPrincipal: system }); // YouTube didn't take it: a plain load
    }
    if (!(await arrived(150)) || mine !== job) return;
    run(browser, id, false);
    // a tab that was never in front holds its video back; let it go once Firefox has caught it
    for (let i = 0; fresh && i < 150 && mine === job && !tab.closing; i++) {
      if (tab.activeMediaBlocked) return tab.resumeDelayedMedia();
      await sleep(100);
    }
  }

  // Close the Picture-in-Picture windows that show a YouTube video (and those whose tab can't be
  // told). The video goes back to its tab, paused as serve.py left it.
  function closePip() {
    for (const w of pipWindows()) {
      const browser = pipBrowser(w);
      if (!browser || videoOf(browser) !== null) w.close();
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
        if (seq !== null && d.seq !== seq) {
          if (d.close) closePip();
          else if (/^[\w-]{11}$/.test(d.id || "")) open(d.id).catch(Cu.reportError);
        }
        seq = d.seq;
      } catch (e) { await sleep(5000); } // the server isn't running; it may come later
    }
  })();
}
