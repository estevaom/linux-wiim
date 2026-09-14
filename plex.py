"""Browse the Plex music library and build stream URLs a WiiM can fetch.

API calls go to PLEX_URL (default: a Plex server on this machine). URLs handed to
the WiiM must be reachable from the device, so they use PLEX_LAN_URL if set, else
this machine's own address on the route to that device. Those URLs carry the token
and never go to the browser; the web app proxies artwork instead.
"""
import os
import socket
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

CONFIG = Path.home() / ".config" / "wiim-remote"
LOCAL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400").rstrip("/")
LAN_OVERRIDE = os.environ.get("PLEX_LAN_URL", "").rstrip("/")

MIME = {"flac": "audio/flac", "mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
        "aac": "audio/aac", "ogg": "audio/ogg", "wav": "audio/wav", "alac": "audio/mp4"}


def lan_base(device_ip):
    """The Plex server's address as the WiiM at `device_ip` can reach it."""
    if LAN_OVERRIDE:
        return LAN_OVERRIDE
    local = urlparse(LOCAL)
    if local.hostname not in ("127.0.0.1", "localhost", "::1"):
        return LOCAL                 # a remote server: the device uses the same address
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((device_ip, 9))    # sends nothing; asks the kernel which source address it would use
        return f"http://{s.getsockname()[0]}:{local.port or 32400}"
    finally:
        s.close()


class Plex:
    def __init__(self):
        self.token = (CONFIG / "plex-token").read_text().strip()
        self.client_id = (CONFIG / "plex-client-id").read_text().strip()

    def _get(self, path, **params):
        r = requests.get(f"{LOCAL}{path}", params=params, timeout=10, headers={
            "Accept": "application/json", "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": self.client_id, "X-Plex-Product": "WiiM Remote"})
        r.raise_for_status()
        return r.json()["MediaContainer"]

    def music_sections(self):
        return [{"id": s["key"], "title": s["title"]}
                for s in self._get("/library/sections").get("Directory", []) if s.get("type") == "artist"]

    def albums(self, section_id, sort="titleSort", limit=500):
        c = self._get(f"/library/sections/{section_id}/all", type=9, sort=sort,
                      **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": limit})
        return [self._album(a) for a in c.get("Metadata", [])]

    def recently_added(self, section_id, limit=40):
        return self.albums(section_id, sort="addedAt:desc", limit=limit)

    def album_tracks(self, album_key):
        return [self._track(t) for t in self._get(f"/library/metadata/{album_key}/children").get("Metadata", [])]

    def track(self, track_key):
        return self._track(self._get(f"/library/metadata/{track_key}")["Metadata"][0])

    def search(self, query, limit=20):
        out = {"artists": [], "albums": [], "tracks": []}
        for hub in self._get("/hubs/search", query=query, limit=limit).get("Hub", []):
            kind, items = hub.get("type"), hub.get("Metadata", [])
            if kind == "album":
                out["albums"] = [self._album(a) for a in items]
            elif kind == "track":
                out["tracks"] = [self._track(t) for t in items]
            elif kind == "artist":
                out["artists"] = [{"id": a["ratingKey"], "title": a["title"], "thumb": a.get("thumb")} for a in items]
        return out

    def artist_albums(self, artist_key):
        return [self._album(a) for a in self._get(f"/library/metadata/{artist_key}/children").get("Metadata", [])]

    # --- URLs --------------------------------------------------------------
    def art_url(self, thumb, size=600, base=LOCAL):
        """Square transcoded artwork. base=lan_base(...) for the WiiM, LOCAL for the backend proxy."""
        if not thumb:
            return ""
        return (f"{base}/photo/:/transcode?width={size}&height={size}&minSize=1&upscale=1"
                f"&url={quote(thumb, safe='')}&X-Plex-Token={self.token}")

    def fetch_art(self, thumb, size=600):
        r = requests.get(self.art_url(thumb, size), timeout=10)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/jpeg")

    def stream(self, track_key, device_ip):
        """What the WiiM at `device_ip` needs to play a track: a URL it can reach, mime and display metadata."""
        t = self._get(f"/library/metadata/{track_key}")["Metadata"][0]
        part = t["Media"][0]["Part"][0]
        container = (part.get("container") or t["Media"][0].get("container") or "flac").lower()
        base = lan_base(device_ip)
        return {
            "url": f"{base}{part['key']}?X-Plex-Token={self.token}",
            "mime": MIME.get(container, "audio/flac"),
            "title": t.get("title", ""),
            "artist": t.get("originalTitle") or t.get("grandparentTitle", ""),
            "album": t.get("parentTitle", ""),
            "album_id": t.get("parentRatingKey"),
            "art": self.art_url(t.get("parentThumb") or t.get("thumb"), 1000, base=base),
        }

    def album_of(self, rating_key):
        """(album key, album title) for a track or album key, or (None, None)."""
        try:
            m = self._get(f"/library/metadata/{rating_key}")["Metadata"][0]
        except (requests.RequestException, KeyError, IndexError):
            return None, None
        if m.get("type") == "album":
            return m["ratingKey"], m.get("title")
        if m.get("type") == "track":
            return m.get("parentRatingKey"), m.get("parentTitle")
        return None, None

    def find_album(self, title, album):
        """Album key for a track playing elsewhere, matched by track title and album name."""
        for t in self.search(title)["tracks"]:
            if t["album"] == album:
                return t["album_id"]
        return None

    # --- shaping -------------------------------------------------------------
    @staticmethod
    def _album(a):
        return {"id": a["ratingKey"], "title": a.get("title", ""), "artist": a.get("parentTitle", ""),
                "year": a.get("year"), "thumb": a.get("thumb"), "tracks": a.get("leafCount")}

    @staticmethod
    def _track(t):
        media = (t.get("Media") or [{}])[0]
        return {"id": t["ratingKey"], "title": t.get("title", ""), "index": t.get("index"),
                "artist": t.get("originalTitle") or t.get("grandparentTitle", ""),
                "album": t.get("parentTitle", ""), "album_id": t.get("parentRatingKey"),
                "thumb": t.get("parentThumb") or t.get("thumb"),
                "duration": (t.get("duration") or 0) // 1000,
                "format": (media.get("audioCodec") or "").upper(),
                "bits": (media.get("Part") or [{}])[0].get("Stream", [{}])[0].get("bitDepth") if media.get("Part") else None}
