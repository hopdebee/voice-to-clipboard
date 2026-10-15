#!/usr/bin/env python3
"""Install the local AutoKey integration without replacing existing shortcuts."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "talk-and-path"
BINDINGS = [
    {"key": "ctrl+alt+shift+p", "command": "copyFilePath", "when": "filesExplorerFocus"},
    {"key": "ctrl+alt+shift+i", "command": "chatgpt.openSidebar"},
]


def backup(path):
    if path.exists():
        target = STATE / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f") / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def install_editor_bindings(path):
    """Append to the JSONC array, retaining comments and all existing bindings."""
    original = path.read_text() if path.exists() else "[]\n"
    # Mask comments and strings while retaining offsets, to find the closing array.
    masked = list(original)
    clean = list(original)
    index = 0
    while index < len(original):
        comment = False
        if original.startswith("//", index):
            comment = True
            end = original.find("\n", index)
            end = len(original) if end < 0 else end
        elif original.startswith("/*", index):
            comment = True
            end = original.find("*/", index + 2)
            if end < 0:
                raise ValueError(f"Unterminated comment in {path}")
            end += 2
        elif original[index] == '"':
            end = index + 1
            while end < len(original):
                if original[end] == "\\":
                    end += 2
                elif original[end] == '"':
                    end += 1
                    break
                else:
                    end += 1
        else:
            index += 1
            continue
        masked[index:end] = " " * (end - index)
        if comment:
            clean[index:end] = " " * (end - index)
        index = end
    structural = "".join(masked)
    start, end = structural.find("["), structural.rfind("]")
    if start < 0 or end < start:
        raise ValueError(f"Expected a JSONC array in {path}")
    # Remove trailing commas only outside strings; JSONC values can contain ',]'.
    for index, char in enumerate(structural):
        if char == "," and structural[index + 1:].lstrip().startswith(("}", "]")):
            clean[index] = " "
    parsed = json.loads("".join(clean))
    additions = []
    for binding in BINDINGS:
        matches = [item for item in parsed if item.get("key") == binding["key"]]
        legacy = {**binding, "command": "copyRelativeFilePath"}
        migration = binding["command"] == "copyFilePath" and matches == [legacy]
        if matches and binding not in matches and not migration:
            raise ValueError(f"Shortcut {binding['key']} already has another action in {path}")
        if binding not in matches:
            additions.append(binding)
    if not additions:
        return
    before = structural[start + 1:end].rstrip()
    inserted = ",\n".join(json.dumps(item, indent=4) for item in additions)
    prefix = original[:end]
    if before and not before.endswith(","):
        # Insert after the last real token, before any trailing line comments.
        position = len(structural[:end].rstrip())
        prefix = prefix[:position] + "," + prefix[position:]
    result = prefix + "\n    // Talk and Path dictation\n" + inserted + "\n" + original[end:]
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result)


def autokey_script(command, key):
    # Lock until physical key release: X11 auto-repeat must not toggle recording.
    invocation = [str(ROOT / ".venv-dictation/bin/python"), str(ROOT / "talk_and_path.py"), command]
    return f'''# Installed by {ROOT}/scripts/install_dictation.py
import fcntl
import os
from pathlib import Path
import subprocess
import time
from Xlib import display, XK

lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "talk-and-path-autokey-{command}.lock"
with lock_path.open("w") as lock_file:
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        subprocess.run({invocation!r}, timeout=10, check=False)
        connection = display.Display()
        try:
            code = connection.keysym_to_keycode(XK.string_to_keysym({key!r}))
            while connection.query_keymap()[code // 8] & (1 << (code % 8)):
                time.sleep(0.025)
        finally:
            connection.close()
'''


def install_autokey():
    root = CONFIG / "autokey/data"
    if not root.is_dir():
        raise RuntimeError(f"AutoKey data directory not found: {root}")
    destination = root / "Talk and Path"
    actions = [("Dictation - Start or finish", "toggle", "<f8>", [], "F8"),
               ("Dictation - Insert hovered path", "path", "<f9>", [], "F9"),
               ("Dictation - Cancel", "cancel", "<f8>", ["<shift>"], "F8")]
    for metadata in root.rglob(".*.json"):
        if destination in metadata.parents:
            continue
        entry = json.loads(metadata.read_text())
        hotkey = entry.get("hotkey", {})
        for _, _, key, modifiers, _ in actions:
            if 3 in entry.get("modes", []) and hotkey.get("hotKey") == key and sorted(hotkey.get("modifiers", [])) == sorted(modifiers):
                raise RuntimeError(f"AutoKey shortcut already used by {metadata}")
    destination.mkdir(exist_ok=True)
    folder = {"type": "folder", "title": "Talk and Path", "modes": [], "usageCount": 0,
              "showInTrayMenu": True, "hotkey": {"hotKey": None, "modifiers": []},
              "filter": {"regex": None, "isRecursive": False},
              "abbreviation": {"wordChars": "[\\w]", "abbreviations": [], "immediate": False,
                               "ignoreCase": False, "backspace": True, "triggerInside": False}}
    (destination / ".folder.json").write_text(json.dumps(folder, indent=4))
    for title, command, key, modifiers, xkey in actions:
        metadata = {**folder, "type": "script", "description": title, "modes": [3],
                    "omitTrigger": False, "prompt": False, "store": {},
                    "hotkey": {"hotKey": key, "modifiers": modifiers}}
        metadata.pop("title", None)
        # Write metadata first; AutoKey watches the subsequent .py create event.
        (destination / f".{title}.json").write_text(json.dumps(metadata, indent=4))
        (destination / f"{title}.py").write_text(autokey_script(command, xkey))


def install_service():
    executable = ROOT / ".venv-dictation/bin/python"
    unit = CONFIG / "systemd/user/talk-and-path.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    quote = lambda value: '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    unit.write_text(f'''[Unit]
Description=Local Whisper dictation with hovered file paths
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={quote(executable)} {quote(ROOT / 'talk_and_path.py')} serve
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
UMask=0077
''')
    launcher = Path.home() / ".local/bin/talk-and-path-session"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('''#!/bin/sh
# Import the actual desktop environment after X11 login (also works on MATE).
systemctl --user import-environment DISPLAY XAUTHORITY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP
exec systemctl --user start talk-and-path.service
''')
    launcher.chmod(0o755)
    autostart = CONFIG / "autostart/talk-and-path.desktop"
    autostart.parent.mkdir(parents=True, exist_ok=True)
    autostart.write_text(f'''[Desktop Entry]
Type=Application
Name=Talk and Path dictation
Comment=Local voice dictation controlled by AutoKey F8 and F9
Exec="{launcher}"
Terminal=false
X-GNOME-Autostart-enabled=true
''')
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run([str(launcher)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-dependencies", action="store_true")
    args = parser.parse_args()
    if not args.skip_dependencies:
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("Install uv first: https://docs.astral.sh/uv/")
        if not (ROOT / ".venv-dictation/bin/python").exists():
            subprocess.run([uv, "venv", "--python", "3.10", str(ROOT / ".venv-dictation")], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(ROOT / ".venv-dictation/bin/python"),
                        "-r", str(ROOT / "requirements-dictation.txt")], check=True)
    config_dir = CONFIG / "talk-and-path"
    config_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from talk_and_path import DEFAULT_CONFIG
    config_path = config_dir / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    for editor in ("Code", "Code - Insiders"):
        user = CONFIG / editor / "User"
        if user.is_dir():
            install_editor_bindings(user / "keybindings.json")
            profiles = user / "profiles"
            if profiles.exists():
                for profile in profiles.iterdir():
                    if profile.is_dir():
                        install_editor_bindings(profile / "keybindings.json")
    install_autokey()
    install_service()
    print("Installed. F8: start/finish, F9: hovered Explorer path, Shift+F8: cancel.")


if __name__ == "__main__":
    main()
