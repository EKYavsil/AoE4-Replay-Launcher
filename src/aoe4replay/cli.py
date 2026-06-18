"""Command-line interface for AoE4 Replay Launcher."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import __version__, config

app = typer.Typer(
    add_completion=False,
    help="Reconstruct historical AoE4 builds and play old replays against them.",
)

ReplayArg = Annotated[
    Path, typer.Argument(exists=True, dir_okay=False, help="Path to a .rec replay")
]
ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Config file override")]


def _load(config_path: Path | None) -> config.Config:
    return config.load(config_path)


# Errors we expect to surface to the user as a clean one-line message rather
# than a traceback.
_EXPECTED_ERRORS = (RuntimeError, ValueError, LookupError, FileNotFoundError, OSError)


def _fail(message: str) -> None:
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def watch(
    replay: ReplayArg,
    config_path: ConfigOpt = None,
    no_launch: Annotated[bool, typer.Option("--no-launch", help="Do not launch")] = False,
) -> None:
    """Reconstruct the matching build and play REPLAY."""
    from . import service

    try:
        service.watch_replay(_load(config_path), replay, no_launch=no_launch)
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))


@app.command()
def add(replay: ReplayArg, config_path: ConfigOpt = None) -> None:
    """Download and store the build for REPLAY without launching."""
    from . import service

    try:
        build = service.add_build(_load(config_path), replay)
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))
    typer.echo(f"Stored build {build.build_id}.")


@app.command()
def ingest(
    cache_dir: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Existing delta_* cache dir")
    ],
    config_path: ConfigOpt = None,
) -> None:
    """One-time import of an existing delta_* build cache into restic (read-only)."""
    from . import service

    try:
        count = service.ingest_cache(_load(config_path), cache_dir)
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))
    typer.echo(f"Stored {count} new build(s).")


@app.command()
def reindex(config_path: ConfigOpt = None) -> None:
    """Rebuild the content index (which stored files can be reused across builds)."""
    from . import service

    try:
        count = service.reindex(_load(config_path))
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))
    typer.echo(f"Indexed {count} unique files.")


@app.command(name="list")
def list_builds(config_path: ConfigOpt = None) -> None:
    """List stored builds (restic snapshots)."""
    from . import resticrepo

    cfg = _load(config_path)
    try:
        resticrepo.ensure_repo(cfg)
        snaps = resticrepo.list_builds(cfg)
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))
    if not snaps:
        typer.echo("No builds stored yet.")
        return
    for snap in snaps:
        typer.echo(f"{snap.short_id}  {snap.build_id or '(untagged)'}")


@app.command(name="harvest-versions")
def harvest_versions(config_path: ConfigOpt = None) -> None:
    """Record each stored build's exe version in the build map (maintainer one-off).

    Lets replays resolve by version (locale-independent) instead of by date.
    """
    from . import service

    cfg = _load(config_path)
    try:
        count = service.harvest_versions(cfg)
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))
    typer.echo(f"Harvested {count} version(s).")


@app.command()
def check(config_path: ConfigOpt = None) -> None:
    """Verify restic repository integrity."""
    from . import resticrepo

    cfg = _load(config_path)
    try:
        resticrepo.ensure_repo(cfg)
        ok = resticrepo.check(cfg)
    except _EXPECTED_ERRORS as exc:
        _fail(str(exc))
    typer.echo("Repository OK." if ok else "Repository check FAILED.")
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def panel(config_path: ConfigOpt = None) -> None:
    """Open the replay download/launch panel (GUI)."""
    from . import panel as panel_module

    panel_module.run(_load(config_path))


_GITHUB_REPO_URL = "https://github.com/EKYavsil/AoE4-Replay-Launcher"


@app.command()
def update(
    source: Annotated[
        str | None,
        typer.Option("--source", help="Override update source (URL or local dir; for testing)"),
    ] = None,
) -> None:
    """Check for and install an application update (packaged release only)."""
    try:
        import velopack
    except ImportError:
        _fail("Updates are only available in the packaged release.")
    src = source if source else velopack.GithubSource(_GITHUB_REPO_URL, None, False)
    try:
        manager = velopack.UpdateManager(src)
        info = manager.check_for_updates()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Update check failed: {exc}")
    if not info:
        typer.echo("You're on the latest version.")
        return
    typer.echo("Downloading update...")
    manager.download_updates(info)
    typer.echo("Applying update and restarting...")
    manager.apply_updates_and_restart(info)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
