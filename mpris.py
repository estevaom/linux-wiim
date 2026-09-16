"""Publish the playing WiiM as an MPRIS player, so desktop media keys control it.

Desktops route media keys to whichever MPRIS player is playing; a player doesn't
have to make sound on this machine, which is what lets a remote speaker qualify.

The bus name is claimed only while a device has a track loaded and released when
it stops, so an idle WiiM Remote never takes the media keys from another player.
"""
import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path

import requests
import urllib3
from dbus_next import BusType, PropertyAccess, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BUS_NAME = "org.mpris.MediaPlayer2.wiimremote"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ART_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "wiim-remote"
POLL_SECONDS = 2.0
STATUS = {"PLAYING": "Playing", "PAUSED_PLAYBACK": "Paused", "TRANSITIONING": "Playing"}


class Root(ServiceInterface):
    def __init__(self, on_raise):
        super().__init__("org.mpris.MediaPlayer2")
        self._on_raise = on_raise

    @method()
    def Raise(self):
        self._on_raise()

    @method()
    def Quit(self):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "WiiM Remote"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return "WiiM Remote"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class Player(ServiceInterface):
    def __init__(self, control):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._control = control            # (action, value=None) -> None, may block
        self.status = "Stopped"
        self.metadata = {}
        self.position = 0                  # microseconds, as of `self.measured_at`
        self.measured_at = time.monotonic()
        self.volume = 0.0
        self.can_seek = False

    async def _run(self, action, value=None):
        await asyncio.get_running_loop().run_in_executor(None, self._control, action, value)

    @method()
    async def PlayPause(self):
        await self._run("toggle")

    @method()
    async def Play(self):
        await self._run("play")

    @method()
    async def Pause(self):
        await self._run("pause")

    @method()
    async def Stop(self):
        await self._run("stop")

    @method()
    async def Next(self):
        await self._run("next")

    @method()
    async def Previous(self):
        await self._run("prev")

    @method()
    async def Seek(self, offset: "x"):
        await self._run("seek", max(0, self.live_position() + offset) // 1_000_000)

    @method()
    async def SetPosition(self, track_id: "o", position: "x"):
        await self._run("seek", max(0, position) // 1_000_000)

    @method()
    async def OpenUri(self, uri: "s"):
        pass

    def live_position(self):
        """Position now, interpolated between polls so progress bars move smoothly."""
        if self.status != "Playing":
            return self.position
        return self.position + int((time.monotonic() - self.measured_at) * 1_000_000)

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self.status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self.metadata

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return self.live_position()

    @dbus_property(access=PropertyAccess.READWRITE)
    def Volume(self) -> "d":
        return self.volume

    @Volume.setter
    def Volume(self, value: "d"):
        self.volume = max(0.0, min(1.0, value))
        threading.Thread(target=self._control, args=("volume", round(self.volume * 100)), daemon=True).start()

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return self.can_seek

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True


def cache_art(url):
    """MPRIS wants a URI the desktop can load itself: save the art locally."""
    if not url:
        return ""
    ART_DIR.mkdir(parents=True, exist_ok=True)
    path = ART_DIR / f"art-{hashlib.sha1(url.encode()).hexdigest()[:16]}.jpg"
    if not path.exists():
        try:
            r = requests.get(url, timeout=10, verify=False)
            r.raise_for_status()
            path.write_bytes(r.content)
        except requests.RequestException:
            return ""
    return path.as_uri()


class Bridge:
    """Mirrors one WiiM's playback onto the session bus."""

    def __init__(self, get_state, control, on_raise=lambda: None):
        self._get_state = get_state        # () -> dict | None
        self._control = control
        self._on_raise = on_raise
        self._owned = False
        self._track_serial = 0
        self._art = ("", "")               # (source url, cached uri)

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    async def _run(self):
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        root, player = Root(self._on_raise), Player(self._control)
        bus.export(OBJECT_PATH, root)
        bus.export(OBJECT_PATH, player)
        loop = asyncio.get_running_loop()
        while True:
            try:
                state = await loop.run_in_executor(None, self._get_state)
                await self._apply(bus, player, state)
            except Exception:
                pass                        # a device blip must not kill the bridge
            await asyncio.sleep(POLL_SECONDS)

    async def _apply(self, bus, player, state):
        status = STATUS.get((state or {}).get("state", ""), "Stopped")
        has_track = bool(state and state.get("title") and status != "Stopped")

        if has_track and not self._owned:
            await bus.request_name(BUS_NAME)
            self._owned = True
        elif not has_track and self._owned:
            await bus.release_name(BUS_NAME)
            self._owned = False
            player.status, player.metadata = "Stopped", {}
            return
        if not has_track:
            return

        changed = {}
        if status != player.status:
            player.status = status
            changed["PlaybackStatus"] = Variant("s", status)

        if state["art"] != self._art[0]:
            self._art = (state["art"], cache_art(state["art"]))

        title, artist, album = state["title"], state.get("artist", ""), state.get("album", "")
        if (title, artist, album) != (player.metadata.get("xesam:title", Variant("s", "")).value,
                                      (player.metadata.get("xesam:artist", Variant("as", [""])).value or [""])[0],
                                      player.metadata.get("xesam:album", Variant("s", "")).value):
            self._track_serial += 1
        metadata = {
            "mpris:trackid": Variant("o", f"/com/wiimremote/track/{self._track_serial}"),
            "mpris:length": Variant("x", int(state.get("duration", 0)) * 1_000_000),
            "xesam:title": Variant("s", title),
            "xesam:artist": Variant("as", [artist] if artist else []),
            "xesam:album": Variant("s", album),
        }
        if self._art[1]:
            metadata["mpris:artUrl"] = Variant("s", self._art[1])
        if metadata != player.metadata:
            player.metadata = metadata
            changed["Metadata"] = Variant("a{sv}", metadata)

        player.position = int(state.get("position", 0)) * 1_000_000
        player.measured_at = time.monotonic()
        player.can_seek = bool(state.get("duration"))

        volume = max(0.0, min(1.0, state.get("volume", 0) / 100))
        if abs(volume - player.volume) > 0.001:
            player.volume = volume
            changed["Volume"] = Variant("d", volume)

        if changed:
            player.emit_properties_changed({k: v.value for k, v in changed.items()})
