"""Configuration loading.

Defaults live here; a ``config.local.toml`` (preferred) or ``config.toml`` at the
project root overlays them. Local config is gitignored so a Steam username never
gets committed. Relative paths are resolved against the project root.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Built-in defaults, mirrored by config.example.toml for documentation.
_DEFAULTS: dict[str, dict[str, str]] = {
    "paths": {
        "steam_install": "",  # empty = auto-detect via Steam (any drive / library folder)
        "steam_exe": "",  # empty = auto-detect via Steam
        "repo": "repo",
        "depotdownloader": "tools/DepotDownloader.exe",
        "restic": "tools/restic.exe",
        "runasdate": "tools/RunAsDate.exe",
        "documents": "",  # empty = auto-detect the real Documents folder (handles OneDrive)
    },
    "steam": {"username": ""},
    "app": {"app_id": "1466860", "default_depot": "1466861"},
}

_CONFIG_FILENAMES = ("config.local.toml", "config.toml")


@dataclass(frozen=True)
class Config:
    """Resolved, absolute configuration for one invocation."""

    project_root: Path
    steam_install: Path
    steam_exe: Path
    repo: Path
    depotdownloader: Path
    restic: Path
    runasdate: Path
    documents: Path
    steam_username: str
    app_id: str
    default_depot: str

    @property
    def playback_dir(self) -> Path:
        return self.documents / "playback"

    @property
    def downloads_dir(self) -> Path:
        return self.project_root / "downloads"

    @property
    def mods_dir(self) -> Path:
        return self.documents / "mods"


def _looks_like_root(path: Path) -> bool:
    return (path / "data" / "aoe4-build-map.json").is_file() or (path / "pyproject.toml").is_file()


def _writable_dir(path: Path) -> bool:
    """True if we can create ``path`` and write a file inside it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("", encoding="ascii")
        probe.unlink()
        return True
    except OSError:
        return False


def _seed_runtime_data(root: Path) -> None:
    """First run of the packaged exe: copy the files the source tree ships
    (config + an initial build map / manifest history) out of the PyInstaller
    bundle into the writable ``root``. The build map then self-updates from the
    network at startup, so the bundled copy is only a starting point. Existing
    files are never overwritten, so user edits and accumulated data survive.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    seed = Path(meipass) / "seed_data"
    (root / "data").mkdir(parents=True, exist_ok=True)
    pairs = [
        (seed / "config.example.toml", root / "config.example.toml"),
        (seed / "config.example.toml", root / "config.local.toml"),
        (seed / "aoe4-build-map.json", root / "data" / "aoe4-build-map.json"),
        (seed / "aoe4-manifest-history.json", root / "data" / "aoe4-manifest-history.json"),
    ]
    for src, dst in pairs:
        if src.is_file() and not dst.exists():
            with contextlib.suppress(OSError):
                shutil.copyfile(src, dst)


def _frozen_root() -> Path:
    """Writable data root for the packaged exe.

    Velopack (our installer / auto-update framework) runs the app from
    ``<RootAppDir>/current/`` and **replaces that whole ``current`` folder on
    every update**, so all persistent data (config, build map, the multi-GB
    restic repo, downloads, tools) must live in ``RootAppDir`` — one level up —
    which Velopack preserves across updates. We detect a Velopack install by its
    ``sq.version`` marker next to the exe (or ``Update.exe`` in RootAppDir).

    Without Velopack (legacy plain-PyInstaller portable build) data stays next to
    the exe as before. If neither location is writable (e.g. under Program Files),
    fall back to ``%LocalAppData%\\AoE4ReplayLauncher``.
    """
    exe_dir = Path(sys.executable).resolve().parent
    root_app_dir = exe_dir.parent
    is_velopack = (exe_dir / "sq.version").is_file() or (root_app_dir / "Update.exe").is_file()
    if is_velopack and _writable_dir(root_app_dir):
        return root_app_dir
    if _writable_dir(exe_dir):
        return exe_dir
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    root = Path(base) / "AoE4ReplayLauncher"
    root.mkdir(parents=True, exist_ok=True)
    return root


def find_project_root() -> Path:
    """Locate the project root: where data/, config and the restic repo live.

    With an editable install the package sits in ``<repo>/src/aoe4replay`` so the
    third parent is the repo. With a plain ``pip install .`` the package is copied
    into site-packages (its third parent would be ``.venv/Lib``), so fall back to
    an env override (set by the launcher) or a search up from the working
    directory for the project marker. A frozen (PyInstaller) build keeps its data
    next to the exe (or under %APPDATA%) and seeds it from the bundle on first run.
    """
    override = os.environ.get("AOE4REPLAY_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    if getattr(sys, "frozen", False):
        root = _frozen_root()
        _seed_runtime_data(root)
        return root

    # Editable install: src/aoe4replay/config.py -> parents[2] is the repo root.
    editable = Path(__file__).resolve().parents[2]
    if _looks_like_root(editable):
        return editable

    # Plain install (copied to site-packages): the user runs from the cloned repo.
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _looks_like_root(candidate):
            return candidate
    return editable  # last resort, keeps the previous behaviour


def _resolve(root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


# AoE4 stores its data under the user's real Documents folder, which Windows can
# redirect to OneDrive (and localise, e.g. "Belgeler"). A hardcoded ~/Documents
# misses that, so the replay lands where the game never looks. These resolve the
# actual folder. An empty config value (or the old hardcoded default) auto-detects.
_OLD_DEFAULT_DOCUMENTS = "~/Documents/My Games/Age of Empires IV"
_GAME_DOCS_SUBPATH = ("My Games", "Age of Empires IV")


def _known_documents() -> Path | None:
    """The real Documents folder via the Windows known-folder API (a single
    instant lookup — no scanning — that honours OneDrive redirection)."""
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        folder = _GUID(
            0xFDD39AD0, 0x238F, 0x46AF,
            (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
        )
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder), 0, None, ctypes.byref(ptr)
        ) != 0:
            return None
        try:
            return Path(ptr.value) if ptr.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(ptr)
    except Exception:  # noqa: BLE001 - any failure falls through to other methods
        return None


def _registry_documents() -> Path | None:
    """The Documents folder from the user shell-folders registry key (fallback)."""
    try:
        import winreg

        sub = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as handle:
            value, _ = winreg.QueryValueEx(handle, "Personal")
        return Path(os.path.expandvars(value))
    except OSError:
        return None


def _default_documents() -> Path:
    """``<real Documents>/My Games/Age of Empires IV`` — where the game actually
    stores its data, even when Documents is redirected to OneDrive."""
    base = _known_documents() or _registry_documents() or (Path.home() / "Documents")
    return base.joinpath(*_GAME_DOCS_SUBPATH)


# The game and Steam may live on any drive / in any Steam library folder, so a
# hardcoded "C:\Program Files (x86)\Steam\..." misses most non-default setups.
# These find the real locations from Steam's own registry + libraryfolders.vdf.
_OLD_DEFAULT_STEAM_INSTALL = "C:/Program Files (x86)/Steam/steamapps/common/Age of Empires IV"
_OLD_DEFAULT_STEAM_EXE = "C:/Program Files (x86)/Steam/steam.exe"


def _steam_path() -> Path | None:
    """Steam's install directory from the registry (works on any drive)."""
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
    return None


def _steam_libraries(steam: Path) -> list[Path]:
    """All Steam library folders: the main install plus those in libraryfolders.vdf."""
    import re

    libraries = [steam]
    for vdf in (
        steam / "steamapps" / "libraryfolders.vdf",
        steam / "config" / "libraryfolders.vdf",
    ):
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Both vdf shapes store library paths as drive-rooted quoted values.
        for raw in re.findall(r'"[^"]*"\s+"([A-Za-z]:[^"]*)"', text):
            libraries.append(Path(raw.replace("\\\\", "\\")))
        break
    seen: set[str] = set()
    unique: list[Path] = []
    for lib in libraries:
        key = str(lib).lower()
        if key not in seen:
            seen.add(key)
            unique.append(lib)
    return unique


def _find_game_install(app_id: str) -> Path | None:
    """Locate the installed game across Steam's library folders, or None."""
    import re

    steam = _steam_path()
    if steam is None:
        return None
    for lib in _steam_libraries(steam):
        acf = lib / "steamapps" / f"appmanifest_{app_id}.acf"
        try:
            text = acf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r'"installdir"\s+"([^"]+)"', text)
        if match:
            return lib / "steamapps" / "common" / match.group(1)
    return None


def _default_steam_install(app_id: str) -> Path:
    """The installed game folder (any drive/library), or the legacy default."""
    return _find_game_install(app_id) or Path(_OLD_DEFAULT_STEAM_INSTALL)


def _default_steam_exe() -> Path:
    """steam.exe next to the detected Steam install, or the legacy default."""
    steam = _steam_path()
    if steam is not None and (steam / "steam.exe").is_file():
        return steam / "steam.exe"
    return Path(_OLD_DEFAULT_STEAM_EXE)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_config_file(root: Path, explicit: Path | None) -> dict:
    # An explicitly supplied --config must exist (and parse) — a typo'd path must
    # fail loudly, not silently fall back to defaults / project-local discovery.
    if explicit is not None and not explicit.is_file():
        raise FileNotFoundError(f"Config file not found: {explicit}")
    candidates = [explicit] if explicit else [root / name for name in _CONFIG_FILENAMES]
    for path in candidates:
        if path and path.is_file():
            with path.open("rb") as fh:
                return tomllib.load(fh)
    return {}


def set_steam_username(root: Path, username: str) -> None:
    """Persist the Steam username into config.local.toml (created from the example
    if missing), updating only the [steam] username so other settings are kept."""
    import re

    path = root / "config.local.toml"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        example = root / "config.example.toml"
        if example.is_file():
            text = example.read_text(encoding="utf-8")
        else:
            text = '[steam]\nusername = ""\n'
    line = f'username = "{username}"'
    if re.search(r"(?m)^\s*username\s*=.*$", text):
        text = re.sub(r"(?m)^\s*username\s*=.*$", line, text, count=1)
    elif "[steam]" in text:
        text = text.replace("[steam]", f"[steam]\n{line}", 1)
    else:
        text = text.rstrip() + f"\n\n[steam]\n{line}\n"
    path.write_text(text, encoding="utf-8")


def set_path_overrides(root: Path, overrides: dict[str, str]) -> None:
    """Persist ``[paths]`` overrides into config.local.toml so a user-picked folder
    sticks. Written with forward slashes in basic strings (no backslash-escaping
    headaches); other settings are preserved."""
    import re

    path = root / "config.local.toml"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        example = root / "config.example.toml"
        text = example.read_text(encoding="utf-8") if example.is_file() else "[paths]\n"
    for key, value in overrides.items():
        line = f'{key} = "{value.replace(chr(92), "/")}"'
        pattern = rf"(?m)^\s*{re.escape(key)}\s*=.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, line, text, count=1)
        elif "[paths]" in text:
            text = text.replace("[paths]", f"[paths]\n{line}", 1)
        else:
            text = f"[paths]\n{line}\n{text}"
    path.write_text(text, encoding="utf-8")


def load(config_path: Path | None = None) -> Config:
    """Load configuration, overlaying a local config file onto the defaults."""
    root = find_project_root()
    data = _deep_merge(_DEFAULTS, _read_config_file(root, config_path))

    paths = data["paths"]
    app_id = str(data["app"]["app_id"])

    # Auto-detect machine-specific paths unless the user set an explicit value
    # (empty or the old hardcoded default both mean "auto-detect").
    documents_cfg = paths["documents"]
    documents = (
        _default_documents()
        if documents_cfg in ("", _OLD_DEFAULT_DOCUMENTS)
        else _resolve(root, documents_cfg)
    )
    steam_install_cfg = paths["steam_install"]
    steam_install = (
        _default_steam_install(app_id)
        if steam_install_cfg in ("", _OLD_DEFAULT_STEAM_INSTALL)
        else _resolve(root, steam_install_cfg)
    )
    steam_exe_cfg = paths["steam_exe"]
    steam_exe = (
        _default_steam_exe()
        if steam_exe_cfg in ("", _OLD_DEFAULT_STEAM_EXE)
        else _resolve(root, steam_exe_cfg)
    )
    return Config(
        project_root=root,
        steam_install=steam_install,
        steam_exe=steam_exe,
        repo=_resolve(root, paths["repo"]),
        depotdownloader=_resolve(root, paths["depotdownloader"]),
        restic=_resolve(root, paths["restic"]),
        runasdate=_resolve(root, paths["runasdate"]),
        documents=documents,
        steam_username=str(data["steam"]["username"]),
        app_id=app_id,
        default_depot=str(data["app"]["default_depot"]),
    )
