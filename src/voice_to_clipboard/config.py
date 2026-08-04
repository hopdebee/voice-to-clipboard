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
    return cfg


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    favorites = "\n".join(
        f'"{label}" = "{spec}"' for label, spec in cfg.favorites.items()
    )
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
[models]
{favorites}
''')
