"""In-app updates: check GitHub releases, fast-forward the checkout, restart.

The app is installed editable into a venv that lives inside its own git
checkout, so the running code *is* the working tree. Updating therefore means
moving that checkout onto the released tag, reinstalling in case dependencies
changed, and restarting the systemd user service.

Everything here is stdlib-only and safe to call from a worker thread. The
network and subprocess steps report progress through a ``notify(title,
message, error)`` callback so the caller can put them on the GUI thread.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

RELEASE_API = "https://api.github.com/repos/binaryninja/voice-to-clipboard/releases/latest"
USER_AGENT = "voice-to-clipboard-update-check"
SERVICE_NAME = "voice-to-clipboard.service"

NET_TIMEOUT = 10  # the GitHub API answers fast or not at all
GIT_TIMEOUT = 180
INSTALL_TIMEOUT = 1800  # a torch reinstall is slow on a cold cache

# (title, message, error) -> None
Notify = Callable[[str, str, bool], None]


class UpdateError(RuntimeError):
    """A step failed in a way worth showing the user verbatim."""


@dataclass
class Release:
    version: str  # normalized, no leading "v" — e.g. "0.2.1"
    tag: str  # tag exactly as published — e.g. "v0.2.1"
    name: str  # release title, falling back to the tag
    url: str  # release notes page
    newer: bool  # newer than the running version


# ---------- version comparison ----------


def parse_version(text: str) -> tuple[int, ...]:
    """"v0.10.0" -> (0, 10, 0).

    Tolerates a leading "v" and pre-release/build trailers: "0.2.1rc1" and
    "0.2.1-beta" both parse as (0, 2, 1). Numeric tuples are what make
    0.10.0 sort above 0.9.0, which string comparison gets backwards.
    """
    cleaned = text.strip().lstrip("vV").split("+", 1)[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a strictly higher version than ``current``."""
    left, right = parse_version(candidate), parse_version(current)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


# ---------- checking ----------


def check_latest(current: str = __version__) -> Release | None:
    """The latest published release, or None when the repo has none yet.

    Raises on network/parse failures, so run it off the GUI thread.
    """
    req = urllib.request.Request(
        RELEASE_API,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:  # repo exists, nothing released yet — not an error
            log.info("no GitHub releases published yet")
            return None
        raise
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        log.info("latest release has no tag_name")
        return None
    release = Release(
        version=tag.lstrip("vV"),
        tag=tag,
        name=str(data.get("name") or tag).strip() or tag,
        url=str(data.get("html_url") or ""),
        newer=is_newer(tag, current),
    )
    log.info("latest release %s (running %s, newer=%s)", release.tag, current, release.newer)
    return release


# ---------- the checkout ----------


def _run(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    log.info("running %s (cwd=%s)", " ".join(argv), cwd)
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _git(root: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], root, timeout)


def _failure(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    return text[:400] if text else f"exit code {proc.returncode}"


def repo_root() -> Path | None:
    """The git checkout holding the running code, or None if it isn't one."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            break
    else:
        return None
    try:
        proc = _git(parent, "rev-parse", "--show-toplevel", timeout=30)
    except Exception:
        log.exception("git rev-parse failed")
        return None
    if proc.returncode != 0:
        log.warning("%s has a .git but git disagrees: %s", parent, _failure(proc))
        return None
    return Path(proc.stdout.strip() or parent)


def dirty_files(root: Path) -> list[str]:
    """Tracked files with local changes. Untracked files don't block an update."""
    proc = _git(root, "status", "--porcelain")
    if proc.returncode != 0:
        raise UpdateError(f"git status failed: {_failure(proc)}")
    return [
        line[3:].strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def fast_forward(root: Path, release: Release) -> None:
    """Land the checkout exactly on the released commit, or raise."""
    proc = _git(root, "fetch", "--tags", "origin")
    if proc.returncode != 0:
        raise UpdateError(f"git fetch failed: {_failure(proc)}")

    proc = _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{release.tag}^{{commit}}")
    commit = proc.stdout.strip()
    if proc.returncode != 0 or not commit:
        raise UpdateError(f"tag {release.tag} is not in origin after fetching")

    proc = _git(root, "merge", "--ff-only", commit)
    if proc.returncode != 0:
        raise UpdateError(
            f"cannot fast-forward to {release.tag}: {_failure(proc)}. "
            "This checkout has diverged from the release; update it by hand."
        )
    log.info("checkout moved to %s (%s)", release.tag, commit[:12])


# ---------- dependencies ----------


def venv_python() -> str:
    """The interpreter of the venv we're installed into."""
    candidate = Path(sys.prefix) / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for path in (
        Path(sys.prefix) / "bin" / "uv",
        Path.home() / ".local" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ):
        if path.exists():
            return str(path)
    return None


def reinstall(root: Path) -> None:
    """Re-run the editable install so dependency changes in the release land."""
    python = venv_python()
    commands: list[list[str]] = []
    uv = _find_uv()
    if uv:
        commands.append([uv, "pip", "install", "--python", python, "-e", "."])
    commands.append([python, "-m", "pip", "install", "-e", "."])

    problem = ""
    for argv in commands:
        proc = _run(argv, root, INSTALL_TIMEOUT)
        output = (proc.stdout or "").strip()
        log.info("%s -> exit %d\n%s", argv[0], proc.returncode, output[-2000:])
        if proc.returncode == 0:
            return
        problem = _failure(proc)
        log.warning("install failed (%s): %s", argv[0], problem)
    raise UpdateError(problem or "no installer available")


# ---------- restart ----------


def under_systemd() -> bool:
    """Whether this process is the systemd user service, so we may restart it."""
    if os.environ.get("INVOCATION_ID"):
        return True
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        log.exception("systemctl is-active failed")
        return False
    return proc.stdout.strip() == "active"


def restart_service() -> None:
    """Restart detached: the restart kills the process asking for it, so the
    request has to outlive us. --no-block hands the job to systemd and returns
    immediately, and start_new_session keeps the child off our process group.
    """
    subprocess.Popen(
        ["systemctl", "--user", "--no-block", "restart", SERVICE_NAME],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log.info("requested restart of %s", SERVICE_NAME)


# ---------- the whole update ----------


def run_update(release: Release, notify: Notify) -> bool:
    """Move the checkout onto ``release``, reinstall, restart.

    Returns True if the code on disk was updated. Never raises: every failure
    is reported through ``notify`` so a broken update can't take down the tray.
    """
    try:
        root = repo_root()
        if root is None:
            notify(
                "Update failed",
                "This install is not a git checkout, so it can't update itself. "
                "Update it the way you installed it.",
                True,
            )
            return False
        dirty = dirty_files(root)
        if dirty:
            listed = ", ".join(dirty[:5]) + (", …" if len(dirty) > 5 else "")
            notify(
                "Update refused",
                f"{root} has uncommitted changes ({listed}). "
                "Commit or stash them first — updating would overwrite your work.",
                True,
            )
            return False
        fast_forward(root, release)
    except Exception as e:
        log.exception("update failed")
        notify("Update failed", str(e), True)
        return False

    notify(f"Updated to {release.tag}", "Reinstalling dependencies…", False)
    try:
        reinstall(root)
    except Exception as e:
        log.exception("dependency reinstall failed")
        # The new code is on disk and editable, so it is already what a restart
        # would run — don't restart into possibly-missing dependencies.
        notify(
            "Dependency install failed",
            f"{e} — {release.tag} is checked out; finish with "
            f"'uv pip install -e .' in {root}, then restart.",
            True,
        )
        return True

    if not under_systemd():
        notify(
            "Update installed",
            f"{release.tag} is in place — restart voice-to-clipboard to run it.",
            False,
        )
        return True

    notify(
        "Restarting",
        f"Restarting voice-to-clipboard to finish updating to {release.tag}.",
        False,
    )
    try:
        restart_service()
    except Exception as e:
        log.exception("service restart failed")
        notify(
            "Restart failed",
            f"{e} — run: systemctl --user restart {SERVICE_NAME}",
            True,
        )
    return True
