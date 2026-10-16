"""Shared settings, AutoKey bindings and transactional desktop configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_ROOT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_DIR = CONFIG_ROOT / "talk-and-path"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "talk-and-path"
DEFAULT_CONFIG = {
    "model": "small",
    "device": "cpu",
    "compute_type": "int8",
    "cpu_threads": 4,
    "language": None,
    "max_seconds": 180,
    "auto_paste": True,
    "copy_path_shortcut": "ctrl+alt+shift+p",
    "focus_codex_shortcut": "ctrl+alt+shift+i",
    "hotwords_file": None,
    "hotwords_priority": [],
    "input_device": None,
    "notifications": True,
    "path_notifications": True,
    "start_at_login": True,
    "hotkeys": {"toggle": "F8", "path": "F9", "cancel": "Shift+F8"},
}
ACTIONS = {
    "toggle": "Dictation - Start or finish",
    "path": "Dictation - Insert hovered path",
    "cancel": "Dictation - Cancel",
}
MODIFIERS = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "super": "<super>"}
NAMED_KEYS = {
    "space": ("<space>", "space", "Space"),
    "escape": ("<escape>", "Escape", "Escape"),
    "return": ("<enter>", "Return", "Return"),
    "tab": ("<tab>", "Tab", "Tab"),
    "insert": ("<insert>", "Insert", "Insert"),
    "delete": ("<delete>", "Delete", "Delete"),
    "home": ("<home>", "Home", "Home"),
    "end": ("<end>", "End", "End"),
    "pageup": ("<page_up>", "Page_Up", "PageUp"),
    "pagedown": ("<page_down>", "Page_Down", "PageDown"),
    **{key: (f"<{key}>", key.title(), key.title()) for key in ("up", "down", "left", "right")},
}


def parse_shortcut(value):
    if not isinstance(value, str):
        raise ValueError("A shortcut must be text such as F8 or Ctrl+F8")
    parts = [part.strip().lower() for part in value.split("+")]
    if not parts or any(not part for part in parts):
        raise ValueError("Use a shortcut such as F8 or Ctrl+Shift+F8")
    aliases = {"control": "ctrl", "meta": "super", "win": "super", "enter": "return"}
    parts = [aliases.get(part, part) for part in parts]
    mods, key = parts[:-1], parts[-1]
    if any(mod not in MODIFIERS for mod in mods) or len(mods) != len(set(mods)):
        raise ValueError("Shortcut modifiers must be Ctrl, Alt, Shift or Super")
    if re.fullmatch(r"f(?:[1-9]|1[0-2])", key):
        autokey, xkey, label = f"<{key}>", key.upper(), key.upper()
    elif key in NAMED_KEYS:
        autokey, xkey, label = NAMED_KEYS[key]
    elif re.fullmatch(r"[a-z0-9]", key) and mods:
        autokey, xkey, label = key, key, key.upper()
    else:
        raise ValueError("Use F1–F12, a named key, or a letter/number with a modifier")
    canonical = "+".join([mod.title() for mod in MODIFIERS if mod in mods] + [label])
    return {
        "label": canonical,
        "hotKey": autokey,
        "modifiers": sorted(MODIFIERS[mod] for mod in mods),
        "xkey": xkey,
    }


def validate_config(values):
    if not isinstance(values, dict):
        raise ValueError("Settings must be an object")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(values)
    if not isinstance(values.get("hotkeys", {}), dict):
        raise ValueError("Shortcuts must be an object with toggle, path and cancel entries")
    config["hotkeys"] = {**DEFAULT_CONFIG["hotkeys"], **values.get("hotkeys", {})}
    for key in ("model", "device", "compute_type", "copy_path_shortcut", "focus_codex_shortcut"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    for key, maximum in (("max_seconds", 3600), ("cpu_threads", 64)):
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f"{key} must be between 1 and {maximum}")
    for key in ("auto_paste", "notifications", "path_notifications", "start_at_login"):
        if type(config[key]) is not bool:
            raise ValueError(f"{key} must be enabled or disabled")
    language = config["language"]
    if language is not None and (
        not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)
    ):
        raise ValueError("Language must be automatic or a language code such as en")
    for key in ("hotwords_file", "input_device"):
        if config[key] is not None and (
            not isinstance(config[key], str) or not config[key].strip()
        ):
            raise ValueError(f"{key} must be text or null")
    priorities = config["hotwords_priority"]
    if not isinstance(priorities, list) or any(
        not isinstance(term, str) or "\n" in term for term in priorities
    ):
        raise ValueError("Vocabulary priorities must be a list of terms")
    config["hotwords_priority"] = list(
        dict.fromkeys(term.strip() for term in priorities if term.strip())
    )
    combinations = set()
    for command in ACTIONS:
        parsed = parse_shortcut(config["hotkeys"][command])
        combination = (parsed["hotKey"], tuple(parsed["modifiers"]))
        if combination in combinations:
            raise ValueError("Each dictation action needs a different shortcut")
        combinations.add(combination)
        config["hotkeys"][command] = parsed["label"]
    return config


def load_config():
    path = CONFIG_DIR / "config.json"
    return validate_config(json.loads(path.read_text()) if path.exists() else {})


def glossary_path(config):
    filename = config.get("hotwords_file")
    if not filename:
        return None
    path = Path(filename).expanduser()
    return path if path.is_absolute() else CONFIG_DIR / path


def vocabulary_terms(text):
    return list(
        dict.fromkeys(
            line.strip()
            for line in text.lstrip("\ufeff").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    )


def check_hotkey_conflicts(config, root=None):
    root = root or CONFIG_ROOT / "autokey/data"
    if not root.is_dir():
        raise ValueError(f"AutoKey data directory not found: {root}")
    destination = root / "Talk and Path"
    wanted = {
        command: parse_shortcut(value)
        for command, value in config["hotkeys"].items()
        if command in ACTIONS
    }
    for metadata in root.rglob(".*.json"):
        if destination in metadata.parents:
            continue
        try:
            entry = json.loads(metadata.read_text())
        except (OSError, ValueError):
            continue
        if 3 not in entry.get("modes", []):
            continue
        hotkey = entry.get("hotkey", {})
        for shortcut in wanted.values():
            if (
                hotkey.get("hotKey") == shortcut["hotKey"]
                and sorted(hotkey.get("modifiers", [])) == shortcut["modifiers"]
            ):
                name = entry.get("description", entry.get("title", metadata.stem))
                raise ValueError(
                    f"{shortcut['label']} is already assigned to AutoKey item “{name}”"
                )


def autokey_script(command, xkey):
    # Keep repeat suppression local to this action. Other AutoKey scripts keep working.
    invocation = [str(ROOT / ".venv-dictation/bin/python"), str(ROOT / "talk_and_path.py"), command]
    return f"""# Managed by Talk and Path settings.
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
            code = connection.keysym_to_keycode(XK.string_to_keysym({xkey!r}))
            while code and connection.query_keymap()[code // 8] & (1 << (code % 8)):
                time.sleep(0.025)
        finally:
            connection.close()
"""


def autokey_files(config, root=None):
    root = root or CONFIG_ROOT / "autokey/data"
    check_hotkey_conflicts(config, root)
    destination = root / "Talk and Path"
    folder = {
        "type": "folder",
        "title": "Talk and Path",
        "modes": [],
        "usageCount": 0,
        "showInTrayMenu": True,
        "hotkey": {"hotKey": None, "modifiers": []},
        "filter": {"regex": None, "isRecursive": False},
        "abbreviation": {
            "wordChars": "[\\w]",
            "abbreviations": [],
            "immediate": False,
            "ignoreCase": False,
            "backspace": True,
            "triggerInside": False,
        },
    }
    files = {destination / ".folder.json": json.dumps(folder, indent=4).encode()}
    for command, title in ACTIONS.items():
        shortcut = parse_shortcut(config["hotkeys"][command])
        metadata = {
            **folder,
            "type": "script",
            "description": title,
            "modes": [3],
            "omitTrigger": False,
            "prompt": False,
            "store": {},
            "hotkey": {"hotKey": shortcut["hotKey"], "modifiers": shortcut["modifiers"]},
        }
        metadata.pop("title", None)
        files[destination / f".{title}.json"] = json.dumps(metadata, indent=4).encode()
        files[destination / f"{title}.py"] = autokey_script(command, shortcut["xkey"]).encode()
    bridge_title = "Dictation - Reload bindings"
    bridge_metadata = {
        **folder,
        "type": "script",
        "description": bridge_title,
        "modes": [],
        "omitTrigger": False,
        "prompt": False,
        "store": {},
    }
    bridge_metadata.pop("title", None)
    files[destination / f".{bridge_title}.json"] = json.dumps(bridge_metadata, indent=4).encode()
    files[destination / f"{bridge_title}.py"] = rebind_script().encode()
    return files


def rebind_script():
    return f"""# Refresh only this integration; do not restart or pause AutoKey.
import copy
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, {str(ROOT)!r})
from dictation_settings import ACTIONS, atomic_write, parse_shortcut
runtime_root = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/talk-and-path-{{os.getuid()}}")
runtime = Path(runtime_root) / "talk-and-path"
request_file = runtime / "autokey-rebind-request.json"
if request_file.exists():
    request = json.loads(request_file.read_text())
    result = {{"nonce": request["nonce"], "ok": False}}
    try:
        manager = engine.configManager
        folder = engine.get_folder("Talk and Path")
        with manager.lock:
            for command, title in ACTIONS.items():
                item = next(item for item in folder.items if item.description == title)
                old = copy.copy(item)
                shortcut = parse_shortcut(request["old"][command])
                old.hotKey, old.modifiers = shortcut["hotKey"], shortcut["modifiers"]
                manager.app.hotkey_removed(old)
                item.load(folder)
                manager.app.hotkey_created(item)
        manager.config_altered(False)
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    atomic_write(runtime / "autokey-rebind-result.json", json.dumps(result).encode())
"""


def refresh_autokey(old, new, timeout=4):
    if old["hotkeys"] == new["hotkeys"]:
        return
    runtime = (
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/talk-and-path-{os.getuid()}"))
        / "talk-and-path"
    )
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    nonce = uuid.uuid4().hex
    atomic_write(
        runtime / "autokey-rebind-request.json",
        json.dumps({"nonce": nonce, "old": old["hotkeys"], "new": new["hotkeys"]}).encode(),
    )
    subprocess.run(
        ["autokey-run", "-s", "Dictation - Reload bindings"],
        check=True,
        timeout=3,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + timeout
    path = runtime / "autokey-rebind-result.json"
    while time.monotonic() < deadline:
        if path.exists():
            result = json.loads(path.read_text())
            if result.get("nonce") == nonce:
                if not result["ok"]:
                    raise RuntimeError(
                        "AutoKey could not apply shortcuts: " + result.get("error", "unknown error")
                    )
                return
        time.sleep(0.04)
    raise RuntimeError(
        "AutoKey did not confirm the shortcut change; the previous settings were restored."
    )


def startup_entry(enabled):
    launcher = Path.home() / ".local/bin/talk-and-path-session"
    return f'''[Desktop Entry]
Type=Application
Name=Talk and Path
Comment=Local voice dictation and panel controls
Exec="{launcher}"
Terminal=false
Hidden={str(not enabled).lower()}
X-GNOME-Autostart-enabled={str(enabled).lower()}
'''.encode()


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".talk-path-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def commit_files(files, backup_root=None, after_write=None):
    """Restore every modified file if any write fails, including AutoKey metadata."""
    backup_root = backup_root or STATE_DIR / "backups"
    snapshots = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        for path in files
    }
    changed = [
        path
        for path, data in files.items()
        if snapshots[path] is None or snapshots[path][0] != data
    ]
    if not changed:
        return
    backup = backup_root / datetime.now().strftime("%Y%m%d-%H%M%S-%f-settings")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = {}
    for index, path in enumerate(changed):
        snapshot = snapshots[path]
        manifest[str(path)] = str(index) if snapshot else None
        if snapshot:
            atomic_write(backup / str(index), snapshot[0], snapshot[1])
    atomic_write(backup / "manifest.json", json.dumps(manifest, indent=2).encode())
    written = []
    try:
        for path in changed:
            atomic_write(path, files[path], snapshots[path][1] if snapshots[path] else 0o600)
            written.append(path)
        if after_write:
            after_write()
    except Exception:
        for path in reversed(written):
            snapshot = snapshots[path]
            if snapshot:
                atomic_write(path, *snapshot)
            else:
                path.unlink(missing_ok=True)
        raise


def configuration_files(config, glossary_text=None):
    config = validate_config(config)
    files = {}
    if glossary_text is not None:
        if not isinstance(glossary_text, str) or len(glossary_text.encode()) > 128 * 1024:
            raise ValueError("Glossary must be text under 128 KB")
        path = glossary_path(config)
        if path:
            files[path] = glossary_text.encode("utf-8")
    files[CONFIG_DIR / "config.json"] = (
        json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    files.update(autokey_files(config))
    files[CONFIG_ROOT / "autostart/talk-and-path.desktop"] = startup_entry(config["start_at_login"])
    return files


def save_configuration(config, glossary_text=None, expected_revision=None):
    config = validate_config(config)
    if glossary_text is not None and expected_revision is not None:
        path = glossary_path(config)
        if path:
            current = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            if hashlib.sha256(current.encode()).hexdigest() != expected_revision:
                raise ValueError(
                    "The glossary changed outside Settings. "
                    "Load the file again before saving vocabulary edits."
                )
    old = load_config()
    try:
        commit_files(
            configuration_files(config, glossary_text),
            after_write=lambda: refresh_autokey(old, config),
        )
    except Exception:
        # The file transaction has rolled back; restore any grabs already updated.
        try:
            refresh_autokey(config, old)
        except Exception:
            pass
        raise
    return config
