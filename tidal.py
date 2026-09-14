"""Search Tidal and resolve tracks to stream URLs a WiiM can play.

The WiiM can't play DASH manifests, so the session asks for 16-bit lossless,
which Tidal serves as one URL: FLAC inside an MP4 container (audio/mp4). The WiiM
plays that fine (tested 2026-09-14). Hi-res comes back as DASH and is skipped.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import requests
import tidalapi

CONFIG = Path.home() / ".config" / "wiim-remote"
SESSION_FILE = CONFIG / "tidal-session.json"


class Tidal:
    def __init__(self):
        self.session = tidalapi.Session()
        self.session.audio_quality = tidalapi.Quality.high_lossless
        s = json.loads(SESSION_FILE.read_text())
        expiry = datetime.fromisoformat(s["expiry_time"]) if s.get("expiry_time") else None
        if not self.session.load_oauth_session(s["token_type"], s["access_token"], s["refresh_token"], expiry):
            raise RuntimeError("tidal session invalid; run login_tidal.py again")
        self._save()

    def _save(self):
        old = os.umask(0o077)
        try:
            SESSION_FILE.write_text(json.dumps({
                "token_type": self.session.token_type,
                "access_token": self.session.access_token,
                "refresh_token": self.session.refresh_token,
                "expiry_time": self.session.expiry_time.isoformat() if self.session.expiry_time else None,
            }))
        finally:
            os.umask(old)

    def search(self, query, limit=20):
        r = self.session.search(query, models=[tidalapi.Artist, tidalapi.Album, tidalapi.Track], limit=limit)
        return {
            "artists": [{"id": a.id, "title": a.name, "thumb": _img(a, 320)} for a in r.get("artists", [])],
            "albums": [self._album(a) for a in r.get("albums", [])],
            "tracks": [self._track(t) for t in r.get("tracks", [])],
        }

    def album_tracks(self, album_id):
        return [self._track(t) for t in self.session.album(album_id).tracks()]

    def artist_albums(self, artist_id):
        return [self._album(a) for a in self.session.artist(artist_id).get_albums()]

    def favorite_albums(self, limit=100):
        return [self._album(a) for a in self.session.user.favorites.albums(limit=limit)]

    def stream(self, track_id):
        t = self.session.track(track_id)
        url = t.get_url()
        # Lossless arrives as FLAC inside an MP4 container, not a bare .flac file.
        try:
            mime = requests.get(url, headers={"Range": "bytes=0-0"}, timeout=5).headers.get("Content-Type", "audio/mp4")
        except requests.RequestException:
            mime = "audio/mp4"
        return {
            "url": url,
            "mime": mime.split(";")[0],
            "title": t.name,
            "artist": t.artist.name if t.artist else "",
            "album": t.album.name if t.album else "",
            "album_id": t.album.id if t.album else None,
            "art": _img(t.album, 1280) if t.album else "",
        }

    @staticmethod
    def _album(a):
        return {"id": a.id, "title": a.name, "artist": a.artist.name if a.artist else "",
                "year": a.year, "thumb": _img(a, 640), "tracks": a.num_tracks}

    @staticmethod
    def _track(t):
        return {"id": t.id, "title": t.name, "index": t.track_num,
                "artist": t.artist.name if t.artist else "", "album": t.album.name if t.album else "",
                "album_id": t.album.id if t.album else None, "thumb": _img(t.album, 640) if t.album else "",
                "duration": t.duration, "format": "FLAC"}


def _img(obj, size):
    try:
        return obj.image(size)
    except Exception:  # objects without artwork raise
        return ""
