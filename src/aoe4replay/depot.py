"""DepotDownloader wrapper: fetch manifests and download build deltas."""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import steamqr, tools
from .buildmap import Build
from .config import Config
from .manifest import parse_manifest

_MAX_ATTEMPTS = 4
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DownloadCancelled(RuntimeError):
    """Raised when the user cancels a download (so callers don't retry/treat it
    as a failure)."""


class SteamAuthError(RuntimeError):
    """Raised when a download fails because the saved Steam login expired or was
    rejected — the GUI clears the cached identity and reconnects on retry."""


# Substrings in DepotDownloader output that mean "the saved login is no good".
_AUTH_FAIL_MARKERS = (
    "invalidpassword", "invalid password", "logon requires",
    "access token", "two-factor", "auth code",
)


def _is_auth_failure(output: str) -> bool:
    return any(s in output.lower() for s in _AUTH_FAIL_MARKERS)

# Steam Guard prompt fingerprints (substrings, case-insensitive). These come
# from the SteamKit2 / DepotDownloader build we ship and are verified against the
# bundled binary; keep them in one place so a version bump is easy to re-check.
_CONFIRM = "use the steam mobile app to confirm"  # phone push — no code to type
_DEVICE = "auth code from your authenticator"  # mobile authenticator TOTP
_EMAIL = ("auth code sent to the email", "code sent to your email")  # mailed code
_INCORRECT = ("is incorrect", "previous 2-factor")
_EMAIL_RE = re.compile(r"email at\s+(.+?)\s*:", re.I)
_ACCOUNT_RE = re.compile(r"login with\s+-username\s+(\S+)", re.I)

# on_2fa(kind, email, previous_was_wrong) -> code | None
#   kind == "confirm": phone push; return value ignored (nothing to type)
#   kind == "device" : authenticator code; return the 6-digit code
#   kind == "email"  : mailed code; ``email`` is the (masked) address Steam shows
Prompt2FA = Callable[[str, str | None, bool], "str | None"]


def _classify_2fa(low: str) -> str | None:
    if _CONFIRM in low:
        return "confirm"
    if _DEVICE in low:
        return "device"
    if any(s in low for s in _EMAIL):
        return "email"
    return None


def _kill_on_cancel(proc: subprocess.Popen, cancel: threading.Event | None) -> None:
    """Kill ``proc`` if ``cancel`` is set — the reader's ``read(1)`` blocks, so a
    watchdog thread is the only way to unblock a user-cancelled sign-in."""
    if cancel is None:
        return

    def _watch() -> None:
        # Wake periodically so the watcher also exits when the process finishes on
        # its own — otherwise it would block on cancel forever and leak a thread.
        while not cancel.wait(0.2):
            if proc.poll() is not None:
                return
        with contextlib.suppress(Exception):
            proc.kill()

    threading.Thread(target=_watch, daemon=True).start()


def steam_login(
    cfg: Config,
    build: Build,
    username: str,
    password: str,
    on_2fa: Prompt2FA,
    cancel: threading.Event | None = None,
) -> None:
    """Sign in with username/password (no console), caching the credential.

    Steam Guard is reported through ``on_2fa`` so the UI can tell the user
    exactly what is happening — approve a push on the phone, type the
    authenticator code, or type the mailed code — instead of one generic prompt.
    A throwaway manifest-only fetch forces the authentication. Raises on failure.
    """
    exe = tools.ensure_depotdownloader(cfg)
    tmp = Path(tempfile.mkdtemp(prefix="aoe4login_"))
    args = [
        *_depot_args(cfg, build),
        "-manifest-only", "-dir", str(tmp),
        "-username", username, "-password", password, "-remember-password",
    ]
    proc = subprocess.Popen(  # noqa: S603
        [str(exe), *args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, creationflags=_NO_WINDOW,
    )
    _kill_on_cancel(proc, cancel)
    transcript: list[str] = []
    try:
        line = bytearray()
        handled = False  # a prompt on the current line was already answered
        incorrect = False  # the previous code was rejected
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in (b"\n", b"\r"):
                text = bytes(line).decode("utf-8", "replace")
                if text.strip():
                    transcript.append(text.strip())
                if any(s in text.lower() for s in _INCORRECT):
                    incorrect = True
                line.clear()
                handled = False
                continue
            line += ch
            if handled:
                continue
            # Prompts are written without a trailing newline (they wait for input),
            # so detect them on the partial line as it is typed out.
            decoded = bytes(line).decode("utf-8", "replace")
            kind = _classify_2fa(decoded.lower())
            if kind is None:
                continue
            handled = True
            if kind == "confirm":
                on_2fa("confirm", None, False)  # show "approve on your phone"; no input
                continue
            email = None
            if kind == "email":
                m = _EMAIL_RE.search(decoded)
                email = m.group(1).strip() if m else None
            code = on_2fa(kind, email, incorrect)
            incorrect = False
            if not code:
                proc.kill()
                raise RuntimeError("Steam sign-in was cancelled.")
            proc.stdin.write((code + "\n").encode())
            proc.stdin.flush()
            line.clear()
        proc.wait()
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(_login_error(transcript))


def steam_login_qr(
    cfg: Config,
    build: Build,
    on_qr: Callable[[list[list[bool]]], None],
    on_approved: Callable[[], None] | None = None,
    cancel: threading.Event | None = None,
) -> str | None:
    """Sign in by QR code (no password): the user scans it in the Steam Mobile
    App and approves. Returns the signed-in account name (so later downloads can
    reuse the cached token silently), or raises on failure/timeout.

    ``on_qr`` is called with a fresh module matrix to display, and again whenever
    Steam refreshes the challenge. ``on_approved`` fires once the scan succeeds.
    """
    exe = tools.ensure_depotdownloader(cfg)
    tmp = Path(tempfile.mkdtemp(prefix="aoe4qr_"))
    args = [
        *_depot_args(cfg, build),
        "-manifest-only", "-dir", str(tmp), "-qr", "-remember-password",
    ]
    proc = subprocess.Popen(  # noqa: S603
        [str(exe), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, creationflags=_NO_WINDOW,
    )
    _kill_on_cancel(proc, cancel)
    asm = steamqr.QrAssembler()
    transcript: list[str] = []
    account: str | None = None
    try:
        line = bytearray()
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            # The QR rows are newline-terminated; finalise only on "\n" (keeping
            # any trailing "\r") so a "\r\n" never injects a blank QR row.
            if ch != b"\n":
                line += ch
                continue
            raw = bytes(line)
            line.clear()
            matrix = asm.feed_line(raw)
            if matrix is not None:
                on_qr(matrix)
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            transcript.append(text)
            m = _ACCOUNT_RE.search(text)
            if m:
                account = m.group(1)
                if on_approved is not None:
                    on_approved()
        proc.wait()
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(_login_error(transcript))
    return account


def _login_error(transcript: list[str]) -> str:
    """Turn DepotDownloader's (hidden) output into a meaningful sign-in error."""
    text = "\n".join(transcript).lower()
    if "ratelimit" in text or "rate limit" in text:
        return (
            "Steam is temporarily blocking sign-in attempts after too many tries. "
            "Wait ~15-30 minutes, then try again (this is a Steam limit, not your "
            "password)."
        )
    if ("twofactor" in text or "two-factor" in text or "auth code" in text) and (
        "mismatch" in text or "invalid" in text or "incorrect" in text
    ):
        return "The Steam Guard code was not accepted. Try again with a fresh code."
    if "invalidpassword" in text or "invalid password" in text:
        return (
            "Steam rejected the sign-in. This is usually too many recent attempts "
            "(rate limiting) — wait a few minutes and try again — or a wrong password."
        )
    if "not available" in text or "no license" in text or "not own" in text:
        return (
            "Signed in, but this Steam account can't access the Age of Empires IV "
            "files. Use the account that actually owns the game."
        )
    for ln in reversed(transcript):
        if ln.strip():
            return f"Steam sign-in failed: {ln.strip()}"
    return "Steam sign-in failed. Please try again."


_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _run_capturing(
    exe: Path,
    args: list[str],
    progress: Callable[[float], None] | None,
    cancel: threading.Event | None = None,
) -> tuple[int, str]:
    r"""Run DepotDownloader, reporting the latest percentage and returning the
    ``(returncode, recent non-progress output)`` so the caller can classify a
    failure instead of blindly retrying it.

    DepotDownloader rewrites its progress in place with carriage returns
    (``10%\r25%\r``) rather than newlines, so read byte by byte and treat both
    ``\r`` and ``\n`` as a progress boundary — a line-based reader would block
    until a newline and leave the panel stuck near 0%.
    """
    proc = subprocess.Popen(  # noqa: S603
        [str(exe), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=_NO_WINDOW,
    )
    _kill_on_cancel(proc, cancel)  # a user cancel kills the process -> stdout EOF below
    last = -1.0
    line = bytearray()
    tail: list[str] = []  # recent status/error lines, for failure classification
    assert proc.stdout is not None
    while True:
        char = proc.stdout.read(1)
        if not char:
            break
        if char in (b"\r", b"\n"):
            text = bytes(line).decode("utf-8", "replace")
            match = _PCT_RE.search(text)
            if match:
                pct = float(match.group(1))
                if pct != last:
                    last = pct
                    if progress:
                        progress(pct)
            elif text.strip():
                tail.append(text.strip())
                del tail[:-40]  # keep only the last ~40 lines
            line.clear()
        else:
            line += char
    proc.wait()
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("Download cancelled.")
    return proc.returncode, "\n".join(tail)


def _fatal_download_error(output: str) -> str | None:
    """A non-transient DepotDownloader failure (retrying won't help), or None."""
    low = output.lower()
    if "ratelimit" in low or "rate limit" in low:
        return (
            "Steam is rate-limiting this account after too many attempts. "
            "Wait ~15-30 minutes, then try again."
        )
    if _is_auth_failure(low):
        return (
            "Steam sign-in failed (the saved login expired or was rejected). "
            "Reconnect your Steam account and try again."
        )
    if any(s in low for s in ("no license", "not own", "no subscription", "not available")):
        return (
            "This Steam account can't access the Age of Empires IV files. "
            "Use the account that owns the game."
        )
    if any(s in low for s in ("access is denied", "unauthorizedaccess", "could not create")):
        return "A file access error stopped the download (permissions or a locked file)."
    return None


def _auth_args(cfg: Config) -> list[str]:
    if cfg.steam_username:
        return ["-username", cfg.steam_username, "-remember-password"]
    return ["-qr"]


def _run_with_retry(
    cfg: Config,
    args: list[str],
    progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    exe = tools.ensure_depotdownloader(cfg)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Download cancelled.")
        print(f"DepotDownloader attempt {attempt}/{_MAX_ATTEMPTS}")
        returncode, output = _run_capturing(exe, args, progress, cancel)
        if returncode == 0:
            return
        # Don't burn minutes retrying an error a retry can't fix (wrong/expired
        # login, no license, permission); fail fast with a clear reason.
        fatal = _fatal_download_error(output)
        if fatal:
            # An auth failure gets a distinct type so the GUI can clear the stale
            # saved login and reconnect, instead of looping the same silent failure.
            if _is_auth_failure(output):
                raise SteamAuthError(fatal)
            raise RuntimeError(fatal)
        if attempt == _MAX_ATTEMPTS:
            tail = " ".join(output.strip().splitlines()[-3:])
            raise RuntimeError(
                f"DepotDownloader failed after {_MAX_ATTEMPTS} attempts. {tail}".strip()
            )
        delay = min(120, 30 * attempt)
        print(f"DepotDownloader failed (likely transient Steam CDN); retrying in {delay}s...")
        # Cancellable backoff: a cancel during the wait aborts immediately.
        if cancel is not None:
            if cancel.wait(delay):
                raise DownloadCancelled("Download cancelled.")
        else:
            time.sleep(delay)


def _depot_args(cfg: Config, build: Build) -> list[str]:
    depot = build.depots[0]
    return [
        "-app", cfg.app_id,
        "-depot", depot.id,
        "-manifest", depot.manifest,
    ]


def fetch_manifest(cfg: Config, build: Build, manifest_dir: Path) -> Path:
    """Download the manifest-only text for a build's primary depot.

    DepotDownloader names the file ``manifest_<depotId>_<manifestId>.txt``. We
    reuse a cached file only when it is *that exact* depot+manifest — a different
    manifest id is a different build (e.g. after a build-map correction or a
    misplaced cache file), and reusing it would reconstruct the wrong file set.
    """
    manifest_dir = Path(manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    depot = build.depots[0]
    expected = f"manifest_{depot.id}_{depot.manifest}.txt"

    # Reuse a cached manifest only if it actually parses to file records — an
    # empty/truncated file left by an interrupted fetch would otherwise be reused
    # and cause a permanent "no records" error (or a silently incomplete build).
    cached = next(iter(manifest_dir.rglob(expected)), None)
    if cached is not None and parse_manifest(cached):
        return cached

    args = [*_depot_args(cfg, build), "-manifest-only", "-dir", str(manifest_dir), *_auth_args(cfg)]
    _run_with_retry(cfg, args)

    written = next(iter(manifest_dir.rglob(expected)), None)
    if written is None:
        raise FileNotFoundError(
            f"Expected manifest {expected!r} was not written to {manifest_dir} "
            f"(depot {depot.id}, manifest {depot.manifest})."
        )
    return written


def download_files(
    cfg: Config,
    build: Build,
    target_dir: Path,
    file_list: list[str],
    progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Download a specific list of files for a build into ``target_dir`` (with retry).

    ``progress`` is called with the latest download percentage (0-100) if given.
    A set ``cancel`` event kills the download and raises ``DownloadCancelled``.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    # Keep the filelist inside the target so it is removed together with it.
    filelist_path = target_dir / "_filelist.txt"
    filelist_path.write_text("\n".join(file_list), encoding="utf-8")

    args = [
        *_depot_args(cfg, build),
        "-dir", str(target_dir),
        "-filelist", str(filelist_path),
        *_auth_args(cfg),
    ]
    _run_with_retry(cfg, args, progress, cancel)
