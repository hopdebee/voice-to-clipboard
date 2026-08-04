"""Microphone capture via sounddevice (PortAudio -> PipeWire)."""

import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # what Whisper-family models expect


class Recorder:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._chunks = []

        def callback(indata, frames, time_info, status):
            if status:
                log.warning("audio status: %s", status)
            self._chunks.append(indata[:, 0].copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        """Stop and return the captured mono float32 audio at 16 kHz."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self._chunks)
        self._chunks = []
        return audio
