"""One honest progress event for the whole playback flow.

The panel's notification area draws a single right-aligned string, so every
phase has to describe itself in one place: what it is doing and — only when the
work is genuinely measurable — how far along it is.

Three rules this module exists to enforce:

* **Only measured percentages.** ``pct`` stays ``None`` unless the producer
  counted real bytes or real files. Nothing here invents, extrapolates or times
  a number, so a phase that cannot be measured says what it is doing and lets
  the panel animate instead of showing a fictional percentage.
* **Monotonic inside a phase.** A download that reconnects and restarts must not
  drag the number backwards: a percentage that falls reads as broken, which is
  worse than one that sits still.
* **Throttled at the producer.** The panel marshals every event onto the Tk
  event loop, so a per-block report from an 18 GB copy would flood it and freeze
  the very window this is meant to keep responsive. Updates are dropped here, on
  the worker thread, before they ever reach the UI.

There are deliberately no step numbers ("3/9"). Which phases a run goes through
is only known as it goes — a scan can remove the download and restore phases
entirely — so a total printed up front would be wrong, and one that changes
mid-run reads as a bug.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Phase:
    """A named stretch of work, with the user-facing wording it is shown as.

    ``cancellable`` drives the ✕ button: it is true only where the worker really
    does check the cancel event, so the button is never offered for work that
    would ignore it. ``animated`` is false for a line that reports an outcome
    rather than work in progress — a moving marker beside it would be a lie.
    ``alert`` marks a line that asks the user to act; the panel draws it in the
    warning colour instead of the progress colour.
    """

    key: str
    label: str
    cancellable: bool = False
    animated: bool = True
    alert: bool = False


_PHASES: tuple[Phase, ...] = (
    Phase("check", "Checking the replay"),
    Phase("manifest", "Asking Steam for the file list", cancellable=True),
    Phase("retry", "Steam didn't answer — trying again", cancellable=True),
    Phase("saved", "Checking the saved build"),
    Phase("rebuild", "Saved build is out of date — rebuilding it"),
    Phase("scan", "Checking which files you already have", cancellable=True),
    Phase("seed", "Copying files you already have", cancellable=True),
    Phase("prepare", "Working out which parts must be downloaded", cancellable=True),
    Phase("download", "Downloading from Steam", cancellable=True),
    Phase("verify", "Checking the downloaded files"),
    Phase("store", "Saving the files for next time"),
    Phase("restore", "Reading the saved files"),
    Phase("compose", "Putting the game build together"),
    Phase("cleanup", "Clearing temporary files"),
    Phase("playback", "Preparing the replay file"),
    Phase("launch", "Starting the game through Steam"),
    Phase("closing", "Replay finished — closing the game"),
    Phase(
        "close_failed",
        "Replay finished — the game didn't close, please close it yourself",
        animated=False,
        alert=True,
    ),
)

PHASES: dict[str, Phase] = {phase.key: phase for phase in _PHASES}


@dataclass(frozen=True)
class Event:
    """What the panel draws: one phase, optionally measured, as one line."""

    key: str
    label: str
    pct: float | None = None
    detail: str | None = None
    cancellable: bool = False
    animated: bool = True
    alert: bool = False

    def text(self) -> str:
        """The single line shown in the notification area.

        Lives here rather than in the panel so any other consumer (the CLI, a
        log) words a phase exactly the same way.
        """
        parts = [self.label]
        if self.pct is not None:
            parts.append(f"{self.pct:.0f}%")
        if self.detail:
            parts.append(f"· {self.detail}")
        return " ".join(parts)


_UNITS = ("B", "KB", "MB", "GB", "TB")


def _unit(total: float) -> tuple[int, str, int]:
    """Pick one unit for a pair of numbers: ``(divisor, name, decimals)``."""
    scaled = float(max(total, 1))
    index = 0
    while scaled >= 1024 and index < len(_UNITS) - 1:
        scaled /= 1024
        index += 1
    return 1024**index, _UNITS[index], 0 if index <= 1 else 1


def fmt_bytes(num: float) -> str:
    """``12.4 GB`` — a single size."""
    divisor, name, decimals = _unit(num)
    return f"{num / divisor:.{decimals}f} {name}"


def fmt_byte_pair(done: float, total: float) -> str:
    """``4.8 / 11.4 GB`` — both numbers in the *total's* unit, so the left one
    climbs steadily instead of jumping between units as it grows."""
    divisor, name, decimals = _unit(total)
    return f"{done / divisor:.{decimals}f} / {total / divisor:.{decimals}f} {name}"


def fmt_count(done: int, total: int, noun: str = "files") -> str:
    """``3,204 / 15,454 files``."""
    return f"{done:,} / {total:,} {noun}"


class Reporter:
    """Turns raw producer calls into throttled, monotonic, UI-ready events.

    Producers call :meth:`phase` when the work changes character and
    :meth:`update` as it advances; everything else — rate limiting, clamping,
    never walking backwards, never raising into the worker — happens here.
    """

    #: Minimum gap between two updates inside one phase. ~8 redraws a second is
    #: more than the eye resolves and far less than Tk starts queueing behind.
    MIN_INTERVAL = 0.12

    def __init__(self, sink: Callable[[Event | None], None] | None = None) -> None:
        self._sink = sink
        self._phase: Phase | None = None
        self._pct: float | None = None
        self._detail: str | None = None
        self._sent_at = 0.0
        # The worker reports most phases, but the end of a replay is reported
        # from the log watcher's thread; one lock keeps a phase and its numbers
        # from being torn between the two.
        self._lock = threading.RLock()

    @property
    def key(self) -> str | None:
        """The current phase key, so a producer can tell a first report from a
        continuation without tracking that itself."""
        return self._phase.key if self._phase else None

    # -- producer API ---------------------------------------------------------

    def phase(
        self, key: str, *, pct: float | None = None, detail: str | None = None
    ) -> None:
        """Enter a phase. Always emitted — a phase change is never throttled,
        because it is the one event the user is waiting to see."""
        with self._lock:
            self._phase = PHASES.get(key) or Phase(key, key.replace("_", " ").capitalize())
            self._pct = None if pct is None else _clamp(pct)
            self._detail = detail
            self._emit()

    def update(
        self,
        pct: float | None = None,
        *,
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        """Advance inside the current phase. Dropped if it comes too soon after
        the last one, unless it completes the phase or ``force`` is set."""
        with self._lock:
            if self._phase is None:
                return
            if pct is not None:
                pct = _clamp(pct)
                if self._pct is not None:
                    pct = max(pct, self._pct)  # never walk backwards inside a phase
                self._pct = pct
            if detail is not None:
                self._detail = detail
            complete = pct is not None and pct >= 100.0
            if force or complete or (time.monotonic() - self._sent_at) >= self.MIN_INTERVAL:
                self._emit()

    def clear(self) -> None:
        """No operation is running; the panel drops the line entirely."""
        with self._lock:
            self._phase = None
            self._pct = None
            self._detail = None
            self._sent_at = 0.0
            self._send(None)

    # -- adapters -------------------------------------------------------------

    def pct_sink(self) -> Callable[[float], None]:
        """A plain ``(percent) -> None`` for the lower-level modules, which all
        take that shape (``depot``, ``resticrepo``, ``compose``)."""
        return lambda pct: self.update(pct)

    def bytes_sink(self) -> Callable[[int, int], None]:
        """A ``(done, total)`` sink in bytes: ``42% · 4.8 / 11.4 GB``."""

        def report(done: int, total: int) -> None:
            self.update(
                done / total * 100.0 if total else None,
                detail=fmt_byte_pair(done, total) if total else None,
            )

        return report

    def count_sink(self, noun: str = "files") -> Callable[[int, int], None]:
        """A ``(done, total)`` sink in items: ``42% · 320 / 800 files``."""

        def report(done: int, total: int) -> None:
            self.update(
                done / total * 100.0 if total else None,
                detail=fmt_count(done, total, noun) if total else None,
            )

        return report

    # -- internals ------------------------------------------------------------

    def _emit(self) -> None:
        if self._phase is None:
            return
        self._sent_at = time.monotonic()
        self._send(
            Event(
                self._phase.key,
                self._phase.label,
                self._pct,
                self._detail,
                self._phase.cancellable,
                self._phase.animated,
                self._phase.alert,
            )
        )

    def _send(self, event: Event | None) -> None:
        if self._sink is None:
            return
        # A progress line is decoration; a broken one must never take down a
        # download that is otherwise fine.
        with contextlib.suppress(Exception):
            self._sink(event)


def _clamp(pct: float) -> float:
    return min(100.0, max(0.0, float(pct)))
