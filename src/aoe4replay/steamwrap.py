"""Steam LaunchOptions wrapper entry points.

Steam starts this process as the official AoE4 app through LaunchOptions. In
normal mode it passes Steam's original ``%command%`` through to the live game. If
the launcher has written an active replay request, it starts that reconstructed
build instead while keeping Steam's app launch environment alive.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_FALLBACK_LOG = Path(os.environ.get("TEMP", ".")) / "aoe4-replay-wrapper.log"
_LOG_MAX_BYTES = 1_000_000  # rotate the log past ~1 MB; keep a single .1 backup


def _log(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Every AoE4 launch goes through the dispatcher and appends here, so cap the
    # file: once it passes ~1 MB roll it to wrapper.log.1 and start fresh. Rotation
    # is best-effort so logging never breaks a launch.
    with contextlib.suppress(OSError):
        if log_path.exists() and log_path.stat().st_size >= _LOG_MAX_BYTES:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            with contextlib.suppress(OSError):
                if backup.exists():
                    backup.unlink()
                log_path.rename(backup)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _quote_command(args: list[str]) -> str:
    return subprocess.list2cmdline([str(a) for a in args])


def _process_running(image: str) -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    ).stdout
    return image.lower() in out.lower()


def _split_windows(command_line: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command_line)
    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    argv = command_line_to_argv(command_line, ctypes.byref(argc))
    if not argv:
        return shlex.split(command_line, posix=False)
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        local_free(argv)


def _run_process(
    cmd: list[str] | str,
    cwd: Path | None,
    log_path: Path,
    *,
    shell: bool = False,
    wait_image: str | None = None,
) -> int:
    _log(log_path, f"launching={cmd!r}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=os.environ.copy(),
            creationflags=_NO_WINDOW,
            shell=shell,
        )
    except OSError as exc:
        _log(log_path, f"launch failed: {exc}")
        return 2
    _log(log_path, f"child_pid={proc.pid}")
    if wait_image:
        appeared = False
        deadline = time.time() + 180
        child_exited_at: float | None = None
        while time.time() < deadline:
            running = _process_running(wait_image)
            if running:
                appeared = True
                break
            if proc.poll() is not None:
                if child_exited_at is None:
                    child_exited_at = time.time()
                elif time.time() - child_exited_at > 10:
                    break
            else:
                child_exited_at = None
            time.sleep(1)
        if appeared:
            _log(log_path, f"waiting_for_image={wait_image!r}")
            while _process_running(wait_image):
                time.sleep(2)
        else:
            _log(log_path, f"wait_image_not_observed={wait_image!r}")
    rc = proc.wait()
    _log(log_path, f"child_returncode={rc}")
    return int(rc or 0)


def _run_target(data: dict, steam_command: list[str] | None = None) -> int:
    target = Path(data["target"])
    cwd = Path(data["cwd"])
    app_id = str(data["app_id"])
    game_args = [str(x) for x in data.get("game_args", [])]
    log_path = Path(data["log"])

    _log(log_path, f"wrapper argv={sys.argv!r}")
    if steam_command:
        _log(log_path, f"steam command={steam_command!r}")
    _log(log_path, f"cwd={Path.cwd()}")
    for key in ("SteamAppId", "SteamGameId", "SteamOverlayGameId", "SteamClientLaunch", "SteamEnv"):
        _log(log_path, f"env {key}={os.environ.get(key)!r}")

    if not target.is_file():
        _log(log_path, f"target missing: {target}")
        return 2
    cwd.mkdir(parents=True, exist_ok=True)
    # Harmless when the Steam environment is already present, but useful if the
    # game asks SteamAPI for the app id before consuming the environment.
    (cwd / "steam_appid.txt").write_text(app_id, encoding="ascii")

    runasdate = data.get("runasdate")
    if isinstance(runasdate, dict):
        runasdate_exe = Path(str(runasdate.get("exe") or ""))
        fake_date = str(runasdate.get("date") or "").strip()
        fake_time = str(runasdate.get("time") or "12:00:00").strip()
        if not runasdate_exe.is_file():
            _log(log_path, f"runasdate missing: {runasdate_exe}")
            return 2
        if not fake_date:
            _log(log_path, "runasdate date missing")
            return 2
        _log(log_path, f"runasdate={runasdate_exe} fake={fake_date} {fake_time}")
        # Match the launcher's historical RunAsDate invocation exactly: /movetime
        # with /startin, faking the clock for the whole session (no /returntime).
        return _run_process(
            [
                str(runasdate_exe),
                "/movetime",
                "/startin",
                str(cwd),
                fake_date,
                fake_time,
                str(target),
                *game_args,
            ],
            cwd,
            log_path,
            wait_image=target.name,
        )

    return _run_process([str(target), *game_args], cwd, log_path)


def run_config(config_path: Path, steam_command: list[str] | None = None) -> int:
    """Run the one-shot target game described by ``config_path``."""
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return _run_target(data, steam_command)


def _active_request(dispatch: dict, log_path: Path) -> dict | None:
    raw = dispatch.get("request")
    if not raw:
        return None
    request_path = Path(str(raw))
    try:
        data = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expires_at = float(data.get("expires_at") or 0)
    if expires_at and expires_at < time.time():
        _log(log_path, f"active request expired: {request_path}")
        with contextlib.suppress(OSError):
            request_path.unlink()
        return None
    with contextlib.suppress(OSError):
        request_path.unlink()
    return data


def _passthrough_command(dispatch: dict, steam_command: list[str], log_path: Path) -> int:
    original = str(dispatch.get("original_launch_options") or "").strip()
    if not steam_command:
        _log(log_path, "no steam command was supplied for passthrough")
        return 2
    if not original:
        return _run_process(steam_command, None, log_path)

    base = _quote_command(steam_command)
    if "%command%" in original:
        command_line = original.replace("%command%", base)
        return _run_process(command_line, None, log_path)

    try:
        extra = _split_windows(original)
    except ValueError as exc:
        _log(log_path, f"could not parse original launch options {original!r}: {exc}")
        extra = []
    return _run_process([*steam_command, *extra], None, log_path)


def run_dispatch(config_path: Path, steam_command: list[str] | None = None) -> int:
    """Run an active replay request, or pass Steam's normal command through."""
    cmd = [str(x) for x in (steam_command or [])]
    try:
        dispatch = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log(_FALLBACK_LOG, f"dispatch config unavailable: {config_path} ({exc})")
        return _passthrough_command({}, cmd, _FALLBACK_LOG)
    if not isinstance(dispatch, dict):
        _log(_FALLBACK_LOG, f"dispatch config is not an object: {config_path}")
        return _passthrough_command({}, cmd, _FALLBACK_LOG)
    # Tolerate a missing/blank "log" so a partially-corrupt config can't crash us.
    log_path = Path(str(dispatch.get("log") or _FALLBACK_LOG))
    _log(log_path, f"dispatch argv={sys.argv!r}")
    _log(log_path, f"dispatch config={config_path}")

    request = _active_request(dispatch, log_path)
    if request is not None:
        _log(log_path, f"active request token={request.get('token')!r}")
        # A corrupt request (missing/invalid keys) must fall back to a normal launch,
        # never crash — otherwise even normal Steam Play would fail.
        try:
            return _run_target(request, steam_command)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _log(log_path, f"active request unusable ({exc}); passing through")

    _log(log_path, "no active replay request; passing through to Steam command")
    return _passthrough_command(dispatch, cmd, log_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--dispatch")
    args, rest = parser.parse_known_args(argv)
    if args.dispatch:
        return run_dispatch(Path(args.dispatch), rest)
    if args.config:
        return run_config(Path(args.config), rest)
    raise SystemExit("--config or --dispatch is required")


if __name__ == "__main__":
    raise SystemExit(main())
