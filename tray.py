"""A system tray icon for WiiM Remote: open the page, play/pause, or quit.

Implements StatusNotifierItem plus its dbusmenu directly on the session bus, so
the tray needs no GTK or appindicator packages — just dbus-next, which the MPRIS
bridge already uses.
"""
import asyncio
import os
import threading

from dbus_next import BusType, PropertyAccess, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method, signal

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierMenu"
WATCHER = "org.kde.StatusNotifierWatcher"

OPEN, PLAYPAUSE, SEPARATOR, QUIT = 1, 2, 3, 4


class Menu(ServiceInterface):
    """com.canonical.dbusmenu: a fixed three-item menu."""

    def __init__(self, on_click):
        super().__init__("com.canonical.dbusmenu")
        self._on_click = on_click
        self._revision = 1

    def _items(self):
        return [
            (OPEN, {"label": Variant("s", "Open WiiM Remote")}),
            (PLAYPAUSE, {"label": Variant("s", "Play/Pause")}),
            (SEPARATOR, {"type": Variant("s", "separator")}),
            (QUIT, {"label": Variant("s", "Quit")}),
        ]

    @staticmethod
    def _props(props):
        return {"enabled": Variant("b", True), "visible": Variant("b", True), **props}

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        children = [Variant("(ia{sv}av)", [i, self._props(p), []]) for i, p in self._items()]
        root = [0, {"children-display": Variant("s", "submenu")}, children]
        return [self._revision, root]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        return [[i, self._props(p)] for i, p in self._items() if not ids or i in ids]

    @method()
    def GetProperty(self, id: "i", name: "s") -> "v":
        for i, p in self._items():
            if i == id and name in p:
                return p[name]
        return Variant("s", "")

    @method()
    def Event(self, id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self._on_click(id)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for id_, event_id, data, timestamp in events:
            self.Event(id_, event_id, data, timestamp)
        return []

    @method()
    def AboutToShow(self, id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @signal()
    def ItemsPropertiesUpdated(self) -> "a(ia{sv})a(ias)":
        return [[], []]

    @signal()
    def LayoutUpdated(self) -> "ui":
        return [self._revision, 0]


class Item(ServiceInterface):
    """org.kde.StatusNotifierItem: the icon itself."""

    def __init__(self, on_activate, icon_name="wiim-remote"):
        super().__init__("org.kde.StatusNotifierItem")
        self._on_activate = on_activate
        self._icon = icon_name
        self.tooltip_text = "WiiM Remote"

    @method()
    def Activate(self, x: "i", y: "i"):
        self._on_activate()

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        self._on_activate()

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass                                  # the menu is served over dbusmenu

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "wiim-remote"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "WiiM Remote"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return self._icon

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return str(os.path.join(os.path.expanduser("~"), ".local/share/icons"))

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return [self._icon, [], "WiiM Remote", self.tooltip_text]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False                          # left click activates, right click opens the menu

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @signal()
    def NewIcon(self):
        pass

    @signal()
    def NewToolTip(self):
        pass

    @signal()
    def NewStatus(self) -> "s":
        return "Active"


class Tray:
    def __init__(self, on_activate, on_open, on_playpause, on_quit, describe=lambda: "WiiM Remote"):
        self._on_activate = on_activate          # left click
        self._actions = {OPEN: on_open, PLAYPAUSE: on_playpause, QUIT: on_quit}
        self._describe = describe
        self._item = None

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def _click(self, item_id):
        action = self._actions.get(item_id)
        if action:
            threading.Thread(target=action, daemon=True).start()

    async def _run(self):
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._item = Item(lambda: threading.Thread(target=self._on_activate, daemon=True).start())
        bus.export(ITEM_PATH, self._item)
        bus.export(MENU_PATH, Menu(self._click))
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        await bus.request_name(name)

        introspection = await bus.introspect(WATCHER, "/StatusNotifierWatcher")
        watcher = bus.get_proxy_object(WATCHER, "/StatusNotifierWatcher", introspection)
        await watcher.get_interface(WATCHER).call_register_status_notifier_item(name)

        loop = asyncio.get_running_loop()
        while True:                            # keep the tooltip showing the current track
            await asyncio.sleep(5)
            try:
                text = await loop.run_in_executor(None, self._describe)
                if text and text != self._item.tooltip_text:
                    self._item.tooltip_text = text
                    self._item.NewToolTip()
            except Exception:
                pass
