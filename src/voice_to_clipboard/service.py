"""Install/uninstall a systemd user service for the tray app."""

import subprocess
import sys
from pathlib import Path

UNIT_NAME = "voice-to-clipboard.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
AUTOSTART_DESKTOP = Path.home() / ".config" / "autostart" / "voice-to-clipboard.desktop"

UNIT_TEMPLATE = """\
[Unit]
Description=voice-to-clipboard dictation tray service
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_path}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""


def install() -> int:
    exec_path = Path(sys.argv[0]).resolve()
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / UNIT_NAME).write_text(UNIT_TEMPLATE.format(exec_path=exec_path))
    if AUTOSTART_DESKTOP.exists():
        AUTOSTART_DESKTOP.unlink()  # avoid double-start alongside the service
        print(f"removed legacy autostart entry {AUTOSTART_DESKTOP}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)
    print(f"installed and started {UNIT_NAME} (ExecStart={exec_path})")
    print("manage it with: systemctl --user status|restart|stop voice-to-clipboard")
    print("note: if a manually-started instance is still running, quit it from the")
    print("tray first, then: systemctl --user restart voice-to-clipboard")
    return 0


def uninstall() -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    (UNIT_DIR / UNIT_NAME).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"removed {UNIT_NAME}")
    return 0
