"""Deliver text: clipboard via xclip, or typed/pasted into the focused window."""

import logging
import subprocess
import time

from pynput.keyboard import Controller, Key

log = logging.getLogger(__name__)

_MODIFIERS = [
    Key.ctrl, Key.ctrl_l, Key.ctrl_r,
    Key.alt, Key.alt_l, Key.alt_r,
    Key.shift, Key.shift_l, Key.shift_r,
    Key.cmd,
]


def copy(text: str) -> None:
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=text.encode(),
        check=True,
        timeout=5,
    )


def read() -> str | None:
    """Current clipboard text, or None if empty/non-text."""
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-o", "-t", "UTF8_STRING"],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode(errors="replace")


def type_into_focus(text: str) -> None:
    """Deliver text into whatever has keyboard focus, leaving the clipboard as-is.

    ASCII is typed directly via XTEST (reliable, works in terminals). Text with
    non-ASCII characters is pasted via Ctrl+V with clipboard save/restore,
    because X11 keymap remapping for unmapped keysyms races the receiving app's
    keymap cache and garbles characters.
    """
    kb = Controller()
    # a still-held hotkey modifier would turn typed keys into shortcuts
    for mod in _MODIFIERS:
        kb.release(mod)

    if text.isascii():
        for ch in text:
            if ch == "\n":
                kb.tap(Key.enter)
            elif ch == "\t":
                kb.tap(Key.tab)
            else:
                kb.tap(ch)
            time.sleep(0.004)
        return

    previous = read()
    copy(text)
    time.sleep(0.05)
    kb.press(Key.ctrl)
    kb.tap("v")
    kb.release(Key.ctrl)
    time.sleep(0.3)  # let the target app fetch the selection before restoring
    if previous is not None:
        copy(previous)
    else:
        log.info("previous clipboard was empty/non-text; not restored")
