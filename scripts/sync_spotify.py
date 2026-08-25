#!/usr/bin/env python3
"""Regenerate the Music section of media.html from Spotify playlists.

Runs in CI (see .github/workflows/sync-spotify.yml), never in the browser --
the client secret must never reach a visitor. Stdlib only, so CI needs no pip
install. Rewrites only the text between the SPOTIFY:* markers in media.html;
everything else on the page (the whole anime section) is left untouched.
"""
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "media.html")
ART_DIR = os.path.join(ROOT, "assets", "music")
CONFIG = os.path.join(ROOT, "spotify-playlists.json")

API = "https://api.spotify.com/v1"
UA = "anish-bio-playlist-sync"

CARDS_START = "<!-- SPOTIFY:CARDS:START -->"
CARDS_END = "<!-- SPOTIFY:CARDS:END -->"
TABS_START = "<!-- SPOTIFY:SUBTABS:START -->"
TABS_END = "<!-- SPOTIFY:SUBTABS:END -->"


# -- helpers -----------------------------------------------------------
def slug(text, limit=50):
    """Match the filenames already in assets/music: lowercase, dash-joined."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-")


def esc(text):
    """Escape for use inside a double-quoted HTML attribute or as body text."""
    return html.escape(text, quote=False).replace('"', "&quot;")


def get(url, token, tries=4):
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "User-Agent": UA}
    )
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 429 carries a Retry-After; 5xx is worth another go.
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "2")) + 1
            elif e.code >= 500:
                wait = 2 ** attempt
            else:
                raise
            if attempt == tries - 1:
                raise
            print("  %d on %s -- retrying in %ds" % (e.code, url, wait))
            time.sleep(wait)


def token_from_env():
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET are not set")
    basic = base64.b64encode(("%s:%s" % (cid, secret)).encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Authorization": "Basic " + basic, "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def playlist_id(raw):
    """Accept a bare id, a spotify:playlist:... URI, or an open.spotify.com URL."""
    raw = raw.strip()
    if "/playlist/" in raw:
        raw = raw.split("/playlist/")[1]
    if raw.startswith("spotify:playlist:"):
        raw = raw.split(":")[-1]
    return raw.split("?")[0].split("/")[0]


# -- fetch -------------------------------------------------------------
def pick_image(images):
    """Cards render small -- take the smallest cover that is still >= 300px."""
    if not images:
        return None
    usable = [i for i in images if (i.get("width") or 0) >= 300]
    if usable:
        return min(usable, key=lambda i: i["width"])["url"]
    return images[0]["url"]


def fetch_playlist(pid, token):
    meta = get(API + "/playlists/" + pid + "?fields=name", token)
    fields = "next,items(track(name,artists(name),album(name,images)))"
    url = "%s/playlists/%s/tracks?limit=100&fields=%s" % (
        API,
        pid,
        urllib.parse.quote(fields, safe="(),"),
    )
    tracks = []
    while url:
        page = get(url, token)
        for item in page.get("items", []):
            t = item.get("track")
            # local files, removed tracks and podcast episodes all show up here
            if not t or not t.get("name") or not t.get("artists"):
                continue
            album = t.get("album") or {}
            tracks.append(
                {
                    "name": t["name"],
                    "artist": t["artists"][0]["name"],
                    "album": album.get("name", ""),
                    "image": pick_image(album.get("images") or []),
                }
            )
        url = page.get("next")
    return meta["name"], tracks


def ensure_art(track):
    """Download the cover once; reuse it on every later sync."""
    base = slug(track["name"] + " " + track["artist"])
    if not base:
        return None
    path = os.path.join(ART_DIR, base + ".jpg")
    rel = "assets/music/" + base + ".jpg"
    if os.path.exists(path):
        return rel
    if not track["image"]:
        return None
    req = urllib.request.Request(track["image"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    os.makedirs(ART_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print("  + cover " + rel)
    return rel


# -- render ------------------------------------------------------------
def render_cards(playlists):
    out = []
    for sub, label, tracks in playlists:
        out.append("            <!-- MUSIC - %s -->\n" % esc(label))
        for t in tracks:
            art = ensure_art(t)
            if not art:
                continue
            name = esc(t["name"])
            artist = esc(t["artist"])
            desc = esc(t["artist"] + " · " + t["album"]) if t["album"] else artist
            out.append(
                '            <div class="exp-card" data-cat="music" data-sub="%s"\n'
                '                 data-thumb="%s"\n'
                '                 data-org="%s" data-role="%s"\n'
                '                 data-desc="%s">\n'
                '              <div class="exp-card-logo">'
                '<img src="%s" alt="%s"></div>\n'
                '              <div class="exp-org">%s</div>\n'
                '              <div class="exp-role">%s</div>\n'
                "            </div>\n"
                % (sub, art, name, artist, desc, art, name, name, artist)
            )
    return "\n".join(out)


def render_tabs(playlists):
    return "".join(
        '              <a class="exp-subcat-link" href="#" data-sub="%s">%s</a>\n'
        % (sub, esc(label))
        for sub, label, _ in playlists
    )


def splice(page, start, end, body):
    i = page.index(start)
    j = page.index(end)
    return page[: i + len(start)] + "\n" + body + page[j:]


# -- main --------------------------------------------------------------
def main():
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    if any("PASTE_PLAYLIST_ID" in c["id"] for c in config):
        sys.exit("Fill in the playlist ids in " + os.path.basename(CONFIG) + " first")

    token = token_from_env()

    playlists = []
    seen = set()
    for entry in config:
        pid = playlist_id(entry["id"])
        name, tracks = fetch_playlist(pid, token)
        label = entry.get("label") or name
        sub = slug(label, 24) or ("playlist%d" % (len(playlists) + 1))
        while sub in seen:  # two playlists could slug alike
            sub += "-2"
        seen.add(sub)
        print("%s: %d tracks" % (label, len(tracks)))
        if not tracks:
            sys.exit(label + " came back empty -- refusing to wipe the section")
        playlists.append((sub, label, tracks))

    with open(PAGE, encoding="utf-8") as f:
        page = f.read()
    for marker in (CARDS_START, CARDS_END, TABS_START, TABS_END):
        if marker not in page:
            sys.exit("marker " + marker + " missing from media.html")

    page = splice(page, TABS_START, TABS_END, render_tabs(playlists))
    page = splice(page, CARDS_START, CARDS_END, render_cards(playlists) + "\n")

    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(page)
    print("media.html updated -- %d tracks" % sum(len(t) for _, _, t in playlists))


if __name__ == "__main__":
    main()
