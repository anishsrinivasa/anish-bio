#!/usr/bin/env python3
"""Regenerate the Anime section of media.html from a Discord message.

The message is the source of truth. Format:

    **watched**
    anything without **dropped** = finished
    vinland saga - 10/10
    my dress-up darling **dropped s2** - 6/10
    ...

    **TO DO:**
    Demon slayer
    ...

Shorthand titles are resolved through anime-aliases.json, which is committed
and hand-editable -- fix a bad match there once and it sticks. Unknown names
fall back to an AniList search and are written into the cache.

Local test without Discord:
    python scripts/sync_anime.py --from-file some_message.txt
"""
import html
import io
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
ART_DIR = os.path.join(ROOT, "assets", "media")
ALIASES = os.path.join(ROOT, "anime-aliases.json")
CONFIG = os.path.join(ROOT, "discord-anime.json")

CARDS_START = "<!-- ANIME:CARDS:START -->"
CARDS_END = "<!-- ANIME:CARDS:END -->"

UA = "anish-bio-anime-sync"
AL_QUERY = (
    "query($s:String){Page(perPage:1){media(search:$s,type:ANIME,sort:SEARCH_MATCH)"
    "{id title{romaji english} coverImage{extraLarge}}}}"
)


# -- helpers -----------------------------------------------------------
def slug(text, limit=50):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-")


def esc(text):
    return html.escape(text, quote=False).replace('"', "&quot;")


# -- source ------------------------------------------------------------
def fetch_discord():
    """Concatenate the configured messages, in order.

    Several messages are supported because a single one caps at 2000 chars
    (4000 with Nitro) and the list already sits just under that.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        sys.exit("DISCORD_BOT_TOKEN is not set")
    with io.open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    channel = str(cfg.get("channel_id") or "")
    ids = [str(i) for i in (cfg.get("message_ids") or [])]
    if not channel or not ids:
        # Exit clean, not failing: the scheduled run shouldn't email an error
        # every 15 minutes just because the feed isn't wired up yet.
        print("channel_id / message_ids not set in %s -- nothing to do"
              % os.path.basename(CONFIG))
        raise SystemExit(0)

    parts = []
    for mid in ids:
        url = "https://discord.com/api/v10/channels/%s/messages/%s" % (channel, mid)
        req = urllib.request.Request(
            url, headers={"Authorization": "Bot " + token, "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                parts.append(json.load(r).get("content") or "")
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            sys.exit("Discord %d on message %s -- %s" % (e.code, mid, body))
    return "\n".join(parts)


# -- parse -------------------------------------------------------------
def parse(raw):
    watched, todo, mode = [], [], None
    for line in raw.splitlines():
        l = line.strip()
        if not l:
            continue
        low = l.lower()
        if low.startswith("**watched**"):
            mode = "w"
            continue
        if low.startswith("**to do"):
            mode = "t"
            continue
        if low.startswith("anything without"):  # the legend, not an entry
            continue
        if mode == "w":
            m = re.match(r"^(.*?)\s*-\s*(\d+)\s*/\s*10\s*$", l)
            if not m:
                print("  ! skipping unparsed watched line: " + l)
                continue
            title, score = m.group(1), int(m.group(2))
            d = re.search(r"\*\*dropped(?:\s*(s\d+))?\*\*", title, re.I)
            season = (d.group(1) or "").upper() if d else ""
            title = re.sub(r"\*\*dropped(?:\s*s\d+)?\*\*", "", title, flags=re.I)
            watched.append((title.strip(" -"), score, bool(d), season))
        elif mode == "t":
            todo.append(l)
    return watched, todo


# -- resolve -----------------------------------------------------------
def load_aliases():
    if os.path.exists(ALIASES):
        with io.open(ALIASES, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_aliases(a):
    with io.open(ALIASES, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(a.items())), f, indent=2, ensure_ascii=False)
        f.write("\n")


def anilist(term):
    req = urllib.request.Request(
        "https://graphql.anilist.co",
        data=json.dumps({"query": AL_QUERY, "variables": {"s": term}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)["data"]["Page"]["media"]
    if not d:
        return None
    m = d[0]
    return {
        "title": m["title"]["english"] or m["title"]["romaji"],
        "image": "assets/media/%s.jpg" % slug(m["title"]["english"] or m["title"]["romaji"]),
        "remote": m["coverImage"]["extraLarge"],
        "anilist_id": m["id"],
        "source": "anilist",
    }


def resolve(name, aliases):
    """Alias cache first; AniList only for names never seen before."""
    key = name.lower()
    if key in aliases:
        return aliases[key]
    print("  resolving new title: " + name)
    r = anilist(name)
    time.sleep(0.8)
    if not r:
        print("  !! no AniList match for " + name + " -- skipped")
        return None
    aliases[key] = r
    print("     -> " + r["title"])
    return r


def ensure_art(entry):
    """Covers are downloaded once and reused; site images already exist."""
    rel = entry["image"]
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    if os.path.exists(path):
        return rel
    if not entry.get("remote"):
        return None
    req = urllib.request.Request(entry["remote"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    os.makedirs(ART_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print("  + cover " + rel)
    return rel


# -- render ------------------------------------------------------------
def card(sub, title, role, art):
    logo = ('<img src="%s" alt="%s">' % (art, title)) if art else title
    return (
        '            <div class="exp-card" data-cat="anime" data-sub="%s"\n'
        '                 data-thumb="%s"\n'
        '                 data-org="%s" data-role="%s"\n'
        '                 data-desc="%s">\n'
        '              <div class="exp-card-logo">%s</div>\n'
        '              <div class="exp-org">%s</div>\n'
        '              <div class="exp-role">%s</div>\n'
        "            </div>\n" % (sub, art or "", title, role, role, logo, title, role)
    )


def render(watched, todo, aliases):
    out = ["            <!-- ══ WATCHED ══ -->\n"]
    rows = []
    for i, (name, score, dropped, season) in enumerate(watched):
        e = resolve(name, aliases)
        if not e:
            continue
        role = "My Score: %d/10" % score
        if dropped:
            role += " (Dropped%s)" % ((" " + season) if season else "")
        rows.append((score, i, esc(e["title"]), role, ensure_art(e)))
    rows.sort(key=lambda r: (-r[0], r[1]))  # score desc, message order as tiebreak
    for _, _, title, role, art in rows:
        out.append(card("watched", title, role, art))

    out.append("            <!-- ══ UP NEXT ══ -->\n")
    seen = set()
    for name in todo:
        e = resolve(name, aliases)
        if not e or e["title"] in seen:
            continue
        seen.add(e["title"])
        out.append(card("upnext", esc(e["title"]), "Up Next", ensure_art(e)))
    return "\n".join(out), len(rows), len(seen)


# -- main --------------------------------------------------------------
def main():
    if "--from-file" in sys.argv:
        raw = io.open(sys.argv[sys.argv.index("--from-file") + 1], encoding="utf-8").read()
        print("(reading from file, Discord not contacted)")
    else:
        raw = fetch_discord()

    watched, todo = parse(raw)
    print("parsed: %d watched, %d up next" % (len(watched), len(todo)))
    if not watched:
        sys.exit("no watched entries parsed -- refusing to wipe the section")

    aliases = load_aliases()
    before = len(aliases)
    body, nw, nt = render(watched, todo, aliases)
    if len(aliases) != before:
        save_aliases(aliases)
        print("alias cache: +%d new" % (len(aliases) - before))

    page = io.open(PAGE, encoding="utf-8").read()
    for marker in (CARDS_START, CARDS_END):
        if marker not in page:
            sys.exit("marker " + marker + " missing from media.html")
    i, j = page.index(CARDS_START), page.index(CARDS_END)
    page = page[: i + len(CARDS_START)] + "\n" + body + " " * 12 + page[j:]

    with io.open(PAGE, "w", encoding="utf-8") as f:
        f.write(page)
    print("media.html updated -- %d watched, %d up next" % (nw, nt))


if __name__ == "__main__":
    main()
