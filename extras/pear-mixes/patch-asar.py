#!/usr/bin/env python3
"""Teach Pear Desktop's API a few things, inside Pear's own logged-in session:

- POST /api/v1/queue with a playlist id (anything that isn't an 11-char video id) in `videoId`
  queues the whole playlist/mix (YouTube Music's get_queue with playlistId). It also fixes Pear's
  own add-to-queue, which adds nothing while a mix is playing.
- POST /api/v1/search with query "browse:<browseId>" returns that browse page, e.g.
  "browse:FEmusic_mixed_for_you" = your personal "Mixed for you" shelf, and with
  "next:<playlistId>:<params>" a playlist's first songs.
- POST /api/v1/queue with "play:<playlistId>:<first videoId>:<params>" starts that playlist now,
  like its play button in YouTube Music (videoId and params may be empty).

Usage: patch-asar.py <app.asar> [--check]. Idempotent. If Pear's code no longer matches, it
changes nothing and exits 2 (Pear keeps working, just without mixes).
"""
import hashlib
import json
import os
import struct
import sys
import tempfile

TARGET = "dist/renderer/youtube-music.iife.js"
# (accepted old strings, replacement). The first old string is Pear 3.12.0's original; later
# ones are earlier versions of this patch, so a patched install upgrades in place.
PATCHES = [
    # Pear sends the current queue's queueContextParams; while a mix plays, YouTube answers that
    # with no queueDatas and Pear silently adds nothing (songs too). Without it, get_queue returns
    # the items, and a playlistId (mixes, albums, playlists) expands in Pear's logged-in session.
    # Video ids are always 11 characters; anything else (RDTMAK… mixes, PL…, OLAK…, "LM" = Liked
    # Music) is a playlist id.
    (["queueContextParams:a.getState().queue.queueContextParams,queueInsertPosition:n,videoIds:[t]}",
      "queueContextParams:a.getState().queue.queueContextParams,queueInsertPosition:n,"
      "...(t.length>11?{playlistId:t}:{videoIds:[t]})}",
      "queueInsertPosition:n,...(t.length>11?{playlistId:t}:{videoIds:[t]})}"],
     "queueInsertPosition:n,...(t.length!==11?{playlistId:t}:{videoIds:[t]})}"),
    # search "browse:<browseId>" = a browse page; "next:<playlistId>:<params>" = a mix's first songs
    # (the remote prefetches them on hover, so a click can start the first song straight away)
    (["let o=await i.networkManager.fetch(`/search`,{query:t,",
      "let o=t.startsWith(`browse:`)?await i.networkManager.fetch(`/browse`,{browseId:t.slice(7),"
      "...(r?{continuation:r}:{})}):await i.networkManager.fetch(`/search`,{query:t,"],
     "let o=t.startsWith(`next:`)?await i.networkManager.fetch(`/next`,{playlistId:t.split(`:`)[1],"
     "params:t.split(`:`)[2]||`wAEB`,isAudioOnly:!0}):"
     "t.startsWith(`browse:`)?await i.networkManager.fetch(`/browse`,{browseId:t.slice(7),"
     "...(r?{continuation:r}:{})}):await i.networkManager.fetch(`/search`,{query:t,"),
    # queue "play:<playlistId>:<videoId>:<params>" = start that playlist now, like clicking its play
    # button in YouTube Music; with the first song's videoId known, audio starts in well under 1 s
    (["window.ipcRenderer.on(`peard:add-to-queue`,(e,t,n)=>{let r=document.querySelector(`#queue`),"
      "i=document.querySelector(`ytmusic-app`);if(!i)return;"],
     "window.ipcRenderer.on(`peard:add-to-queue`,(e,t,n)=>{let r=document.querySelector(`#queue`),"
     "i=document.querySelector(`ytmusic-app`);if(!i)return;"
     "if(t.startsWith(`play:`)){let[,l,v,m]=t.split(`:`);m||=`wAEB`;"
     "i.resolveCommand(v?{watchEndpoint:{videoId:v,playlistId:l,params:m}}"
     ":{watchPlaylistEndpoint:{playlistId:l,params:m}});return}"),
]


def read_asar(path):
    with open(path, "rb") as f:
        data = f.read()
    _, hsize, _, jlen = struct.unpack_from("<4I", data, 0)
    header = json.loads(data[16:16 + jlen])
    return header, data, 8 + hsize


def files(node, prefix=""):
    for name, v in node["files"].items():
        p = f"{prefix}/{name}" if prefix else name
        if "files" in v:
            yield from files(v, p)
        elif "offset" in v:  # skips unpacked files, which live in app.asar.unpacked
            yield p, v


def integrity(blob, block=4 * 1024 * 1024):
    return {"algorithm": "SHA256", "hash": hashlib.sha256(blob).hexdigest(), "blockSize": block,
            "blocks": [hashlib.sha256(blob[i:i + block]).hexdigest()
                       for i in range(0, max(len(blob), 1), block)]}


def contents(path):
    header, data, base = read_asar(path)
    entries = sorted(files(header), key=lambda e: int(e[1]["offset"]))
    blobs = {p: data[base + int(v["offset"]):base + int(v["offset"]) + v["size"]] for p, v in entries}
    return header, entries, blobs


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    check = "--check" in sys.argv
    header, entries, blobs = contents(path)

    src = blobs[TARGET].decode("utf-8")
    todo = []
    for olds, new in PATCHES:
        # an earlier version of this patch can contain `new`, so "done" also needs every old form
        # gone, except an old that `new` itself contains (a patch that inserts after its anchor)
        if new in src and not any(o in src and o not in new for o in olds):
            continue
        # the longest match first: an earlier patch version contains the original as a prefix
        hits = [o for o in sorted(olds, key=len, reverse=True) if src.count(o) == 1]
        if not hits:
            print(f"pear-mixes: Pear's code changed, not patching (no unique match for {olds[0][:40]!r})",
                  file=sys.stderr)
            return 2
        todo.append((hits[0], new))
    if not todo:
        print("pear-mixes: already patched")
        return 0
    if check:
        print("pear-mixes: patchable")
        return 0
    for old, new in todo:
        src = src.replace(old, new)
    blobs[TARGET] = src.encode("utf-8")

    offset = 0
    for p, v in entries:
        v["offset"] = str(offset)
        v["size"] = len(blobs[p])
        if "integrity" in v:
            v["integrity"] = integrity(blobs[p])
        offset += len(blobs[p])

    j = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    pad = (-len(j)) % 4
    hpickle = struct.pack("<II", 4 + len(j) + pad, len(j)) + j + b"\0" * pad
    out = struct.pack("<II", 4, len(hpickle)) + hpickle + b"".join(blobs[p] for p, _ in entries)

    # sanity: everything but the target reads back byte-identical
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".app.asar.")
    with os.fdopen(fd, "wb") as f:
        f.write(out)
    _, _, check_blobs = contents(tmp)
    bad = [p for p in blobs if check_blobs.get(p) != blobs[p]]
    if bad:
        os.unlink(tmp)
        print(f"pear-mixes: rebuilt archive doesn't read back ({bad[:3]}), not patching", file=sys.stderr)
        return 1
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    print("pear-mixes: patched", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
