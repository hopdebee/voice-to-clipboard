"""Speech-to-text backends.

Four backends:
  - "qwen-asr": Qwen3-ASR family via the transformers multimodal-LM API
    (needs transformers >= 5.13). Top open-leaderboard accuracy; the default.
  - "granite-speech": IBM granite-speech family via its chat-template API
    (needs torchaudio).
  - "faster-whisper": CTranslate2-based Whisper, fastest Whisper option.
    Model is a size alias (large-v3, distil-large-v3.5, ...), a HF repo id
    containing a CTranslate2 conversion, or a local path.
  - "transformers": Hugging Face pipeline. Model is any HF repo id that works
    with the automatic-speech-recognition pipeline — this is the escape hatch
    for running brand-new models.
"""

import gc
import logging
import threading

import numpy as np

# Import torch first so the CUDA runtime libraries (cuDNN, cuBLAS) bundled in
# its wheel are loaded into the process before CTranslate2 goes looking for them.
import torch

log = logging.getLogger(__name__)


def _has_speech(audio: np.ndarray) -> bool:
    """Silero VAD check (CPU, milliseconds). Fails open if VAD is unavailable."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        return bool(get_speech_timestamps(audio, VadOptions()))
    except Exception:
        log.exception("VAD check failed; assuming speech")
        return True


class Transcriber:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backend = ""
        self._model_id = ""
        self._model = None  # WhisperModel, transformers pipeline, or LM model
        self._processor = None  # qwen-asr only

    @property
    def loaded_model(self) -> str:
        return f"{self._backend}:{self._model_id}" if self._model else ""

    def load(self, backend: str, model_id: str, device: str, compute_type: str) -> None:
        with self._lock:
            if self._model is not None and (backend, model_id) == (self._backend, self._model_id):
                return
            self._unload_locked()
            log.info("loading %s:%s on %s", backend, model_id, device)
            if backend == "faster-whisper":
                from faster_whisper import WhisperModel

                self._model = WhisperModel(model_id, device=device, compute_type=compute_type)
            elif backend == "transformers":
                from transformers import pipeline

                self._model = pipeline(
                    "automatic-speech-recognition",
                    model=model_id,
                    dtype=torch.float16 if device == "cuda" else torch.float32,
                    device=0 if device == "cuda" else -1,
                )
            elif backend == "qwen-asr":
                from transformers import AutoModelForMultimodalLM, AutoProcessor

                self._processor = AutoProcessor.from_pretrained(model_id)
                self._model = AutoModelForMultimodalLM.from_pretrained(
                    model_id,
                    dtype=torch.float16 if device == "cuda" else torch.float32,
                ).to(device)
            elif backend == "granite-speech":
                from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

                self._processor = AutoProcessor.from_pretrained(model_id)
                # the model card recommends bfloat16
                self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
                    model_id,
                    dtype=torch.bfloat16 if device == "cuda" else torch.float32,
                ).to(device)
            else:
                raise ValueError(f"unknown backend: {backend!r}")
            self._backend, self._model_id = backend, model_id
            log.info("model loaded")

    def unload(self) -> None:
        """Free the model (and its GPU memory); it reloads on next use."""
        with self._lock:
            self._unload_locked()
            self._backend = self._model_id = ""
            log.info("model unloaded")

    def _unload_locked(self) -> None:
        if self._model is not None:
            self._model = None
            self._processor = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def transcribe(self, audio: np.ndarray, language: str = "") -> str:
        """audio: mono float32 at 16 kHz."""
        with self._lock:
            if self._model is None:
                raise RuntimeError("no model loaded")
            # LLM-based models can hallucinate text from pure noise (verified with
            # granite-speech), so gate every backend except faster-whisper (which
            # runs the same Silero VAD internally).
            if self._backend != "faster-whisper" and not _has_speech(audio):
                return ""
            if self._backend == "faster-whisper":
                segments, _info = self._model.transcribe(
                    audio,
                    language=language or None,
                    vad_filter=True,
                )
                text = " ".join(seg.text.strip() for seg in segments)
            elif self._backend == "qwen-asr":
                inputs = self._processor.apply_transcription_request(
                    audio=audio, language=language or None
                ).to(self._model.device, self._model.dtype)
                output_ids = self._model.generate(**inputs, max_new_tokens=1024)
                generated = output_ids[:, inputs["input_ids"].shape[1]:]
                raw = self._processor.batch_decode(generated, skip_special_tokens=True)[0]
                # output format: "language English<asr_text>the transcription"
                text = raw.split("<asr_text>", 1)[-1]
            elif self._backend == "granite-speech":
                # granite has no language parameter; `language` is ignored
                tokenizer = self._processor.tokenizer
                chat = [
                    {
                        "role": "user",
                        "content": "<|audio|>transcribe the speech with proper "
                        "punctuation and capitalization.",
                    }
                ]
                prompt = tokenizer.apply_chat_template(
                    chat, tokenize=False, add_generation_prompt=True
                )
                wav = torch.from_numpy(audio).unsqueeze(0)  # mono (1, samples)
                inputs = self._processor(
                    prompt, wav, device=str(self._model.device), return_tensors="pt"
                ).to(self._model.device)
                outputs = self._model.generate(
                    **inputs, max_new_tokens=1024, do_sample=False, num_beams=1
                )
                new_tokens = outputs[:, inputs["input_ids"].shape[-1]:]
                text = tokenizer.batch_decode(new_tokens, skip_special_tokens=True)[0]
            else:
                kwargs = {}
                if language:
                    kwargs["generate_kwargs"] = {"language": language}
                result = self._model(
                    {"array": audio, "sampling_rate": 16000},
                    **kwargs,
                )
                text = result["text"]
            return text.strip()
