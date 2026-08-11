"""Work Louder x OpenAI Codex Micro support: trigger key, RGB feedback, on-device menu.

The Micro (USB 303a:8360) does not emit keystrokes. Its 13 keys, encoder and
joystick are reported as JSON-RPC notifications on a vendor HID channel, and its
six agent-key LEDs are driven by sending JSON-RPC back. Everything here speaks
that protocol directly over ``/dev/hidrawN`` using only the standard library.

Wire format — every message is one or more 64-byte reports::

    byte 0 : 0x06        report ID
    byte 1 : channel     1 = debug log, 2 = RPC
    byte 2 : length      payload bytes in this report (0..61)
    3..63  : payload     UTF-8; long messages span several reports

Device -> host messages are newline-delimited; host -> device messages are bare
JSON with no terminator, so inbound reassembly is done by brace balancing, which
handles both.

The whole module is optional: with no device attached, :func:`find_device`
returns ``None`` and the controller thread idles harmlessly.
"""

from __future__ import annotations

import json
import logging
import os
import select
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

VENDOR_ID = "303A"
PRODUCT_ID = "8360"

REPORT_ID = 0x06
REPORT_SIZE = 64
MAX_PAYLOAD = REPORT_SIZE - 3
CHANNEL_RPC = 2

SLOT_COUNT = 6
EFFECT_OFF = 0
EFFECT_SOLID = 1

# Keycodes the firmware reports (KV_OAI_* with the prefix stripped).
AGENT_KEYS = ("AG00", "AG01", "AG02", "AG03", "AG04", "AG05")
ENCODER_CW = "ENC_CW"
ENCODER_CCW = "ENC_CC"
ENCODER_CLICK = "ENC_CLK"

ACT_RELEASE, ACT_PRESS, ACT_ROTATE = 0, 1, 2

# Menu pages cycled with the encoder. The first is the resting page.
PAGE_MODEL, PAGE_MODE, PAGE_DELIVERY = "model", "mode", "delivery"
PAGES = (PAGE_MODEL, PAGE_MODE, PAGE_DELIVERY)

MENU_TIMEOUT = 4.0  # seconds of inactivity before the menu closes itself

DIM = 0.18  # brightness multiplier for "available but not selected"


def _scale(color: int, factor: float) -> int:
    r = min(255, max(0, int(((color >> 16) & 0xFF) * factor)))
    g = min(255, max(0, int(((color >> 8) & 0xFF) * factor)))
    b = min(255, max(0, int((color & 0xFF) * factor)))
    return (r << 16) | (g << 8) | b


def parse_color(text: str) -> int:
    """``"#e93d3d"`` or ``"0xe93d3d"`` -> packed 0xRRGGBB."""
    raw = text.strip().lstrip("#")
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    return int(raw, 16) & 0xFFFFFF


def find_device() -> str | None:
    """Return the Micro's hidraw path, or None if it is not attached.

    Always resolved by vendor/product ID: the hidraw index is not stable across
    reconnects, Bluetooth/USB switches, or a trip through the bootloader.
    """
    base = "/sys/class/hidraw"
    try:
        nodes = sorted(os.listdir(base))
    except OSError:
        return None
    for node in nodes:
        try:
            with open(os.path.join(base, node, "device", "uevent")) as fh:
                info = fh.read()
        except OSError:
            continue
        for line in info.splitlines():
            if not line.startswith("HID_ID="):
                continue
            parts = line.split("=", 1)[1].split(":")
            # HID_ID=<bus>:<vendor>:<product>; 0003 = USB, 0005 = Bluetooth
            if (
                len(parts) == 3
                and parts[0] in ("0003", "0005")
                and parts[1].endswith(VENDOR_ID)
                and parts[2].endswith(PRODUCT_ID)
            ):
                return os.path.join("/dev", node)
    return None


def _encode(message: str) -> list[bytes]:
    data = message.encode("utf-8")
    reports, offset = [], 0
    while True:
        chunk = min(MAX_PAYLOAD, len(data) - offset)
        report = bytearray(REPORT_SIZE)
        report[0] = REPORT_ID
        report[1] = CHANNEL_RPC
        report[2] = chunk
        report[3 : 3 + chunk] = data[offset : offset + chunk]
        reports.append(bytes(report))
        offset += chunk
        if offset >= len(data):
            return reports


def _extract_json(buffer: str) -> tuple[list[str], str]:
    """Split complete top-level JSON objects off the front of ``buffer``."""
    objects: list[str] = []
    depth = 0
    in_string = escaped = False
    start, last_end = -1, 0
    for i, ch in enumerate(buffer):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(buffer[start : i + 1])
                last_end = i + 1
                start = -1
    rest = buffer[start:] if depth > 0 and start >= 0 else buffer[last_end:]
    return objects, rest


class CodexMicro:
    """Owns the hidraw connection: reads notifications, writes lighting."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._buffer = ""
        self._req_id = 0
        self._last_frame: tuple[int, ...] | None = None
        try:
            self._fd = os.open(path, os.O_RDWR)
            self.writable = True
        except PermissionError:
            # Readable-only is still useful: the trigger key works, LEDs do not.
            self._fd = os.open(path, os.O_RDONLY)
            self.writable = False
            log.warning(
                "%s is not writable; Codex Micro LED feedback disabled. Install the "
                "udev rule from the README to enable it.",
                path,
            )

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    # ---------- outbound ----------

    def _send(self, method: str, params: object | None = None) -> None:
        if not self.writable:
            return
        self._req_id = (self._req_id % 999) + 1
        message: dict[str, object] = {"id": self._req_id, "method": method}
        if params is not None:
            message["params"] = params
        # Host -> device is bare JSON with no terminator.
        payload = json.dumps(message, separators=(",", ":"))
        for report in _encode(payload):
            os.write(self._fd, report)

    def set_slots(self, colors: list[int], brightness: int) -> None:
        """Paint the six agent keys. Repeated identical frames are dropped."""
        frame = tuple(colors)
        if frame == self._last_frame:
            return
        self._last_frame = frame
        params = [
            {
                "id": slot,
                "c": color,
                "b": 0 if color == 0 else brightness,
                "e": EFFECT_OFF if color == 0 else EFFECT_SOLID,
                "s": 0,
            }
            for slot, color in enumerate(colors[:SLOT_COUNT])
        ]
        try:
            self._send("v.oai.thstatus", params)
        except OSError as exc:
            log.debug("Codex Micro lighting write failed: %s", exc)
            raise

    def blank(self) -> None:
        try:
            self.set_slots([0] * SLOT_COUNT, 0)
        except OSError:
            pass

    # ---------- inbound ----------

    def poll(self, timeout: float = 0.25) -> list[dict]:
        """Return any complete notifications received within ``timeout``."""
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return []
        report = os.read(self._fd, REPORT_SIZE)
        if not report:
            raise OSError("device closed")
        view = report[1:] if report[0] == REPORT_ID else report
        if len(view) < 2 or view[0] != CHANNEL_RPC:
            return []  # debug channel or a runt report
        self._buffer += bytes(view[2 : 2 + view[1]]).decode("utf-8", "replace")
        objects, self._buffer = _extract_json(self._buffer)
        messages = []
        for obj in objects:
            try:
                messages.append(json.loads(obj))
            except json.JSONDecodeError:
                continue
        return messages


class CodexMicroController:
    """Background thread: maps device input to callbacks and app state to LEDs.

    All callbacks fire on this thread. The app wraps them in Qt signals so the
    work lands on the GUI thread.
    """

    def __init__(
        self,
        settings,
        on_trigger: Callable[[], None],
        on_release: Callable[[], None],
        on_pick_model: Callable[[int], None],
        on_set_mode: Callable[[str], None],
        on_set_delivery: Callable[[str], None],
        on_presence: Callable[[str], None],
    ) -> None:
        self.settings = settings
        self.on_trigger = on_trigger
        self.on_release = on_release
        self.on_pick_model = on_pick_model
        self.on_set_mode = on_set_mode
        self.on_set_delivery = on_set_delivery
        self.on_presence = on_presence

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device: CodexMicro | None = None
        self._lock = threading.Lock()

        # Latest app state, pushed in from the GUI thread.
        self._state = "idle"
        self._enabled = True
        self._model_status = "loading"
        self._model_index = 0
        self._model_count = SLOT_COUNT
        self._mode = "toggle"
        self._delivery = "clipboard"

        self._page: str | None = None  # None = resting display
        self._page_touched = 0.0

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="codex-micro", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        with self._lock:
            if self._device is not None:
                self._device.blank()
                self._device.close()
                self._device = None

    # ---------- state in ----------

    def update(
        self,
        *,
        state: str,
        enabled: bool,
        model_status: str,
        model_index: int,
        model_count: int,
        mode: str,
        delivery: str,
    ) -> None:
        """Called from the GUI thread whenever app state changes."""
        self._state = state
        self._enabled = enabled
        self._model_status = model_status
        self._model_index = model_index
        self._model_count = max(1, model_count)
        self._mode = mode
        self._delivery = delivery
        self._paint()

    # ---------- main loop ----------

    def _run(self) -> None:
        announced = False
        while not self._stop.is_set():
            path = find_device()
            if path is None:
                if announced:
                    self.on_presence("")
                    announced = False
                self._stop.wait(2.0)
                continue
            try:
                device = CodexMicro(path)
            except OSError as exc:
                log.debug("cannot open %s: %s", path, exc)
                self._stop.wait(2.0)
                continue

            with self._lock:
                self._device = device
            log.info("Codex Micro connected at %s", path)
            self.on_presence(path)
            announced = True
            self._paint(force=True)

            try:
                while not self._stop.is_set():
                    for message in device.poll(0.25):
                        self._handle(message)
                    idle_for = time.monotonic() - self._page_touched
                    if self._page is not None and idle_for > MENU_TIMEOUT:
                        self._page = None
                        self._paint(force=True)
            except OSError:
                log.info("Codex Micro disconnected")
            finally:
                with self._lock:
                    self._device = None
                device.close()

        with self._lock:
            if self._device is not None:
                self._device.blank()

    # ---------- input ----------

    def _handle(self, message: dict) -> None:
        if message.get("m") != "v.oai.hid":
            return  # joystick (v.oai.rad) and replies are not used here
        params = message.get("p") or {}
        key, act = params.get("k"), params.get("act")

        if key == self.settings.trigger_key:
            if act == ACT_PRESS:
                self.on_trigger()
            elif act == ACT_RELEASE:
                self.on_release()
            return

        if not self.settings.menu:
            return

        if key in (ENCODER_CW, ENCODER_CCW) and act == ACT_ROTATE:
            self._cycle_page(1 if key == ENCODER_CW else -1)
        elif key == ENCODER_CLICK and act == ACT_PRESS:
            self._page = None  # confirm / close
            self._paint(force=True)
        elif key in AGENT_KEYS and act == ACT_PRESS:
            self._select(AGENT_KEYS.index(key))

    def _cycle_page(self, direction: int) -> None:
        if self._state != "idle":
            return  # never open the menu mid-recording
        current = PAGES.index(self._page) if self._page in PAGES else -1
        self._page = PAGES[(current + direction) % len(PAGES)]
        self._page_touched = time.monotonic()
        self._paint(force=True)

    def _select(self, slot: int) -> None:
        if self._state != "idle":
            return
        self._page_touched = time.monotonic()
        page = self._page or PAGE_MODEL
        if page == PAGE_MODEL:
            if slot < self._model_count:
                self.on_pick_model(slot)
        elif page == PAGE_MODE:
            self.on_set_mode("toggle" if slot == 0 else "hold")
        elif page == PAGE_DELIVERY:
            self.on_set_delivery("clipboard" if slot == 0 else "type")

    # ---------- output ----------

    def _colors(self) -> list[int]:
        s = self.settings
        if not s.led_feedback:
            return [0] * SLOT_COUNT

        # A live recording or transcription outranks the menu entirely.
        if not self._enabled:
            return [_scale(s.color_disabled, DIM)] * SLOT_COUNT
        if self._state == "recording":
            return [s.color_recording] * SLOT_COUNT
        if self._state == "busy":
            return [s.color_busy] * SLOT_COUNT
        if self._model_status in ("loading", "unloading"):
            return [_scale(s.color_loading, 0.5)] * SLOT_COUNT

        page = self._page or PAGE_MODEL
        if page == PAGE_MODEL:
            accent = s.color_loading if self._page else s.color_idle
            return [
                accent if i == self._model_index
                else (_scale(accent, DIM) if i < self._model_count else 0)
                for i in range(SLOT_COUNT)
            ]
        if page == PAGE_MODE:
            on = self._mode == "toggle"
            return [
                s.color_option_a if on else _scale(s.color_option_a, DIM),
                0, 0, 0, 0,
                _scale(s.color_option_b, DIM) if on else s.color_option_b,
            ]
        # PAGE_DELIVERY
        on = self._delivery == "clipboard"
        return [
            s.color_option_a if on else _scale(s.color_option_a, DIM),
            0, 0, 0, 0,
            _scale(s.color_option_b, DIM) if on else s.color_option_b,
        ]

    def _paint(self, force: bool = False) -> None:
        with self._lock:
            device = self._device
        if device is None:
            return
        if force:
            device._last_frame = None
        try:
            device.set_slots(self._colors(), self.settings.brightness)
        except OSError:
            pass  # the reader loop will notice the disconnect
