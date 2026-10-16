#!/usr/bin/env python3
"""Install the local AutoKey integration without replacing existing shortcuts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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
        if char == "," and structural[index + 1 :].lstrip().startswith(("}", "]")):
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
    before = structural[start + 1 : end].rstrip()
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
    sys.path.insert(0, str(ROOT))
    from dictation_settings import autokey_script as generate

    return generate(command, key)


def install_autokey():
    sys.path.insert(0, str(ROOT))
    from dictation_settings import autokey_files, commit_files, load_config

    commit_files(autokey_files(load_config(), CONFIG / "autokey/data"))


def install_service():
    executable = ROOT / ".venv-dictation/bin/python"
    unit = CONFIG / "systemd/user/talk-and-path.service"
    unit.parent.mkdir(parents=True, exist_ok=True)

    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    unit.write_text(f"""[Unit]
Description=Local Whisper dictation with hovered file paths
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={quote(executable)} {quote(ROOT / "talk_and_path.py")} serve
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
UMask=0077
""")
    tray_unit = CONFIG / "systemd/user/talk-and-path-tray.service"
    tray_unit.write_text(f"""[Unit]
Description=Talk and Path MATE panel icon
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {quote(ROOT / "tray_app.py")} --background
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
UMask=0077
""")
    launcher = Path.home() / ".local/bin/talk-and-path-session"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("""#!/bin/sh
# Import the actual desktop environment after X11 login (also works on MATE).
systemctl --user import-environment DISPLAY XAUTHORITY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP
exec systemctl --user start talk-and-path.service talk-and-path-tray.service
""")
    launcher.chmod(0o755)
    from dictation_settings import load_config, startup_entry

    autostart = CONFIG / "autostart/talk-and-path.desktop"
    autostart.parent.mkdir(parents=True, exist_ok=True)
    autostart.write_bytes(startup_entry(load_config()["start_at_login"]))
    control = launcher.parent / "talk-and-path"
    control.write_text(f"""#!/bin/sh
systemctl --user start talk-and-path-tray.service
exec /usr/bin/python3 {quote(ROOT / "tray_app.py")} --settings
""")
    control.chmod(0o755)
    applications = Path.home() / ".local/share/applications"
    applications.mkdir(parents=True, exist_ok=True)
    (applications / "talk-and-path.desktop").write_text(f'''[Desktop Entry]
Type=Application
Name=Talk and Path
Comment=Local voice dictation settings
Exec="{control}"
Icon={ROOT / "assets/tray/talk-and-path-ready.svg"}
Terminal=false
Categories=Utility;Accessibility;
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
            subprocess.run(
                [uv, "venv", "--python", "3.10", str(ROOT / ".venv-dictation")], check=True
            )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(ROOT / ".venv-dictation/bin/python"),
                "-r",
                str(ROOT / "requirements-dictation.txt"),
            ],
            check=True,
        )
    config_dir = CONFIG / "talk-and-path"
    config_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from dictation_settings import atomic_write, load_config

    config_path = config_dir / "config.json"
    if config_path.exists():
        backup(config_path)
    atomic_write(config_path, (json.dumps(load_config(), indent=2) + "\n").encode())
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
    print("Installed Talk and Path panel icon, settings and AutoKey integration.")


if __name__ == "__main__":
    main()
