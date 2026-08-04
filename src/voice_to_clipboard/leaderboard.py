"""Rank Open ASR Leaderboard models and map them onto our backends.

Methodology (kept deliberately identical to re-running the analysis by hand):

- Data source: ``english_short_latest.csv`` from the
  ``hf-audio/open-asr-leaderboard-results`` dataset — the exact file the
  leaderboard Space renders, so ranks match the public UI. Ranking metric is
  "avg cleaned" WER over the leaderboard's standard English test-set suite.
  All models are scored on the same datasets; nothing from other leaderboards
  is mixed in.
- A model is only marked runnable if it can load through one of our backends
  without security compromises:
    * proprietary / API-only entries are excluded (no public weights),
    * models that require ``trust_remote_code`` are excluded (that means
      executing arbitrary repo code),
    * NeMo-only checkpoints are excluded (separate toolkit),
    * models with no transformers metadata are excluded (no standard loader).
- Backend mapping: ``qwen3_asr*`` architectures -> "qwen-asr"; whisper
  architectures -> "transformers" (or faster-whisper for CT2 conversions);
  any other transformers model tagged for the automatic-speech-recognition
  pipeline -> "transformers", flagged as untested since a pipeline tag does
  not guarantee our exact pipeline call works.
"""

import csv
import io
import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)

RESULTS_URL = (
    "https://huggingface.co/datasets/hf-audio/open-asr-leaderboard-results"
    "/raw/main/english_short_latest.csv"
)
MODEL_API = "https://huggingface.co/api/models/{model_id}"
USER_AGENT = "voice-to-clipboard-leaderboard-check"


@dataclass
class Candidate:
    rank: int
    model_id: str
    wer: float
    rtfx: float | None
    size_b: float | None  # parameter count in billions, if published
    license: str
    backend: str | None  # None = not runnable by us
    note: str

    @property
    def runnable(self) -> bool:
        return self.backend is not None

    @property
    def spec(self) -> str:
        return f"{self.backend}:{self.model_id}"


@dataclass
class Hardware:
    has_gpu: bool
    gpu_name: str = ""
    vram_gb: float | None = None

    @property
    def summary(self) -> str:
        if self.has_gpu:
            vram = f", {self.vram_gb:.0f} GB VRAM" if self.vram_gb else ""
            return f"GPU: {self.gpu_name}{vram}"
        return "No GPU detected — CPU-suitable models only"


# Interactive dictation on CPU is only tolerable for smallish models; above
# this parameter count we consider a model GPU-only.
CPU_MAX_PARAMS_B = 1.0
# fp16 bytes/param plus runtime overhead when estimating VRAM fit.
VRAM_OVERHEAD = 1.3


def detect_hardware() -> Hardware:
    try:
        import torch

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return Hardware(True, torch.cuda.get_device_name(0), props.total_memory / 1e9)
    except Exception:
        log.exception("GPU detection failed")
    return Hardware(False)


def supports(candidate: Candidate, hw: Hardware) -> bool | None:
    """Whether this machine can run the model. None = unknown (no size data)."""
    if candidate.size_b is None:
        return None
    if hw.has_gpu:
        if hw.vram_gb is None:
            return True
        return candidate.size_b * 2 * VRAM_OVERHEAD <= hw.vram_gb
    return candidate.size_b <= CPU_MAX_PARAMS_B


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _classify(model_id: str, license_name: str) -> tuple[str | None, str]:
    """Return (backend, note) for a leaderboard entry."""
    if license_name.lower() == "proprietary":
        return None, "proprietary / API-only, no public weights"
    try:
        info = json.loads(_fetch(MODEL_API.format(model_id=model_id)))
    except Exception:
        return None, "no public model repo"
    tags = info.get("tags", [])
    library = info.get("library_name", "")
    pipeline = info.get("pipeline_tag", "")
    model_type = (info.get("config") or {}).get("model_type", "")

    if "custom_code" in tags:
        return None, "requires trust_remote_code (executes repo code — excluded)"
    if library == "nemo":
        return None, "needs the NeMo toolkit"
    if model_type.startswith("qwen3_asr"):
        return "qwen-asr", "verified backend"
    if model_type == "whisper":
        return "transformers", "whisper architecture"
    if model_type == "granite_speech":
        return "granite-speech", "verified backend"
    if "ctranslate2" in tags:
        return "faster-whisper", "CTranslate2 conversion"
    if library == "transformers" and pipeline == "automatic-speech-recognition":
        return "transformers", "untested architecture — pipeline tag suggests it works"
    if library != "transformers":
        return None, f"no transformers support (library: {library or 'none'})"
    return None, f"not an ASR-pipeline model (pipeline: {pipeline or 'none'})"


def fetch_ranked(top_n: int | None = None) -> list[Candidate]:
    """Leaderboard entries by avg cleaned WER, classified for our backends."""
    rows = list(csv.DictReader(io.StringIO(_fetch(RESULTS_URL).decode())))
    scored = []
    for row in rows:
        try:
            scored.append((float(row["avg cleaned"]), row))
        except (KeyError, ValueError):
            continue
    scored.sort(key=lambda pair: pair[0])
    if top_n is not None:
        scored = scored[:top_n]

    def parse_float(value: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        classified = list(
            pool.map(lambda pair: _classify(pair[1]["model"], pair[1].get("License", "")), scored)
        )

    candidates = []
    for rank, ((wer, row), (backend, note)) in enumerate(
        zip(scored, classified, strict=True), start=1
    ):
        candidates.append(
            Candidate(
                rank=rank,
                model_id=row["model"],
                wer=wer,
                rtfx=parse_float(row.get("RTFx")),
                size_b=parse_float(row.get("Size (B)")),
                license=row.get("License", ""),
                backend=backend,
                note=note,
            )
        )
        log.info("leaderboard #%d %s -> %s (%s)", rank, row["model"], backend, note)
    return candidates


def format_table(
    candidates: list[Candidate], current_spec: str = "", hw: Hardware | None = None
) -> str:
    lines = []
    if hw is not None:
        lines.append(hw.summary)
    lines += [
        f"{'#':>3} {'fit':<4}{'WER%':>6} {'size':>6} {'RTFx':>7}  {'backend':<15} model",
        "-" * 100,
    ]
    for c in candidates:
        marker = " <- current" if c.spec == current_spec else ""
        if c.runnable and c.note.startswith("untested"):
            marker += " (untested)"
        fit = ""
        if hw is not None and c.runnable:
            fit = {True: "✓", False: "✗", None: "?"}[supports(c, hw)]
        backend = c.backend or "—"
        rtfx = f"{c.rtfx:.0f}" if c.rtfx else "—"
        size = f"{c.size_b:g}B" if c.size_b else "—"
        lines.append(
            f"{c.rank:>3} {fit:<4}{c.wer:>6.2f} {size:>6} {rtfx:>7}  "
            f"{backend:<15} {c.model_id}{marker}"
        )
        if not c.runnable:
            lines.append(f"{'':>25}  excluded: {c.note}")
    return "\n".join(lines)
