"""Audio feedback: a continuous om-like drone while recording, plus result tones."""

import threading

import numpy as np
import sounddevice as sd

_RATE = 44100

DRONE_FREQ = 417.0  # Hz
DRONE_VOLUME = 0.08


class _Drone:
    """Phase-continuous 417 Hz drone with ~120 ms attack/release envelope."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stream: sd.OutputStream | None = None
        self._phase = 0
        self._env = 0.0
        self._target = 0.0

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                if self._target == 1.0 and self._stream.active:
                    return  # already droning
                # stream is fading out or finished; retire it and start fresh.
                # Never close() from the audio callback context — only from here.
                try:
                    self._stream.abort()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self._target = 1.0
            self._phase = 0
            self._env = 0.0
            try:
                self._stream = sd.OutputStream(
                    samplerate=_RATE,
                    channels=1,
                    callback=self._callback,
                )
                self._stream.start()
            except Exception:
                self._stream = None  # audio output problems never block recording

    def stop(self) -> None:
        with self._lock:
            self._target = 0.0
            # the callback fades to silence and raises CallbackStop; the stopped
            # stream is cleaned up on the next start()

    def _callback(self, outdata, frames, _time, _status) -> None:
        t = (self._phase + np.arange(frames)) / _RATE
        wave = (
            0.60 * np.sin(2 * np.pi * DRONE_FREQ * t)
            + 0.25 * np.sin(2 * np.pi * DRONE_FREQ / 2 * t)
            + 0.15 * np.sin(2 * np.pi * DRONE_FREQ * 2 * t)
        )
        breath = 1.0 + 0.12 * np.sin(2 * np.pi * 0.4 * t)  # slow om-like swell
        env = np.empty(frames)
        e, target = self._env, self._target
        step = 1.0 / (0.12 * _RATE)  # 120 ms attack/release
        for i in range(frames):
            diff = target - e
            e += max(-step, min(step, diff))
            env[i] = e
        self._env = e
        outdata[:, 0] = (DRONE_VOLUME * wave * breath * env).astype(np.float32)
        self._phase += frames
        if target == 0.0 and e < 1e-4:
            raise sd.CallbackStop


_drone = _Drone()


def start_drone() -> None:
    _drone.start()


def stop_drone() -> None:
    _drone.stop()


def _tone(freq: float, ms: int, volume: float = 0.25) -> np.ndarray:
    t = np.linspace(0, ms / 1000, int(_RATE * ms / 1000), endpoint=False)
    wave = volume * np.sin(2 * np.pi * freq * t)
    fade = min(len(t) // 4, 400)
    env = np.ones_like(wave)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    return (wave * env).astype(np.float32)


def _play(wave: np.ndarray) -> None:
    try:
        sd.play(wave, _RATE)
    except Exception:
        pass  # feedback tones are never worth crashing over


def success() -> None:
    _play(_tone(1040, 120))


def error() -> None:
    _play(_tone(220, 250))
