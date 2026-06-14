"""Launching the reconstructed build and playing a replay.

Ensures Steam is running, temporarily disables user mods, and starts the game
through RunAsDate at the replay's timestamp so old builds skip date checks.
"""

from __future__ import annotations

import contextlib
import ctypes
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import tools
from .config import Config

# The game executable only. EssenceEditor.exe (the content editor) is deliberately
# excluded — it is not the replay target.
_EXE_CANDIDATES = ("RelicCardinal.exe", "AoE4.exe")
_GAME_IMAGE = "RelicCardinal.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console pop-ups under pythonw


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
        "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
        "dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS",
    )]


def _exe_build_number(path: Path) -> int | None:
    """Build component of a Windows exe's file version (e.g. 10604 for 16.2.10604.0)."""
    from ctypes import wintypes

    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
        return None
    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or not ptr.value:
        return None
    info = ctypes.cast(ptr, ctypes.POINTER(_FixedFileInfo)).contents
    return (info.dwFileVersionLS >> 16) & 0xFFFF


def installed_game_version(cfg: Config) -> int | None:
    """Build number of the installed RelicCardinal.exe, or None if unavailable.

    Matches a replay's :func:`replay.read_version`, so an equal value means the
    replay was recorded on the build that is currently installed.
    """
    try:
        exe = find_executable(cfg.steam_install)
    except FileNotFoundError:
        return None
    try:
        return _exe_build_number(exe)
    except OSError:
        return None


def find_executable(install_dir: Path) -> Path:
    """Locate the AoE4 executable inside a launch build."""
    install_dir = Path(install_dir)
    for name in _EXE_CANDIDATES:
        hit = next(install_dir.rglob(name), None)
        if hit:
            return hit
    # No loose "any Age/Relic .exe" fallback: it could pick the editor or a
    # telemetry tool. Fail clearly instead of launching the wrong executable.
    raise FileNotFoundError(
        f"Could not find the AoE4 executable (RelicCardinal.exe) under '{install_dir}'."
    )


def _process_running(image: str) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
    ).stdout
    return image.lower() in out.lower()


def is_game_running() -> bool:
    """True if the AoE4 executable is currently running."""
    return _process_running(_GAME_IMAGE)


def _steam_logged_in() -> bool:
    """True if a user is signed in to the running Steam client (registry ActiveUser)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess"
        ) as key:
            active_user, _ = winreg.QueryValueEx(key, "ActiveUser")
        return int(active_user) != 0
    except (OSError, ValueError):
        return False


def ensure_steam_running(cfg: Config) -> None:
    """Require Steam to be open and signed in. Never auto-starts Steam — the game
    needs the user's own session, so we ask them to open it instead."""
    if _process_running("steam.exe") and _steam_logged_in():
        return
    raise RuntimeError("Please open Steam and log into your account, then try again.")


@dataclass
class ModsState:
    original: Path
    disabled: Path


def _disabled_mods_root(cfg: Config) -> Path:
    # A sibling of the mods folder so disabling is an atomic same-volume rename;
    # Documents may sit on a different drive than the project (a move there would
    # be a slow, interruptible copy+delete).
    return cfg.mods_dir.parent / ".aoe4-disabled-mods"


def disable_user_mods(cfg: Config) -> ModsState | None:
    if not cfg.mods_dir.exists():
        return None
    root = _disabled_mods_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    # Unique suffix so two disables in the same second never nest one backup
    # inside the other (shutil.move into an existing dir). The timestamp prefix
    # keeps recover()'s newest-first ordering correct.
    disabled = root / f"mods_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}"
    print("Temporarily disabling AOE4 user mods for replay launch...")
    shutil.move(str(cfg.mods_dir), str(disabled))
    return ModsState(original=cfg.mods_dir, disabled=disabled)


def recover_user_mods(cfg: Config) -> None:
    """Restore mods left disabled by a crash or a forced close mid-launch.

    Called at startup: if a disabled-mods backup is lying around and the live
    mods folder is missing or only the game's empty skeleton, move the newest
    backup back. Real user files already in place are never overwritten.
    """
    root = _disabled_mods_root(cfg)
    if not root.exists():
        return
    backups = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("mods_")),
        reverse=True,
    )
    if not backups:
        return
    if cfg.mods_dir.exists() and any(p.is_file() for p in cfg.mods_dir.rglob("*")):
        return  # the user's mods are in place; leave the backups for manual review
    newest = backups[0]
    with contextlib.suppress(OSError):
        if cfg.mods_dir.exists():
            shutil.rmtree(cfg.mods_dir, ignore_errors=True)
        cfg.mods_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(newest), str(cfg.mods_dir))
        print("Recovered user mods left disabled by a previous session.")


def restore_user_mods(state: ModsState | None) -> None:
    if state is None or not state.disabled.exists():
        return
    if state.original.exists():
        # The game recreates an empty default mods skeleton (extension/, replacement/
        # with no files) on every launch. Discard that; only preserve a recreated
        # folder if it unexpectedly holds real files, so we never accumulate junk.
        if any(p.is_file() for p in state.original.rglob("*")):
            conflict = state.original.with_name(
                f"{state.original.name}.replay-conflict-{datetime.now():%Y%m%d%H%M%S}"
            )
            shutil.move(str(state.original), str(conflict))
            print(f"A mods folder with files appeared while the replay ran; kept it at: {conflict}")
        else:
            shutil.rmtree(state.original, ignore_errors=True)
    shutil.move(str(state.disabled), str(state.original))
    print("AOE4 user mods restored.")


def _wait_for_game_exit(image: str, appear_timeout: int = 180) -> None:
    appeared = False
    for _ in range(appear_timeout):
        if _process_running(image):
            appeared = True
            break
        time.sleep(1)
    if not appeared:
        print(f"{image} was not observed starting; not waiting.")
        return
    print("AOE4 is running. Waiting for it to exit...")
    while _process_running(image):
        time.sleep(2)


def launch_replay(
    cfg: Config,
    launch_dir: Path,
    replay_name: str,
    when: datetime,
    use_runasdate: bool = True,
    keep_mods: bool = False,
    dev: bool = True,
) -> None:
    """Launch the game on the composed build and wait for it to exit."""
    launch_dir = Path(launch_dir)
    exe = find_executable(launch_dir)
    (launch_dir / "steam_appid.txt").write_text(cfg.app_id, encoding="ascii")

    game_args = ["-replay", f"playback:{replay_name}"]
    if dev:
        game_args = ["-dev", *game_args]

    ensure_steam_running(cfg)
    mods_state = None if keep_mods else disable_user_mods(cfg)
    try:
        runasdate = None
        if use_runasdate:
            try:
                runasdate = tools.ensure_runasdate(cfg)
            except FileNotFoundError as exc:
                print(f"{exc}\nFalling back to a direct launch; old builds may show date warnings.")

        if runasdate:
            run_date = when.strftime("%d/%m/%Y")
            run_time = when.strftime("%H:%M:%S")
            args = [
                str(runasdate), "/movetime", "/startin", str(launch_dir),
                run_date, run_time, str(exe), *game_args,
            ]
            print(f"Starting AOE4 through RunAsDate at replay time: {when:%Y-%m-%d %H:%M:%S}")
            subprocess.Popen(args, cwd=str(launch_dir))
            # Watch the executable we actually launched, not a hard-coded name, so
            # the watcher can't miss the game and let cleanup delete a running build.
            _wait_for_game_exit(exe.name)
        else:
            print(f"Launching {exe.name} {' '.join(game_args)}")
            proc = subprocess.Popen([str(exe), *game_args], cwd=str(launch_dir))
            proc.wait()
    finally:
        restore_user_mods(mods_state)
