"""Launching Age of Empires IV replay builds.

Current replays use Steam's normal ``-applaunch`` path. Reconstructed old builds
use a persistent Steam LaunchOptions wrapper so Steam still creates the official
AoE4 app session (env/ticket) while the wrapper starts the cached historical
executable. Old builds are still launched through RunAsDate so date-limited
middleware licenses accept them.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import subprocess
import sys
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
# Steam clears its "in-game" state a few seconds after the process exits (measured
# ~10s for a Steam launch). We wait this long before re-enabling Play so a new
# launch never collides with Steam's stale state. A fixed wait — deliberately not
# the Steam registry keys — keeps the binary off antivirus heuristics that flag a
# downloader-style exe for polling HKCU\Software\Valve\Steam right after spawning a child.
_STEAM_SETTLE_SECONDS = 15
# Steam writes localconfig.vdf as it shuts down; on some machines the process leaves
# the task list a moment before that flush lands on disk. Wait this long after Steam
# is gone before writing our LaunchOptions, so Steam's trailing flush can't overwrite
# (clobber) it. The whole one-time-restart install relies on this write surviving.
_STEAM_SHUTDOWN_SETTLE = 5
# A one-shot active replay request is consumed within seconds of the steam
# -applaunch that triggers the wrapper. Expire it quickly so a launcher that died
# mid-launch can't make a later normal Play open the wrong (old) build.
_REQUEST_TTL_SECONDS = 180


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


# --- Steam install / active user discovery (for the LaunchOptions wrapper) -----

def _steam_root(cfg: Config | None = None) -> Path:
    """Steam install root. Prefer the configured steam.exe's folder so every Steam
    operation (shutdown, localconfig, start) targets the *same* client; fall back to
    registry discovery when no override is set."""
    if cfg is not None:
        exe = getattr(cfg, "steam_exe", None)
        if exe is not None:
            root = Path(exe).parent
            if root.is_dir():
                return root
    try:
        import winreg

        for hive, sub, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as handle:
                    value, _ = winreg.QueryValueEx(handle, name)
            except OSError:
                continue
            path = Path(value)
            if path.is_dir():
                return path
    except OSError:
        pass
    return Path(r"C:\Program Files (x86)\Steam")


def _active_user_id() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess"
        ) as key:
            active_user, _ = winreg.QueryValueEx(key, "ActiveUser")
        user_id = str(int(active_user))
        if user_id != "0":
            return user_id
    except (OSError, ValueError):
        pass
    raise RuntimeError("Please open Steam and log into your account, then try again.")


def _localconfig_path(cfg: Config | None = None) -> Path:
    return _steam_root(cfg) / "userdata" / _active_user_id() / "config" / "localconfig.vdf"


# --- wrapper runtime paths ----------------------------------------------------

def _steam_wrapper_root(cfg: Config) -> Path:
    # A stable location OUTSIDE the (deletable) app folder, so the shim and its
    # config survive even if the user deletes the launcher — keeping normal Play
    # working. Independent of the portable app's RootAppDir.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "AoE4ReplayLauncher" / "steam_wrapper"


def _steam_wrapper_dispatch_config(cfg: Config) -> Path:
    return _steam_wrapper_root(cfg) / "dispatch.json"


def _steam_wrapper_request(cfg: Config) -> Path:
    return _steam_wrapper_root(cfg) / "active_request.json"


# --- one-time Steam restart (LaunchOptions are only read on Steam start) -------

def _shutdown_steam(cfg: Config | None = None, timeout: int = 60) -> None:
    steam = _steam_root(cfg) / "steam.exe"
    if steam.is_file():
        subprocess.run([str(steam), "-shutdown"], check=False, creationflags=_NO_WINDOW)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_running("steam.exe"):
            time.sleep(_STEAM_SHUTDOWN_SETTLE)  # let Steam's trailing localconfig flush land
            return
        time.sleep(1)
    raise RuntimeError("Steam did not close in time. Close Steam manually and try again.")


def _start_steam(cfg: Config) -> None:
    if not cfg.steam_exe.is_file():
        raise FileNotFoundError(f"steam.exe was not found: {cfg.steam_exe}")
    subprocess.Popen([str(cfg.steam_exe)], creationflags=_NO_WINDOW)


def _wait_for_steam_ready(timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _process_running("steam.exe") and _steam_logged_in():
            return
        time.sleep(1)
    # The usual cause is simply that the user hasn't picked their account in Steam's
    # sign-in window yet — the connection is fine, Steam just isn't signed in. Say so
    # (not "connection failed") and tell them how to continue.
    raise RuntimeError(
        "Steam restarted but no account is signed in yet. Sign in to Steam "
        "(select your account if the picker appears), then click Play again."
    )


# --- minimal VDF parser/formatter for localconfig.vdf -------------------------

def _tokenise_vdf(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "{}":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            tokens.append("".join(buf))
            continue
        start = i
        while i < n and not text[i].isspace() and text[i] not in "{}":
            i += 1
        tokens.append(text[start:i])
    return tokens


def _parse_vdf(text: str) -> tuple[str, dict]:
    tokens = _tokenise_vdf(text)
    pos = 0

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("Unexpected end of VDF")
        value = tokens[pos]
        pos += 1
        return value

    def parse_obj() -> dict:
        node: dict[str, str | dict] = {}
        while pos < len(tokens):
            key = take()
            if key == "}":
                return node
            value = take()
            if value == "{":
                node[key] = parse_obj()
            elif value == "}":
                raise ValueError("Unexpected VDF block close")
            else:
                node[key] = value
        return node

    root = take()
    if take() != "{":
        raise ValueError("VDF root is not an object")
    return root, parse_obj()


def _quote_vdf(value: object) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _format_vdf(root: str, node: dict) -> str:
    lines = [_quote_vdf(root), "{"]

    def emit(obj: dict, indent: int) -> None:
        pad = "\t" * indent
        for key, value in obj.items():
            if isinstance(value, dict):
                lines.append(f"{pad}{_quote_vdf(key)}")
                lines.append(f"{pad}{{")
                emit(value, indent + 1)
                lines.append(f"{pad}}}")
            else:
                lines.append(f"{pad}{_quote_vdf(key)}\t\t{_quote_vdf(value)}")

    emit(node, 1)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _child(node: dict, key: str) -> dict:
    value = node.get(key)
    if not isinstance(value, dict):
        value = {}
        node[key] = value
    return value


def _replace_with_retry(tmp: Path, path: Path, *, attempts: int = 40, delay: float = 0.02) -> None:
    """``os.replace``, retried on Windows' transient PermissionError when two
    launcher instances race to replace the same %LocalAppData% file at once."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: two launcher instances share %LocalAppData% and could write
    # the same file at once. A fixed .tmp would let them clobber each other mid-write;
    # per-writer temps keep os.replace atomic (last writer wins on the final file).
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _write_json_atomic(path: Path, data: dict) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def _set_launch_options(localconfig: Path, app_id: str, launch_options: str) -> None:
    root, data = _parse_vdf(localconfig.read_text(encoding="utf-8", errors="replace"))
    apps = _child(_child(_child(data, "Software"), "Valve"), "Steam")
    app = _child(_child(apps, "apps"), app_id)
    app["LaunchOptions"] = launch_options
    new_text = _format_vdf(root, data)
    # Safety net for the app's backbone: never hand Steam a file we can't reproduce.
    # Re-parse our own output and confirm it round-trips to exactly the data we meant
    # to write. This catches any formatter/parser defect *before* it could corrupt the
    # user's Steam config — a write that wouldn't round-trip is refused, not applied.
    try:
        root2, data2 = _parse_vdf(new_text)
    except ValueError as exc:
        raise RuntimeError(f"Refusing to write a malformed localconfig.vdf ({exc}).") from exc
    if root2 != root or data2 != data:
        raise RuntimeError(
            "Refusing to write localconfig.vdf: the rewrite did not round-trip cleanly."
        )
    _write_text_atomic(localconfig, new_text)


def _get_launch_options(localconfig: Path, app_id: str) -> str:
    try:
        _root, data = _parse_vdf(localconfig.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""  # unreadable/malformed -> treat as "no launch options", never crash
    node: object = data
    for key in ("Software", "Valve", "Steam", "apps", app_id):
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    if isinstance(node, dict):
        value = node.get("LaunchOptions")
        return str(value) if value is not None else ""
    return ""


# --- LaunchOptions invocation + sanitising ------------------------------------

def _quote_arg(path: Path | str) -> str:
    text = str(path)
    return '"' + text.replace('"', '\\"') + '"'


def _dispatcher_prefix(config_path: Path) -> str:
    """The launcher's dispatch invocation, minus the trailing ``%command%``.

    This is what actually runs the dispatcher (the frozen exe, or the dev module).
    The shim prepends it to Steam's command when the launcher is present.
    """
    if getattr(sys, "frozen", False):
        exe = _quote_arg(Path(sys.executable))
        return f"{exe} --steam-wrapper-dispatch {_quote_arg(config_path)}"
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        python = Path(sys.executable)
    return f"{_quote_arg(python)} -m aoe4replay.steamwrap --dispatch {_quote_arg(config_path)}"


def _bundled_shim_path() -> Path | None:
    """The steamshim.exe shipped with the build (frozen bundle, or source tree)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "steamshim.exe"
        return candidate if candidate.is_file() else None
    candidate = Path(__file__).resolve().parents[2] / "packaging" / "steamshim.exe"
    return candidate if candidate.is_file() else None


def _shim_exe_path(cfg: Config) -> Path:
    return _steam_wrapper_root(cfg) / "steamshim.exe"


def _deploy_shim(cfg: Config) -> Path | None:
    """Copy the shim into the stable %LocalAppData% dir; return its path, or None.

    Only used for packaged (frozen) builds — that's where surviving an app-folder
    deletion matters. Source/dev runs use the direct dispatcher invocation.
    """
    if not getattr(sys, "frozen", False):
        return None
    src = _bundled_shim_path()
    if src is None:
        return None
    dst = _shim_exe_path(cfg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
    except OSError:
        return None
    return dst


def _write_shim_cfg(cfg: Config, check_path: str, prefix: str) -> None:
    """Write the shim config: line 1 = existence check, line 2 = dispatcher prefix.

    UTF-16LE so non-ASCII paths (e.g. localized user folders) round-trip; the shim
    reads it back as wide chars.
    """
    path = _steam_wrapper_root(cfg) / "shim.cfg"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(f"{check_path}\n{prefix}\n", encoding="utf-16-le")
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _is_dispatch_launch_options(value: str) -> bool:
    return (
        "--steam-wrapper-dispatch" in value
        or ("aoe4replay.steamwrap" in value and "--dispatch" in value)
        or "steamshim.exe" in value.lower()
    )


def _is_replay_wrapper_launch_options(value: str) -> bool:
    return _is_dispatch_launch_options(value) or "--steam-wrapper" in value


def _sanitize_original_launch_options(value: str) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if _is_replay_wrapper_launch_options(text):
        return ""
    if "-replay" in lowered and "playback:" in lowered:
        return ""
    return text


# --- dispatch config + active replay request ----------------------------------

def _read_dispatch_config(cfg: Config) -> dict:
    path = _steam_wrapper_dispatch_config(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_dispatch_config(cfg: Config, original_launch_options: str) -> Path:
    root = _steam_wrapper_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    path = _steam_wrapper_dispatch_config(cfg)
    _write_json_atomic(
        path,
        {
            "mode": "dispatch",
            "app_id": cfg.app_id,
            "request": str(_steam_wrapper_request(cfg)),
            "log": str(root / "wrapper.log"),
            "original_launch_options": original_launch_options,
        },
    )
    return path


def _write_active_replay_request(
    cfg: Config,
    launch_dir: Path,
    exe: Path,
    replay_name: str,
    dev: bool,
    runasdate_exe: Path | None = None,
    runasdate_when: datetime | None = None,
) -> Path:
    root = _steam_wrapper_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    game_args = ["-replay", f"playback:{replay_name}"]
    if dev:
        game_args = ["-dev", *game_args]
    path = _steam_wrapper_request(cfg)
    now = time.time()
    request: dict = {
        "token": uuid.uuid4().hex,
        "target": str(exe),
        "cwd": str(launch_dir),
        "app_id": cfg.app_id,
        "game_args": game_args,
        "log": str(root / "wrapper.log"),
        "created_at": now,
        "expires_at": now + _REQUEST_TTL_SECONDS,  # short TTL: no stale-request hijack
    }
    if runasdate_exe is not None and runasdate_when is not None:
        # Same date/time format the launcher has always passed to RunAsDate.
        request["runasdate"] = {
            "exe": str(runasdate_exe),
            "date": runasdate_when.strftime("%d/%m/%Y"),
            "time": runasdate_when.strftime("%H:%M:%S"),
        }
    _write_json_atomic(path, request)
    return path


def _clear_active_replay_request(cfg: Config) -> None:
    with contextlib.suppress(OSError):
        _steam_wrapper_request(cfg).unlink()


def _ensure_steam_wrapper_integration(cfg: Config) -> bool:
    """Install/update the persistent Steam LaunchOptions wrapper.

    Returns True when Steam had to be restarted so the changed LaunchOptions are
    loaded. Once installed, replay launches only write ``active_request.json`` and
    call ``steam -applaunch``; no Steam restart is needed.
    """
    localconfig = _localconfig_path(cfg)
    if not localconfig.is_file():
        raise FileNotFoundError(f"Steam localconfig.vdf was not found: {localconfig}")

    dispatch_config = _steam_wrapper_dispatch_config(cfg)
    prefix = _dispatcher_prefix(dispatch_config)
    shim = _deploy_shim(cfg)
    if shim is not None:
        # LaunchOptions points at the stable shim: it forwards to the launcher when
        # present, or passes Steam's command straight through if the app was deleted
        # (so normal Play never breaks). check_path is the launcher exe itself.
        _write_shim_cfg(cfg, str(Path(sys.executable)), prefix)
        desired = f"{_quote_arg(shim)} %command%"
    else:
        desired = f"{prefix} %command%"
    current = _get_launch_options(localconfig, cfg.app_id)
    existing = _read_dispatch_config(cfg)
    if _is_replay_wrapper_launch_options(current):
        original = _sanitize_original_launch_options(
            str(existing.get("original_launch_options") or "")
        )
    else:
        original = _sanitize_original_launch_options(current)

    _write_dispatch_config(cfg, original)
    if current == desired:
        return False

    print("Installing Steam replay wrapper integration (one-time Steam restart)...")
    for _attempt in range(2):
        _shutdown_steam(cfg)
        # Always bring Steam back, even if writing LaunchOptions fails — otherwise a
        # failed write would leave Steam closed and the user stuck reopening it.
        try:
            _set_launch_options(localconfig, cfg.app_id, desired)
        finally:
            _start_steam(cfg)
        _wait_for_steam_ready()
        # The user may sign into a different account at Steam's picker; re-resolve so we
        # verify against whatever account Steam came back on (and, on a retry, write to
        # it). Then confirm the write actually stuck — on some machines Steam's shutdown
        # flush lands after ours and clobbers it, so retry once if it didn't.
        with contextlib.suppress(Exception):
            localconfig = _localconfig_path(cfg)
        if _get_launch_options(localconfig, cfg.app_id) == desired:
            return True
    raise RuntimeError(
        "Steam keeps resetting the replay launch option after restarting. This is "
        "usually Steam Cloud or a permission issue overriding it — make sure Steam is "
        "online and not running as administrator, then try again. You can also set it "
        "manually in Steam: Age of Empires IV → Properties → Launch Options."
    )


def ensure_steam_wrapper(cfg: Config) -> bool:
    """Install the persistent Steam wrapper (the one-time Steam restart) up front.

    Lets the UI do the restart right after the user confirms it — before the (often
    long) build download — instead of mid-launch at the very end. Idempotent: if the
    wrapper is already installed it returns False without restarting. The later
    per-launch call in launch_replay then becomes a no-op.
    """
    return _ensure_steam_wrapper_integration(cfg)


def wrapper_restart_pending(cfg: Config) -> bool:
    """True if installing the replay wrapper would require the one-time Steam
    restart (LaunchOptions don't yet point at our wrapper). Read-only, best-effort
    — used to warn the user before an old-build replay triggers the restart. Mirrors
    the ``current != desired`` decision in _ensure_steam_wrapper_integration.
    """
    try:
        localconfig = _localconfig_path(cfg)
        if not localconfig.is_file():
            return False
        current = _get_launch_options(localconfig, cfg.app_id)
    except Exception:  # noqa: BLE001 - prediction must never block a launch
        return False
    if getattr(sys, "frozen", False) and _bundled_shim_path() is not None:
        desired = f"{_quote_arg(_shim_exe_path(cfg))} %command%"
    else:
        desired = f"{_dispatcher_prefix(_steam_wrapper_dispatch_config(cfg))} %command%"
    return current != desired


def heal_wrapper_paths(cfg: Config) -> None:
    """Refresh the shim's stored launcher path if the app was moved (or updated).

    The shim lives in %LocalAppData% and survives a move; only the launcher path it
    stores (shim.cfg) goes stale. When stale, normal Play still works (the shim
    falls back to passing Steam's command through), and an old-build replay would
    self-heal on launch — but refreshing here keeps the shim pointed at the live
    launcher immediately. No Steam restart: LaunchOptions still points at the
    (unmoved) shim. Best-effort, packaged builds only, and only when already set up.
    """
    if not getattr(sys, "frozen", False):
        return
    cfg_path = _steam_wrapper_root(cfg) / "shim.cfg"
    if not cfg_path.is_file():
        return
    try:
        text = cfg_path.read_text(encoding="utf-16-le")
    except OSError:
        return
    stored_check = (text.lstrip("﻿").splitlines() or [""])[0]
    current = str(Path(sys.executable))
    if stored_check == current:
        return  # already pointing at the live launcher
    prefix = _dispatcher_prefix(_steam_wrapper_dispatch_config(cfg))
    with contextlib.suppress(OSError):
        _write_shim_cfg(cfg, current, prefix)
        print("Refreshed Steam shim path after the launcher moved/updated.")


# --- temporary user-mods handling (unchanged from the direct-launch design) ----

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


def _wait_for_steam_idle(cfg: Config) -> None:
    """After the game exits, give Steam time to clear its "in-game" state.

    Steam keeps reporting the app as running for several seconds after the process
    is gone; launching a new replay into that gap collides with the stale state
    (long hang, tiny window, etc.). We wait a fixed interval rather than reading
    Steam's registry keys, so the panel re-enables Play only once Steam is free
    without the binary doing registry recon that trips antivirus heuristics.
    """
    time.sleep(_STEAM_SETTLE_SECONDS)


def launch_replay_via_steam(cfg: Config, replay_name: str, keep_mods: bool = False) -> None:
    """Launch a current-build replay *through Steam* (``steam -applaunch``).

    The replay matches the installed build, so Steam launches the live install
    directly with the ``-dev -replay`` arguments — giving the game a real Steam
    session (which a direct spawn lacks, and which crashes mid-match on long
    replays). The persistent wrapper, if installed, sees no active request and
    passes this command straight through.
    """
    ensure_steam_running(cfg)
    mods_state = None if keep_mods else disable_user_mods(cfg)
    try:
        # Drop any stale old-build request so the wrapper passes this current-build
        # replay straight through instead of dispatching an old build.
        _clear_active_replay_request(cfg)
        args = [
            str(cfg.steam_exe), "-applaunch", cfg.app_id,
            "-dev", "-replay", f"playback:{replay_name}",
        ]
        print(f"Launching AOE4 through Steam: {' '.join(args)}")
        subprocess.Popen(args)
        # The game is a child of Steam, not us, so watch the image rather than wait()
        # on our Popen (which is just the steam.exe forwarder and exits immediately).
        _wait_for_game_exit(_GAME_IMAGE)
        _wait_for_steam_idle(cfg)
    finally:
        restore_user_mods(mods_state)


def launch_replay(
    cfg: Config,
    launch_dir: Path,
    replay_name: str,
    when: datetime | None = None,
    use_runasdate: bool = True,
    keep_mods: bool = False,
    dev: bool = True,
) -> None:
    """Launch a reconstructed build through Steam's official AoE4 app session.

    Steam can only ``-applaunch`` the *current* install, so instead of launching
    the historical exe directly (which yields a broken Steam session and a
    mid-match crash), we install a persistent Steam LaunchOptions wrapper. Before
    each launch we write a one-shot ``active_request.json`` describing the
    reconstructed exe (and, for old builds, the RunAsDate shim) and call
    ``steam -applaunch``; the wrapper Steam starts then runs that exe with a real
    Steam app session.
    """
    launch_dir = Path(launch_dir)
    exe = find_executable(launch_dir)
    ensure_steam_running(cfg)
    mods_state = None if keep_mods else disable_user_mods(cfg)
    try:
        restarted = _ensure_steam_wrapper_integration(cfg)

        runasdate_exe = None
        if use_runasdate and when is not None:
            try:
                runasdate_exe = tools.ensure_runasdate(cfg)
                print(f"Launching with RunAsDate shim: {when:%Y-%m-%d %H:%M:%S}")
            except Exception as exc:  # noqa: BLE001 - date shim is best-effort
                print(
                    f"{exc}\nRunAsDate unavailable; launching without the date shim "
                    "(old builds may show date warnings)."
                )
                runasdate_exe = None

        _write_active_replay_request(
            cfg,
            launch_dir,
            exe,
            replay_name,
            dev,
            runasdate_exe=runasdate_exe,
            runasdate_when=when if runasdate_exe is not None else None,
        )
        if restarted:
            print("Steam replay wrapper is installed; future old replays will not restart Steam.")

        args = [str(cfg.steam_exe), "-applaunch", cfg.app_id]
        print(f"Launching reconstructed AOE4 build through Steam: {' '.join(args)}")
        subprocess.Popen(args, creationflags=_NO_WINDOW)
        _wait_for_game_exit(exe.name)
        _wait_for_steam_idle(cfg)
    finally:
        try:
            _clear_active_replay_request(cfg)
        finally:
            restore_user_mods(mods_state)
