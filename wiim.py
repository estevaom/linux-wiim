"""Control WiiM / LinkPlay devices over their HTTP API and UPnP.

The HTTP API (https://<ip>/httpapi.asp) gives cheap status and transport commands.
UPnP AVTransport gives full now-playing metadata (album art, source URL) and is
how we hand the device a URL with proper title/artist/art attached.
"""
import html
import re
import subprocess
import urllib3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # devices use self-signed certs

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
UPNP_PORT = 49152


@dataclass
class Device:
    ip: str
    name: str
    model: str


def discover(timeout=5):
    """Find WiiM/LinkPlay devices via mDNS (_linkplay._tcp).

    Not SSDP: a stateful firewall such as ufw drops the unicast replies to a
    multicast M-SEARCH, so SSDP can find nothing. avahi's mDNS gets through.
    """
    try:
        out = subprocess.run(["avahi-browse", "-rpt", "_linkplay._tcp"], capture_output=True,
                             text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    ips = set()
    for line in out.splitlines():
        f = line.split(";")
        if f[0] == "=" and f[2] == "IPv4" and len(f) > 7:
            ips.add(f[7])
    devices = []
    for ip in sorted(ips):
        try:
            desc = requests.get(f"http://{ip}:{UPNP_PORT}/description.xml", timeout=2).text
        except requests.RequestException:
            continue
        if "wiimu" not in desc and "Linkplay" not in desc:
            continue
        name = re.search(r"<friendlyName>([^<]*)", desc)
        model = re.search(r"<modelName>([^<]*)", desc)
        devices.append(Device(ip, name.group(1) if name else ip, model.group(1) if model else ""))
    return devices


class WiiM:
    def __init__(self, ip):
        self.ip = ip

    # --- HTTP API ---------------------------------------------------------
    def cmd(self, command):
        r = requests.get(f"https://{self.ip}/httpapi.asp", params={"command": command},
                         verify=False, timeout=5)
        r.raise_for_status()
        return r.text

    def player_status(self):
        return requests.get(f"https://{self.ip}/httpapi.asp?command=getPlayerStatus",
                            verify=False, timeout=5).json()

    def pause(self):   self.cmd("setPlayerCmd:onepause")
    def resume(self):  self.cmd("setPlayerCmd:resume")
    def toggle(self):  self.cmd("setPlayerCmd:onepause")
    def stop(self):    self.cmd("setPlayerCmd:stop")
    def next(self):    self.cmd("setPlayerCmd:next")
    def prev(self):    self.cmd("setPlayerCmd:prev")
    def seek(self, seconds): self.cmd(f"setPlayerCmd:seek:{int(seconds)}")
    def set_volume(self, vol): self.cmd(f"setPlayerCmd:vol:{max(0, min(100, int(vol)))}")

    # --- UPnP -------------------------------------------------------------
    def _soap(self, service, control, action, args=""):
        body = (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}"><InstanceID>0</InstanceID>{args}</u:{action}>'
                f'</s:Body></s:Envelope>')
        r = requests.post(f"http://{self.ip}:{UPNP_PORT}/upnp/control/{control}", data=body.encode(),
                          headers={"Content-Type": 'text/xml; charset="utf-8"',
                                   "SOAPACTION": f'"{service}#{action}"'}, timeout=5)
        r.raise_for_status()
        return r.text

    @staticmethod
    def _field(xml, tag):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
        return html.unescape(m.group(1)) if m else ""

    def now_playing(self):
        """Merged view of what the device is doing, from UPnP metadata plus HTTP status."""
        pos = self._soap(AVT, "rendertransport1", "GetPositionInfo")
        info = self._soap(AVT, "rendertransport1", "GetTransportInfo")
        meta = self._field(pos, "TrackMetaData")
        status = self.player_status()
        return {
            "state": self._field(info, "CurrentTransportState"),  # PLAYING / PAUSED_PLAYBACK / STOPPED / TRANSITIONING
            "title": _didl(meta, "dc:title") or _hex(status.get("Title")),
            "artist": _didl(meta, "upnp:artist") or _hex(status.get("Artist")),
            "album": _didl(meta, "upnp:album") or _hex(status.get("Album")),
            "art": _didl(meta, "upnp:albumArtURI"),
            "uri": self._field(pos, "TrackURI"),
            "duration": _hms(self._field(pos, "TrackDuration")),
            "position": _hms(self._field(pos, "RelTime")),
            "volume": int(status.get("vol", 0)),
            "mute": status.get("mute") == "1",
            "source": status.get("vendor") or "",
            "mode": status.get("mode"),
        }

    def play_url(self, url, title="", artist="", album="", art="", mime="audio/flac"):
        """Replace whatever is playing with `url`, tagged so the WiiM app shows it properly."""
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f'<dc:title>{escape(title)}</dc:title><upnp:artist>{escape(artist)}</upnp:artist>'
            f'<upnp:album>{escape(album)}</upnp:album><upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            f'<res protocolInfo="http-get:*:{mime}:*">{escape(url)}</res>'
            '</item></DIDL-Lite>'
        )
        args = f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>"
        self._soap(AVT, "rendertransport1", "SetAVTransportURI", args)
        self._soap(AVT, "rendertransport1", "Play", "<Speed>1</Speed>")


def _didl(meta, tag):
    if not meta or meta == "NOT_IMPLEMENTED":
        return ""
    try:
        root = ET.fromstring(meta)
    except ET.ParseError:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", meta, re.S)
        return html.unescape(m.group(1)).strip() if m else ""
    ns = {"dc": "http://purl.org/dc/elements/1.1/", "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
    el = root.find(f".//{tag}", ns)
    return (el.text or "").strip() if el is not None else ""


def _hex(value):
    try:
        return bytes.fromhex(value).decode("utf-8") if value and value != "556E6B6E6F776E" else ""
    except (ValueError, TypeError):
        return value or ""


def _hms(value):
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + int(float(s))
    except (ValueError, AttributeError):
        return 0
