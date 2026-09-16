"""WiiM Remote: see what's playing on the WiiMs, pick music from Plex or Tidal, play it.

Run: .venv/bin/python app.py  (serves http://0.0.0.0:8765)
Tokens stay in this process. The browser gets artwork through /api/art proxies,
never a URL with a Plex token in it.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

import wiim

PORT = 8765
CONFIG = Path.home() / ".config" / "wiim-remote"
app = Flask(__name__, static_folder="static")

_devices = {}
_plex = None
_tidal = None
_lock = threading.Lock()

# The device last used from the web UI. The media keys follow it when nothing is
# playing, and it is kept on disk so a restart doesn't forget which WiiM you're on.
LAST_DEVICE_FILE = CONFIG / "last-device"
try:
    _last_device = LAST_DEVICE_FILE.read_text().strip() or None
except OSError:
    _last_device = None


def set_last_device(ip):
    global _last_device
    if ip and ip != _last_device:
        _last_device = ip
        try:
            LAST_DEVICE_FILE.write_text(ip)
        except OSError:
            pass


def plex():
    global _plex
    if _plex is None:
        from plex import Plex
        _plex = Plex()
    return _plex


def tidal():
    global _tidal
    if _tidal is None:
        from tidal import Tidal
        _tidal = Tidal()
    return _tidal


def device(ip):
    if ip not in _devices:
        refresh_devices()
    if ip not in _devices:
        abort(404, "unknown device")
    return wiim.WiiM(ip)


def refresh_devices():
    found = wiim.discover()
    if found:
        _devices.clear()
        _devices.update({d.ip: d for d in found})


# --- queue ----------------------------------------------------------------------
# The WiiM only holds one URL at a time when driven over UPnP, so albums are queued
# here and advanced when a track finishes.

class Queue:
    def __init__(self, source, ids, index):
        self.source, self.ids, self.index = source, ids, index
        self.path = None        # URL path of the track we started; used to spot takeovers
        self.album_id = None
        self.started = 0.0
        self.saw_playing = False


_queues = {}


def resolve(source, track_id, ip):
    if source == "plex":
        return plex().stream(track_id, ip)
    if source == "tidal":
        return tidal().stream(track_id)
    abort(400, "unknown source")


def start_track(ip, q):
    s = resolve(q.source, q.ids[q.index], ip)
    wiim.WiiM(ip).play_url(s["url"], s["title"], s["artist"], s["album"], s["art"], s["mime"])
    q.path, q.album_id = urlparse(s["url"]).path, s.get("album_id")
    q.started, q.saw_playing = time.time(), False


def queue_watcher():
    while True:
        time.sleep(2)
        for ip, q in list(_queues.items()):
            try:
                np = wiim.WiiM(ip).now_playing()
            except Exception:
                continue
            with _lock:
                if _queues.get(ip) is not q:
                    continue
                mine = urlparse(np["uri"]).path == q.path
                if np["state"] == "PLAYING" and np["uri"] and not mine and time.time() - q.started > 8:
                    _queues.pop(ip, None)      # something else took over the device
                    continue
                if mine and np["state"] == "PLAYING":
                    q.saw_playing = True
                if q.saw_playing and np["state"] == "STOPPED":
                    if q.index + 1 < len(q.ids):
                        q.index += 1
                        try:
                            start_track(ip, q)
                        except Exception as e:
                            app.logger.warning("queue advance failed: %s", e)
                            _queues.pop(ip, None)
                    else:
                        _queues.pop(ip, None)


# --- which album is playing -----------------------------------------------------
# Needed to make the now-playing artwork open its album. Music started from this app
# knows its album; music started from the WiiM app's own Plex integration is worked
# out from the Plex item key in its artwork URL, then by title search. Cached per
# track because /api/now is polled every two seconds.

_album_links = {}


def plex_album_link(title, album, art):
    key = (title, album)
    if key in _album_links:
        return _album_links[key]
    album_id = None
    m = re.search(r"/library/metadata/(\d+)", unquote(unquote(art or "")))
    if m:
        found_id, found_title = plex().album_of(m.group(1))
        if found_id and (not album or found_title == album):
            album_id = found_id
    if not album_id and title:
        try:
            album_id = plex().find_album(title, album)
        except Exception:
            album_id = None
    link = {"src": "plex", "id": str(album_id)} if album_id else None
    _album_links[key] = link
    return link


def now_for(ip):
    """Everything known about one device's playback. Shared by /api/now and MPRIS."""
    np = device(ip).now_playing()
    np["ip"] = ip
    host = urlparse(np["uri"]).netloc
    if ":32400" in host:
        np["source"] = "Plex"
    elif "tidal" in host or "tidal" in np["source"].lower():
        np["source"] = "Tidal"

    q = _queues.get(ip)
    np["queue"] = {"source": q.source, "index": q.index, "length": len(q.ids)} if q else None
    np["album_link"] = None
    if q and q.album_id and urlparse(np["uri"]).path == q.path:
        np["album_link"] = {"src": q.source, "id": str(q.album_id)}
    elif np["source"] == "Plex" and np["title"]:
        np["album_link"] = plex_album_link(np["title"], np["album"], np["art"])
    return np


# --- media keys -----------------------------------------------------------------
# The desktop sends media keys to whichever MPRIS player is playing, so the WiiM is
# published as one while it has a track. See mpris.py.

_mpris_ip = None


def mpris_device():
    """The device the media keys should drive: the last one used here, else one that's playing."""
    global _mpris_ip
    if not _devices:
        refresh_devices()
    order = ([_last_device] if _last_device in _devices else []) + [ip for ip in _devices if ip != _last_device]
    fallback = None
    for ip in order:
        try:
            np = now_for(ip)
        except Exception:
            continue
        if np["state"] in ("PLAYING", "TRANSITIONING"):
            _mpris_ip = ip
            return np
        if fallback is None and np["title"]:
            fallback = np
    _mpris_ip = fallback["ip"] if fallback else None
    return fallback


def mpris_control(action, value=None):
    """Act on the device the bridge is mirroring; no extra polling, so keys stay snappy."""
    ip = _mpris_ip or (_last_device if _last_device in _devices else None) or next(iter(_devices), None)
    if ip:
        do_control(ip, action, value)


# Chromium derives an app window's class from its URL and ignores --class, so this
# is what a window opened on /mini ends up as. The Hyprland rule matches it too.
MINI_CLASS = "chrome-127.0.0.1__mini-Default"
FULL_CLASS = "chrome-127.0.0.1__-Default"


def raise_window():
    """The desktop's "open the player" action: focus the web app, or start it."""
    subprocess.Popen(["omarchy-launch-or-focus-webapp", "WiiM Remote", f"http://127.0.0.1:{PORT}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def toggle_mini():
    """Tray left click: show the mini player, or close it if it's already up."""
    clients = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout
    if f'"{MINI_CLASS}"' in clients:
        # hyprctl dispatch parses its argument as Lua on this build, and the
        # dispatcher is hl.dsp.window.close — not the closewindow of the docs.
        subprocess.run(["hyprctl", "dispatch", f'hl.dsp.window.close("class:{MINI_CLASS}")'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["omarchy-launch-webapp", f"http://127.0.0.1:{PORT}/mini", "--window-size=340,470"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tray_tooltip():
    np = mpris_device()
    if not np or not np.get("title"):
        return "Nothing playing"
    line = " — ".join(x for x in (np["title"], np.get("artist")) if x)
    return f"{line}\n{_devices[np['ip']].name}" if np["ip"] in _devices else line


def close_windows(*classes):
    """Close our own windows, addressed individually.

    Matching on class alone proved unreliable when two closes were dispatched
    back to back; an address names exactly one window. Never match on title:
    Claude Code renames its terminal, and a title match once closed it.
    """
    try:
        clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"],
                                            capture_output=True, text=True, timeout=5).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    for c in clients:
        if c.get("class") in classes:
            subprocess.run(["hyprctl", "dispatch", f'hl.dsp.window.close("address:{c["address"]}")'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.2)


def quit_app():
    """Tray → Quit: close both windows, then exit.

    The pages are separate browser windows, so quitting the server alone would
    leave them up showing a dead app. Exiting cleanly keeps systemd from
    restarting it (Restart=on-failure).
    """
    close_windows(MINI_CLASS, FULL_CLASS)
    time.sleep(0.5)
    os._exit(0)


# --- pages ------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/mini")
def mini():
    """Small player for the tray: album art, progress, transport."""
    return send_from_directory("static", "mini.html")


@app.get("/api/active")
def active():
    """Whatever the media keys and the mini player act on right now."""
    np = mpris_device()
    if not np:
        return jsonify(ip=None, state="STOPPED", title="", art="", position=0, duration=0, volume=None)
    np.pop("uri", None)
    if np["art"]:
        np["art"] = f"/api/art/now?ip={np['ip']}&k={abs(hash(np['art'])) % 10**8}"
    return jsonify(np)


@app.post("/api/open-full")
def open_full():
    raise_window()
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    return jsonify(plex=(CONFIG / "plex-token").exists(), tidal=(CONFIG / "tidal-session.json").exists())


@app.get("/api/devices")
def devices():
    if request.args.get("refresh") or not _devices:
        refresh_devices()
    return jsonify([{"ip": d.ip, "name": d.name, "model": d.model} for d in _devices.values()])


@app.get("/api/now")
def now():
    ip = request.args["ip"]
    np = now_for(ip)
    set_last_device(ip)
    np.pop("uri")
    if np["art"]:
        np["art"] = f"/api/art/now?ip={ip}&k={abs(hash(np['art'])) % 10**8}"
    return jsonify(np)


@app.get("/api/art/now")
def art_now():
    np = device(request.args["ip"]).now_playing()
    if not np["art"]:
        abort(404)
    r = requests.get(np["art"], timeout=10, verify=False)
    return Response(r.content, content_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "max-age=3600"})


def do_control(ip, action, value=None):
    d = device(ip)
    q = _queues.get(ip)
    if action in ("next", "prev") and q:
        with _lock:
            q.index = max(0, min(len(q.ids) - 1, q.index + (1 if action == "next" else -1)))
            start_track(ip, q)
    elif action == "play":
        d.resume()
    elif action in ("toggle", "pause"):
        d.toggle()
    elif action == "next":
        d.next()
    elif action == "prev":
        d.prev()
    elif action == "stop":
        _queues.pop(ip, None)
        d.stop()
    elif action == "seek":
        d.seek(value)
    elif action == "volume":
        d.set_volume(value)
    else:
        abort(400, "unknown action")


@app.post("/api/control")
def control():
    body = request.get_json()
    do_control(body["ip"], body["action"], body.get("value"))
    set_last_device(body["ip"])
    return jsonify(ok=True)


@app.post("/api/play")
def play():
    """Play a list of track ids from one source, starting at `index`."""
    body = request.get_json()
    ip, source, ids = body["ip"], body["source"], [str(i) for i in body["ids"]]
    device(ip)
    q = Queue(source, ids, int(body.get("index", 0)))
    with _lock:
        _queues[ip] = q
        start_track(ip, q)
    set_last_device(ip)
    return jsonify(ok=True)


# --- Plex -----------------------------------------------------------------------

@app.get("/api/plex/sections")
def plex_sections():
    return jsonify(plex().music_sections())


@app.get("/api/plex/albums")
def plex_albums():
    sort = request.args.get("sort", "titleSort")
    return jsonify(plex().albums(request.args["section"], sort=sort))


@app.get("/api/plex/album/<key>")
def plex_album(key):
    return jsonify(plex().album_tracks(key))


@app.get("/api/plex/artist/<key>")
def plex_artist(key):
    return jsonify(plex().artist_albums(key))


@app.get("/api/plex/search")
def plex_search():
    return jsonify(plex().search(request.args["q"]))


@app.get("/api/plex/art")
def plex_art():
    thumb = request.args.get("thumb", "")
    if not thumb.startswith("/library/"):
        abort(400)
    data, ctype = plex().fetch_art(thumb, int(request.args.get("size", 400)))
    return Response(data, content_type=ctype, headers={"Cache-Control": "max-age=86400"})


# --- Tidal ----------------------------------------------------------------------

@app.get("/api/tidal/favorites")
def tidal_favorites():
    return jsonify(tidal().favorite_albums())


@app.get("/api/tidal/album/<key>")
def tidal_album(key):
    return jsonify(tidal().album_tracks(key))


@app.get("/api/tidal/artist/<key>")
def tidal_artist(key):
    return jsonify(tidal().artist_albums(key))


@app.get("/api/tidal/search")
def tidal_search():
    return jsonify(tidal().search(request.args["q"]))


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    if code == 500:
        app.logger.exception(e)
    return jsonify(error=str(getattr(e, "description", e))), code


if __name__ == "__main__":
    threading.Thread(target=queue_watcher, daemon=True).start()
    try:
        from mpris import Bridge
        Bridge(mpris_device, mpris_control, raise_window).start()
    except Exception as e:                  # no session bus: the web app still works
        app.logger.warning("mpris bridge not started: %s", e)
    try:
        from tray import Tray
        Tray(toggle_mini, raise_window, lambda: mpris_control("toggle"), quit_app, tray_tooltip).start()
    except Exception as e:                  # no tray on this desktop: not fatal
        app.logger.warning("tray icon not started: %s", e)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
