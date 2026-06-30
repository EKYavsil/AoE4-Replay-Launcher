"""PyInstaller entry point for the packaged AoE4 Replay Launcher.

Double-clicking the built ``AoE4-Replay-Launcher.exe`` opens the desktop panel.
If command-line arguments are passed, the regular CLI is dispatched instead, so
the same exe can also run ``watch``/``add``/``list``/etc. from a terminal.

Velopack (the installer/auto-update framework) must be the very first thing that
runs in the main process: when the exe is invoked by Velopack for an install or
update hook it handles that and exits/restarts. During a normal launch it returns
immediately and we continue to the panel.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    try:
        import velopack

        velopack.App().run()
    except ImportError:
        pass  # source/dev runs (and the legacy build) don't ship velopack

    # When Steam launches AoE4 through our LaunchOptions wrapper, the exe runs in
    # dispatch mode: start a pending reconstructed-build replay, or pass Steam's
    # original %command% through so normal Play is unaffected.
    if len(sys.argv) > 1 and sys.argv[1] == "--steam-wrapper-dispatch":
        from aoe4replay import steamwrap

        if len(sys.argv) < 3:
            raise SystemExit("--steam-wrapper-dispatch requires a dispatch config path")
        raise SystemExit(steamwrap.run_dispatch(Path(sys.argv[2]), sys.argv[3:]))

    from aoe4replay import config

    if len(sys.argv) > 1:
        from aoe4replay.cli import app

        app()
        return

    from aoe4replay import panel

    panel.run(config.load())


if __name__ == "__main__":
    main()
