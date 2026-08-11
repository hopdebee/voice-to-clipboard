"""Config handling: ~/.config/voice-to-clipboard/config.toml"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "voice-to-clipboard"
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_FAVORITES = {
    "qwen3-asr-1.7b": "qwen-asr:Qwen/Qwen3-ASR-1.7B-hf",
    "granite-4.1-2b": "granite-speech:ibm-granite/granite-speech-4.1-2b",
    "large-v3 (fw)": "faster-whisper:large-v3",
    "large-v3-turbo (fw)": "faster-whisper:deepdml/faster-whisper-large-v3-turbo-ct2",
    "distil-large-v3.5 (fw)": "faster-whisper:distil-large-v3.5",
    "large-v3-turbo (hf)": "transformers:openai/whisper-large-v3-turbo",
}


@dataclass
class CodexMicroSettings:
    """Work Louder Codex Micro (USB 303a:8360) support.

    Colors are packed 0xRRGGBB and default to the tray icon's state palette, so
    the keypad and the tray icon always agree on what the app is doing.
    """

    enabled: bool = True  # auto-detected; no device present is a silent no-op
    trigger_key: str = "ACT10"  # wide mic key; any AGxx/ACTxx keycode works
    trigger_delivery: str = "clipboard"  # "clipboard" or "type"
    led_feedback: bool = True
    menu: bool = True  # encoder cycles pages, agent keys select
    brightness: int = 255  # 0-255

    color_idle: int = 0x9B59B6  # purple   — matches the tray icon
    color_recording: int = 0xE93D3D  # red
    color_busy: int = 0xE9A13D  # orange
    color_disabled: int = 0x777777  # gray
    color_loading: int = 0x3D8CE9  # blue
    color_option_a: int = 0x2ECC71  # green   — first option on a menu page
    color_option_b: int = 0xE9A13D  # orange  — second option on a menu page


@dataclass
class Config:
    hotkey: str = "<ctrl>+<alt>+<space>"
    paste_hotkey: str = ""  # optional second hotkey: types result into the focused window
    mode: str = "toggle"  # "toggle" (press to start/stop) or "hold" (push-to-talk)
    backend: str = "qwen-asr"  # qwen-asr | granite-speech | faster-whisper | transformers
    model: str = "Qwen/Qwen3-ASR-1.7B-hf"
    language: str = ""  # empty = auto-detect
    device: str = "cuda"
    compute_type: str = "float16"  # faster-whisper only
    max_seconds: int = 120
    sounds: bool = True  # success/error result tones
    record_tone: bool = False  # 417 Hz om drone while recording
    favorites: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FAVORITES))
    codex_micro: CodexMicroSettings = field(default_factory=CodexMicroSettings)

    @property
    def backend_model(self) -> str:
        return f"{self.backend}:{self.model}"


def load() -> Config:
    if not CONFIG_PATH.exists():
        cfg = Config()
        save(cfg)
        return cfg
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    cfg = Config()
    for key in ("hotkey", "paste_hotkey", "backend", "model", "language", "device", "compute_type"):
        if key in raw:
            setattr(cfg, key, str(raw[key]))
    if raw.get("mode") in ("toggle", "hold"):
        cfg.mode = raw["mode"]
    if "max_seconds" in raw:
        cfg.max_seconds = int(raw["max_seconds"])
    if "sounds" in raw:
        cfg.sounds = bool(raw["sounds"])
    if "record_tone" in raw:
        cfg.record_tone = bool(raw["record_tone"])
    if isinstance(raw.get("models"), dict):
        cfg.favorites = {str(k): str(v) for k, v in raw["models"].items()}
    if isinstance(raw.get("codex_micro"), dict):
        cfg.codex_micro = _load_codex_micro(raw["codex_micro"])
    return cfg


def _load_codex_micro(raw: dict) -> CodexMicroSettings:
    cm = CodexMicroSettings()
    for key in ("enabled", "led_feedback", "menu"):
        if key in raw:
            setattr(cm, key, bool(raw[key]))
    if isinstance(raw.get("trigger_key"), str):
        cm.trigger_key = raw["trigger_key"].strip().upper()
    if raw.get("trigger_delivery") in ("clipboard", "type"):
        cm.trigger_delivery = raw["trigger_delivery"]
    if "brightness" in raw:
        cm.brightness = max(0, min(255, int(raw["brightness"])))
    for key in (
        "color_idle",
        "color_recording",
        "color_busy",
        "color_disabled",
        "color_loading",
        "color_option_a",
        "color_option_b",
    ):
        if key not in raw:
            continue
        value = raw[key]
        try:
            # Accept "#rrggbb" / "0xrrggbb" strings as well as TOML integers.
            if isinstance(value, str):
                parsed = int(value.strip().lstrip("#"), 16)
            else:
                parsed = int(value)
        except ValueError:
            continue
        setattr(cm, key, parsed & 0xFFFFFF)
    return cm


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    favorites = "\n".join(
        f'"{label}" = "{spec}"' for label, spec in cfg.favorites.items()
    )
    cm = cfg.codex_micro
    CONFIG_PATH.write_text(f'''# voice-to-clipboard configuration
# Hotkey: pynput syntax like "<ctrl>+<alt>+<space>" or "<f9>", a mouse button
# like "mouse:button9" or "mouse:middle", or a mix: "<ctrl>+mouse:button8".
# Easiest is the tray menu's "Set hotkey..." which captures whatever you press.
hotkey = "{cfg.hotkey}"

# Optional second hotkey (same syntax). Recordings triggered by it are TYPED
# into the currently focused text box instead of copied to the clipboard.
# Empty string disables it.
paste_hotkey = "{cfg.paste_hotkey}"

# mode: "toggle" = press to start, press again to stop.
#       "hold"   = push-to-talk: record while held, transcribe on release.
# Applies to both hotkeys.
mode = "{cfg.mode}"

# backend: "qwen-asr" (Qwen3-ASR family, best accuracy), "granite-speech"
# (IBM granite-speech family), "faster-whisper" (CTranslate2 Whisper, fastest
# Whisper), or "transformers" (any HF automatic-speech-recognition pipeline model)
backend = "{cfg.backend}"

# model: qwen-asr -> a Qwen3-ASR "-hf" repo id (e.g. Qwen/Qwen3-ASR-1.7B-hf).
# faster-whisper -> a size name (large-v3, distil-large-v3.5, ...) or a
# CTranslate2 repo id / local path. transformers -> any HF ASR repo id.
model = "{cfg.model}"

# language: ISO code like "en", or "" for auto-detect
language = "{cfg.language}"

device = "{cfg.device}"
compute_type = "{cfg.compute_type}"   # faster-whisper only: float16, int8_float16, int8
max_seconds = {cfg.max_seconds}       # auto-stop recording after this long
sounds = {str(cfg.sounds).lower()}         # success/error result tones
record_tone = {str(cfg.record_tone).lower()}   # 417 Hz om drone while recording

# Favorites shown in the tray menu. Format: label = "backend:model"
# Labels are free-form — keep them short so the menu doesn't get cut off.
# The first six also map to the Codex Micro's six agent keys, in order.
[models]
{favorites}

# Work Louder x OpenAI Codex Micro (USB 303a:8360). Ignored when no device is
# attached. LED feedback needs write access to the device — see the README for
# the udev rule.
#
#   {cm.trigger_key} (mic key)  start/stop a recording, same as the hotkey
#   encoder turn             cycle menu pages: model -> mode -> delivery
#   agent keys AG00-AG05     pick the item on the current page
#   encoder click            close the menu
#
# The menu closes itself after a few seconds, and never opens while recording.
[codex_micro]
enabled = {str(cm.enabled).lower()}
trigger_key = "{cm.trigger_key}"
trigger_delivery = "{cm.trigger_delivery}"   # "clipboard" or "type"
led_feedback = {str(cm.led_feedback).lower()}
menu = {str(cm.menu).lower()}
brightness = {cm.brightness}                 # 0-255

# Key colors, matching the tray icon's states.
color_idle = "#{cm.color_idle:06x}"
color_recording = "#{cm.color_recording:06x}"
color_busy = "#{cm.color_busy:06x}"
color_disabled = "#{cm.color_disabled:06x}"
color_loading = "#{cm.color_loading:06x}"
color_option_a = "#{cm.color_option_a:06x}"
color_option_b = "#{cm.color_option_b:06x}"
''')
