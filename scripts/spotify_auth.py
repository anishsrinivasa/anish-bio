#!/usr/bin/env python3
"""One-time helper: get a Spotify refresh token for the sync workflow.

Client Credentials tokens can't read catalog data (tracks/albums/playlist
items all 403), so the sync needs a user-authorized token. Run this once
locally; it prints a refresh token to store as the SPOTIFY_REFRESH_TOKEN
repo secret. The refresh token doesn't expire unless you revoke it.

    python scripts/spotify_auth.py

Requires REDIRECT_URI to be registered on the app at
developer.spotify.com/dashboard -> your app -> Settings -> Redirect URIs.
"""
import base64
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"

_code = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _code.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _code
        self.wfile.write(
            b"<h2>Authorized - you can close this tab.</h2>"
            if ok
            else b"<h2>Authorization failed - check the terminal.</h2>"
        )

    def log_message(self, *args):
        pass  # keep the terminal clean


def main():
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first")

    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
        }
    )

    server = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Opening Spotify authorization in your browser...")
    print("If it doesn't open, paste this URL:\n\n" + auth_url + "\n")
    webbrowser.open(auth_url)

    server.timeout = 180
    while "code" not in _code and "error" not in _code:
        server.handle_request()

    if "error" in _code:
        sys.exit("Spotify returned: " + _code["error"])
    if _code.get("state") != state:
        sys.exit("state mismatch - aborting")

    basic = base64.b64encode(("%s:%s" % (cid, secret)).encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": _code["code"],
                "redirect_uri": REDIRECT_URI,
            }
        ).encode(),
        headers={"Authorization": "Basic " + basic},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)

    print("\nRefresh token:\n")
    print(tok["refresh_token"])
    print("\nStore it with:")
    print("  gh secret set SPOTIFY_REFRESH_TOKEN --repo anishsrinivasa/anish-bio")


if __name__ == "__main__":
    main()
