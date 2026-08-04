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
    license: str
    backend: str | None  # None = not runnable by us
    note: str

    @property
    def runnable(self) -> bool:
        return self.backend is not None

    @property
    def spec(self) -> str:
        return f"{self.backend}:{self.model_id}"


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
    if "ctranslate2" in tags:
        return "faster-whisper", "CTranslate2 conversion"
    if library == "transformers" and pipeline == "automatic-speech-recognition":
        return "transformers", "untested architecture — pipeline tag suggests it works"
    if library != "transformers":
        return None, f"no transformers support (library: {library or 'none'})"
    return None, f"not an ASR-pipeline model (pipeline: {pipeline or 'none'})"


def fetch_ranked(top_n: int = 25) -> list[Candidate]:
    """Top-N leaderboard entries by avg cleaned WER, classified for our backends."""
    rows = list(csv.DictReader(io.StringIO(_fetch(RESULTS_URL).decode())))
    scored = []
    for row in rows:
        try:
            scored.append((float(row["avg cleaned"]), row))
        except (KeyError, ValueError):
            continue
    scored.sort(key=lambda pair: pair[0])

    candidates = []
    for rank, (wer, row) in enumerate(scored[:top_n], start=1):
        model_id = row["model"]
        license_name = row.get("License", "")
        try:
            rtfx = float(row.get("RTFx", ""))
        except ValueError:
            rtfx = None
        backend, note = _classify(model_id, license_name)
        candidates.append(
            Candidate(rank, model_id, wer, rtfx, license_name, backend, note)
        )
        log.info("leaderboard #%d %s -> %s (%s)", rank, model_id, backend, note)
    return candidates


def format_table(candidates: list[Candidate], current_spec: str = "") -> str:
    lines = [
        f"{'#':>3} {'WER%':>6} {'RTFx':>7}  {'backend':<15} model",
        "-" * 100,
    ]
    for c in candidates:
        marker = " <- current" if c.spec == current_spec else ""
        if c.runnable and c.note.startswith("untested"):
            marker += " (untested)"
        backend = c.backend or "—"
        rtfx = f"{c.rtfx:.0f}" if c.rtfx else "—"
        lines.append(
            f"{c.rank:>3} {c.wer:>6.2f} {rtfx:>7}  {backend:<15} {c.model_id}{marker}"
        )
        if not c.runnable:
            lines.append(f"{'':>21}  excluded: {c.note}")
    return "\n".join(lines)
