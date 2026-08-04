"""Global hotkey handling (pynput, X11): keyboard combos and mouse buttons.

Spec syntax:
  - keyboard: pynput syntax, e.g. "<ctrl>+<alt>+<space>", "<f9>"
  - mouse button: "mouse:button9", "mouse:middle"
  - modifiers + mouse button: "<ctrl>+mouse:button8"
"""

import logging
import threading
from collections.abc import Callable

from pynput import keyboard, mouse

log = logging.getLogger(__name__)

# X11 auto-repeat emits release/press pairs while a key is held; a release only
# counts as real if no matching press follows within this window.
REPEAT_DEBOUNCE_S = 0.15

MODIFIER_NAMES = {
    keyboard.Key.ctrl: "<ctrl>",
    keyboard.Key.ctrl_l: "<ctrl>",
    keyboard.Key.ctrl_r: "<ctrl>",
    keyboard.Key.alt: "<alt>",
    keyboard.Key.alt_l: "<alt>",
    keyboard.Key.alt_r: "<alt>",
    keyboard.Key.alt_gr: "<alt>",
    keyboard.Key.shift: "<shift>",
    keyboard.Key.shift_l: "<shift>",
    keyboard.Key.shift_r: "<shift>",
    keyboard.Key.cmd: "<cmd>",
    keyboard.Key.cmd_l: "<cmd>",
    keyboard.Key.cmd_r: "<cmd>",
}


def parse_mouse_spec(spec: str) -> tuple[frozenset[str], mouse.Button] | None:
    """If spec involves a mouse button, return (required modifiers, button)."""
    if "mouse:" not in spec:
        return None
    mods: set[str] = set()
    button = None
    for part in (p.strip() for p in spec.split("+")):
        if part.startswith("mouse:"):
            name = part[len("mouse:"):]
            try:
                button = mouse.Button[name]
            except KeyError:
                raise ValueError(f"unknown mouse button: {name!r}") from None
        elif part:
            mods.add(part)
    if button is None:
        raise ValueError(f"bad mouse hotkey spec: {spec!r}")
    return frozenset(mods), button


class HotkeyListener:
    """Fires on_activate when the combo is pressed and on_deactivate when it is
    released again — the app maps these to toggle or push-to-talk behavior."""

    def __init__(
        self,
        combo: str,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None] | None = None,
    ) -> None:
        self._combo = combo
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate or (lambda: None)
        self._kb: keyboard.Listener | None = None
        self._mouse: mouse.Listener | None = None
        self._held_mods: set[str] = set()
        # keyboard-combo state
        self._keys: set = set()
        self._pressed: set = set()
        self._active = False
        self._pending: dict = {}  # key -> debounce Timer for auto-repeat releases

    def start(self) -> None:
        if self._kb is not None or self._mouse is not None:
            return
        parsed = parse_mouse_spec(self._combo)  # raises ValueError on bad spec
        self._pressed = set()
        self._active = False
        self._pending = {}
        if parsed is None:
            self._keys = set(keyboard.HotKey.parse(self._combo))
            self._kb = keyboard.Listener(
                on_press=self._on_kb_press, on_release=self._on_kb_release
            )
            self._kb.start()
        else:
            required, button = parsed
            self._held_mods = set()
            if required:
                self._kb = keyboard.Listener(
                    on_press=self._on_mod_press, on_release=self._on_mod_release
                )
                self._kb.start()

            def on_click(x, y, b, pressed):
                if b != button:
                    return
                if pressed and not self._active and required <= self._held_mods:
                    self._active = True
                    self._on_activate()
                elif not pressed and self._active:
                    self._active = False
                    self._on_deactivate()

            self._mouse = mouse.Listener(on_click=on_click)
            self._mouse.start()
        log.info("hotkey listener started: %s", self._combo)

    # -- keyboard combos --

    def _canonical(self, key):
        k = self._kb.canonical(key) if self._kb else key
        char = getattr(k, "char", None)
        if char and len(char) == 1 and 1 <= ord(char) <= 26:
            # Ctrl+letter arrives as a control character
            return keyboard.KeyCode.from_char(chr(ord(char) + 96))
        return k

    def _on_kb_press(self, key) -> None:
        k = self._canonical(key)
        if k not in self._keys:
            return
        timer = self._pending.pop(k, None)
        if timer is not None:
            timer.cancel()  # release was auto-repeat, not real
        self._pressed.add(k)
        if not self._active and self._pressed == self._keys:
            self._active = True
            self._on_activate()

    def _on_kb_release(self, key) -> None:
        k = self._canonical(key)
        if k not in self._keys or k not in self._pressed:
            return
        timer = threading.Timer(REPEAT_DEBOUNCE_S, self._kb_release_final, args=(k,))
        self._pending[k] = timer
        timer.start()

    def _kb_release_final(self, k) -> None:
        self._pending.pop(k, None)
        self._pressed.discard(k)
        if self._active:
            self._active = False
            self._on_deactivate()

    # -- modifiers for mouse combos --

    def _on_mod_press(self, key) -> None:
        name = MODIFIER_NAMES.get(key)
        if name:
            self._held_mods.add(name)

    def _on_mod_release(self, key) -> None:
        name = MODIFIER_NAMES.get(key)
        if name:
            self._held_mods.discard(name)

    def stop(self) -> None:
        kb, self._kb = self._kb, None
        ms, self._mouse = self._mouse, None
        for timer in self._pending.values():
            timer.cancel()
        self._pending = {}
        if kb is not None:
            kb.stop()
        if ms is not None:
            ms.stop()
        if kb or ms:
            log.info("hotkey listener stopped")

    def restart(self, combo: str) -> None:
        self.stop()
        self._combo = combo
        self.start()


class HotkeyCapture:
    """One-shot capture of the next key/mouse combo.

    Calls on_captured(spec) with the spec string, or None if cancelled with
    Esc. Left click is ignored (it would make an unusable hotkey); modifiers
    held at the moment of the press become part of the combo.
    """

    def __init__(self, on_captured: Callable[[str | None], None]) -> None:
        self._on_captured = on_captured
        self._held: set[str] = set()
        self._done = False
        self._kb: keyboard.Listener | None = None
        self._mouse: mouse.Listener | None = None

    def start(self) -> None:
        self._kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._mouse = mouse.Listener(on_click=self._on_click)
        self._kb.start()
        self._mouse.start()

    def cancel(self) -> None:
        self._finish(None)

    def _finish(self, spec: str | None) -> None:
        if self._done:
            return
        self._done = True
        kb, self._kb = self._kb, None
        ms, self._mouse = self._mouse, None
        if kb is not None:
            kb.stop()
        if ms is not None:
            ms.stop()
        log.info("hotkey capture finished: %s", spec)
        self._on_captured(spec)

    def _with_mods(self, key_spec: str) -> str:
        return "+".join(sorted(self._held) + [key_spec]) if self._held else key_spec

    def _on_press(self, key) -> None:
        mod = MODIFIER_NAMES.get(key)
        if mod:
            self._held.add(mod)
            return
        if key == keyboard.Key.esc:
            self._finish(None)
            return
        spec = self._key_spec(key)
        if spec:
            self._finish(self._with_mods(spec))

    def _on_release(self, key) -> None:
        mod = MODIFIER_NAMES.get(key)
        if mod:
            self._held.discard(mod)

    def _key_spec(self, key) -> str | None:
        if isinstance(key, keyboard.Key):
            return f"<{key.name}>"
        # normalize away modifier effects (e.g. Ctrl+A arrives as '\x01')
        canonical = self._kb.canonical(key) if self._kb else key
        char = getattr(canonical, "char", None)
        if char and char.isprintable():
            return char.lower()
        vk = getattr(key, "vk", None)
        return f"<{vk}>" if vk else None

    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed or button == mouse.Button.left:
            return
        self._finish(self._with_mods(f"mouse:{button.name}"))
