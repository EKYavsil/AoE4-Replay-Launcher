"""restic wrapper: the deduplicated build store.

Each build's delta is stored as a snapshot tagged with its build id. The repo is
created automatically on first use (bootstrap).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import tools
from .config import Config

_HOST = "aoe4"  # stable host so snapshots group cleanly regardless of machine name
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console pop-ups under pythonw


@dataclass(frozen=True)
class Snapshot:
    short_id: str
    build_id: str
    paths: tuple[str, ...]


def _password_file(cfg: Config) -> Path:
    return cfg.project_root / ".restic-password"


def _ensure_password(cfg: Config) -> Path:
    pf = _password_file(cfg)
    if pf.exists():
        return pf
    pf.parent.mkdir(parents=True, exist_ok=True)
    # Atomic election so two first-run processes can't write two different
    # passwords (which would lock the other out of the repo): each writes its own
    # temp, then hard-links it into place — only the first link wins, the rest fail
    # and keep the winner's password.
    tmp = pf.with_name(f"{pf.name}.{os.getpid()}.new")
    tmp.write_text(secrets.token_hex(24), encoding="ascii")
    try:
        os.link(tmp, pf)
    except FileExistsError:
        pass  # another process created the password first — use theirs
    except OSError:  # hard links unsupported here -> best-effort create
        with contextlib.suppress(OSError):
            os.replace(tmp, pf)
            return pf
    with contextlib.suppress(OSError):
        tmp.unlink()
    return pf


def _env(cfg: Config) -> dict[str, str]:
    return {
        **os.environ,
        "RESTIC_REPOSITORY": str(cfg.repo),
        "RESTIC_PASSWORD_FILE": str(_ensure_password(cfg)),
    }


def _run(
    cfg: Config, args: list[str], *, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess:
    exe = tools.ensure_restic(cfg)
    return subprocess.run(
        [str(exe), *args],
        env=_env(cfg),
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",  # restic emits UTF-8 JSON; decode it as such, not the OS code page
        errors="replace",  # (a non-ASCII repo path would otherwise mis-decode -> index misses)
        creationflags=_NO_WINDOW,
    )


def _repo_state(cfg: Config) -> str:
    """``"ready"`` if the repo opens, ``"absent"`` if it isn't initialised yet.

    Raises on every other failure (wrong password, corruption, locked, permission,
    offline drive) instead of mistaking it for "not initialised" and silently
    creating a second empty repo over a real but unreadable one.
    """
    result = _run(cfg, ["snapshots", "--json"], capture=True, check=False)
    if result.returncode == 0:
        return "ready"
    text = ((result.stderr or "") + (result.stdout or "")).strip()
    low = text.lower()
    # restic exit code 10 == "repository does not exist"; match the message too for
    # older restic builds that don't set the dedicated code.
    if result.returncode == 10 or any(
        s in low for s in ("does not exist", "unable to open config", "no such file")
    ):
        return "absent"
    raise RuntimeError(
        f"Could not open the restic repository at {cfg.repo}.\n{text[:600]}"
    )


def ensure_repo(cfg: Config) -> None:
    """Create and initialise the restic repo if it does not exist yet."""
    cfg.repo.parent.mkdir(parents=True, exist_ok=True)
    _ensure_password(cfg)
    if _repo_state(cfg) == "absent":
        _run(cfg, ["init"])


def _snapshots_json(cfg: Config, build_id: str | None = None) -> list[dict]:
    args = ["snapshots", "--json"]
    if build_id:
        args += ["--tag", build_id]
    result = _run(cfg, args, capture=True)
    return json.loads(result.stdout or "[]")


def has_build(cfg: Config, build_id: str) -> bool:
    return len(_snapshots_json(cfg, build_id)) > 0


def backup_delta(cfg: Config, build_id: str, delta_dir: Path) -> None:
    ensure_repo(cfg)
    # Never create an empty snapshot: it would later read as "build already stored"
    # (has_build) while containing no files, blocking a real ingest. Skip instead.
    if not any(p.is_file() for p in Path(delta_dir).rglob("*")):
        print(f"Nothing to store for {build_id} (empty delta); skipping restic backup.")
        return
    _run(cfg, ["backup", "--tag", build_id, "--host", _HOST, str(delta_dir)])


def list_builds(cfg: Config) -> list[Snapshot]:
    out = []
    for snap in _snapshots_json(cfg):
        tags = snap.get("tags") or []
        out.append(
            Snapshot(
                short_id=snap.get("short_id", snap["id"][:8]),
                build_id=tags[0] if tags else "",
                paths=tuple(snap.get("paths", [])),
            )
        )
    return out


def _locate_restored(target: Path, original_path: str) -> Path:
    """Find the delta root inside a restore target.

    restic recreates the snapshot's original (absolute) path under ``target``;
    the exact prefix is version/OS dependent, so we locate by the source folder
    name instead of guessing the prefix.
    """
    basename = original_path.replace("\\", "/").rstrip("/").split("/")[-1]
    for root, dirs, files in os.walk(target):
        # Match the source folder by name; its files may live in subfolders, so
        # do not require direct files here (only that it is not empty).
        if Path(root).name == basename and (dirs or files):
            return Path(root)
    raise FileNotFoundError(f"Could not locate restored delta '{basename}' under {target}")


def restore_snapshot(
    cfg: Config, snapshot_id: str, target_dir: Path, original_path: str
) -> Path | None:
    """Restore a whole snapshot; return its located root (None if empty)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    _run(cfg, ["restore", snapshot_id, "--target", str(target_dir)], capture=True, check=False)
    try:
        return _locate_restored(target_dir, original_path)
    except FileNotFoundError:
        return None


def restore_build(cfg: Config, build_id: str, target_dir: Path) -> list[Path]:
    """Restore every snapshot of a build into ``target_dir``.

    A build can have several snapshots: its base delta plus supplements added
    later when the live install drifted. Each is restored and located by its
    source folder name; the returned roots are the source dirs for compose.

    restic can exit non-zero for benign metadata-only problems (e.g. Windows
    denying a timestamp write on a synthesised intermediate directory) even
    though every file's content restored fine. We don't trust the exit code:
    success is verified by locating the restored trees, and compose surfaces any
    genuinely missing file.
    """
    snaps = _snapshots_json(cfg, build_id)
    if not snaps:
        raise LookupError(f"No stored build tagged {build_id!r}.")
    target_dir.mkdir(parents=True, exist_ok=True)

    roots: list[Path] = []
    for snap in snaps:
        _run(cfg, ["restore", snap["id"], "--target", str(target_dir)], capture=True, check=False)
        original = (snap.get("paths") or [""])[0]
        with contextlib.suppress(FileNotFoundError):
            roots.append(_locate_restored(target_dir, original))

    # An empty result is legitimate: a build fully covered by the live install
    # has an empty delta. compose() validates that every needed file has a
    # source (delta / supplement / seed) and raises clearly if one is missing.
    return roots


def check(cfg: Config) -> bool:
    return _run(cfg, ["check"], check=False).returncode == 0


def stored_form(path: str) -> str:
    """The path as restic records/lists it: 'C:\\a\\b' -> '/C/a/b'."""
    return "/" + path.replace("\\", "/").replace(":", "")


def all_snapshots(cfg: Config) -> list[dict]:
    return _snapshots_json(cfg)


def snapshot_id_for_path(cfg: Config, build_id: str, source_path: Path) -> str | None:
    """Short id of the latest snapshot of ``build_id`` whose source is ``source_path``."""
    want = str(Path(source_path))
    found = None
    for snap in _snapshots_json(cfg, build_id):
        paths = snap.get("paths") or []
        if paths and str(Path(paths[0])) == want:
            found = snap.get("short_id", snap["id"][:8])
    return found


def list_snapshot_files(cfg: Config, snapshot_id: str) -> list[tuple[str, int]]:
    """Return (stored_path, size) for every file in a snapshot."""
    result = _run(cfg, ["ls", snapshot_id, "--json"], capture=True, check=False)
    files: list[tuple[str, int]] = []
    for line in (result.stdout or "").splitlines():
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        if node.get("type") == "file":
            files.append((node["path"], int(node.get("size", 0))))
    return files


def _run_restore_streaming(cfg: Config, args: list[str], progress: Callable[[float], None]) -> None:
    """Run restic with ``--json`` and forward its restore ``percent_done`` (0-100).

    Restoring a multi-GB build is the slow part of the build phase; streaming the
    progress keeps the panel from sitting at 0% until it suddenly finishes.
    """
    exe = tools.ensure_restic(cfg)
    proc = subprocess.Popen(  # noqa: S603
        [str(exe), *args],
        env=_env(cfg),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            msg = json.loads(line)
            if msg.get("message_type") == "status" and isinstance(
                msg.get("percent_done"), (int, float)
            ):
                progress(float(msg["percent_done"]) * 100.0)
    proc.wait()


def _snapshot_root(cfg: Config, snapshot_id: str) -> str | None:
    """The snapshot's source dir in restic's stored form (e.g. ``/C/.../delta_X``),
    used as the restore subpath. None if the snapshot can't be found."""
    for snap in _snapshots_json(cfg):
        if snap.get("short_id") == snapshot_id or str(snap.get("id", "")).startswith(snapshot_id):
            paths = snap.get("paths") or []
            if paths:
                return stored_form(paths[0])
    return None


def restore_paths(
    cfg: Config,
    snapshot_id: str,
    stored_paths: list[str],
    target_dir: Path,
    progress: Callable[[float], None] | None = None,
) -> dict[str, Path]:
    """Restore specific files (by stored path) into ``target_dir``; returns
    ``{stored_path: restored file Path}``.

    Restores *from the snapshot's source dir as root* (restic's ``<id>:<subpath>``
    syntax) so files land flat in the target and the system-path prefix
    (``C/Users/...``) is never recreated — restic gives those dirs the live
    system's restrictive ACLs, which a non-admin user then can't delete (they
    pile up as empty shells). Falls back to the old full-path restore if the
    snapshot's root can't be determined. Tolerant of restic's benign metadata
    exit codes. ``progress`` (if given) receives the restore percentage (0-100).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    root = _snapshot_root(cfg, snapshot_id)
    if root:
        # Strip the snapshot-root prefix from real stored paths so they land flat;
        # leave glob patterns (which don't start with the root) untouched.
        def _rel(sp: str) -> str:
            return sp[len(root):] if sp.startswith(root) else sp

        spec = f"{snapshot_id}:{root}"
        includes = ["/" + _rel(sp).lstrip("/") for sp in stored_paths]
        result = {sp: target_dir / _rel(sp).lstrip("/") for sp in stored_paths}
    else:  # fallback: recreate the full path under target (old behaviour)
        spec = snapshot_id
        includes = list(stored_paths)
        result = {sp: target_dir / sp.lstrip("/") for sp in stored_paths}

    fd, inc = tempfile.mkstemp(suffix=".include", prefix="restic_inc_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(includes))
        args = ["restore", spec, "--target", str(target_dir), "--include-file", inc]
        if progress is not None:
            _run_restore_streaming(cfg, [*args, "--json"], progress)
        else:
            _run(cfg, args, capture=True, check=False)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(inc)
    return result
