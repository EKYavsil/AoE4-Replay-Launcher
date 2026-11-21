"""Notice, from the game's own log, the moment a replay we launched is over.

A replay does not end on a results screen: the game drops straight back to its
main menu. For a reconstructed build that menu is useless (an old build cannot
play online), and even on the current build it is not a normal session — it was
started with replay-only arguments and with the player's mods moved aside until
the game exits. So the session we opened for a replay should end with it.

Nothing outside the game says when that happens. Its state files are not
written live, Steam only knows "running", there is no launch option to quit
after playback, and the engine's ``OnPlayBackOver`` event is only reachable
from script running inside the game. Its log is the one honest external signal:
``Documents/My Games/Age of Empires IV/warnings.log``, written line by line as
things happen (measured live: every line visible on disk within 0.4 s).

**What counts as "the replay is over".** Three engine messages, in this order,
within one game session:

1. ``GameApp::RequestGameBegin()`` — a match was started;
2. ``MatchSetupManager::RequestLeaveGameMatch`` — the game is leaving it;
3. ``GameApp::UpdateStateTransitionUnloadingGame - Set target state to
   AS_FrontEnd. (Current State: 8)`` — the engine's own application state
   machine is moving from in-game (state 8) to the front end.

Every finished session on disk shows exactly this sequence — June to November
2026 builds, natural ends and "leave match" alike — and nothing else produces
it. In particular, none of these do:

* ``recorded game finished`` — written seconds after load, when the recording
  has been read, and again on every restart; it is not the end of anything;
* ``Match end`` / ``GAME OVER`` — come from the game mode's script, so a mod
  with its own win condition may never write them, and a "leave match" never
  does;
* restarting or seeking the replay — reloads inside the world
  (``SPMatchSetup::RestartMatch``) and never passes through the front end;
* closing the game mid-replay — writes ``Requesting game quit`` and exits
  without a front-end transition.

**Which session.** The log is rewritten from scratch at every launch, and its
third line is ``RUN-OPTIONS [-dev -replay playback:<file>]``. The playback file
name is derived from the replay's *content*, so it identifies the replay but
not the launch: the log left by the previous run of the same replay carries the
same name and its own front-end line. So nothing written before we start
watching is ever read, a session is only accepted after the log has visibly
been reset, and only if its header names our playback file.

**Fail-safe by construction.** Every doubtful case — an unexpected header, a
second reset, a quit already in progress, a match starting again during the
grace period — makes the detector inert rather than eager. A missed end leaves
the game open, which is exactly what happened before this module existed; a
false one would close a game someone is watching or playing, so the rules only
ever err one way. The detector fires at most once per launch.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

LOG_NAME = "warnings.log"

# Engine messages, matched within the line so the timestamp prefix (which
# differs between the live log and the per-session copies) never matters.
_RUN_OPTIONS = "RUN-OPTIONS ["
_MATCH_BEGIN = "GameApp::RequestGameBegin()"
_MATCH_START = "MatchSetup::StartMatch()"
_LEAVE_MATCH = "MatchSetupManager::RequestLeaveGameMatch"
_TO_FRONTEND = (
    "GameApp::UpdateStateTransitionUnloadingGame - "
    "Set target state to AS_FrontEnd. (Current State: 8)"
)
_QUIT = "Requesting game quit"

#: The header is a handful of fixed lines (``RUN-OPTIONS`` is the third); if it
#: has not appeared this far in, the session is not one we understand.
_HEADER_LINES = 20

#: Names we generate are ``replay_<hex>[_v<n>].rec``; anything else could not be
#: matched as a single whitespace-separated token, so it is never trusted.
_PLAYBACK_NAME = re.compile(r"[A-Za-z0-9_.-]+")

# Detector states.
AWAIT_RESET = "await_reset"  # still reading whatever was there before we launched
HEADER = "header"  # a new session began; waiting for its RUN-OPTIONS line
BOUND = "bound"  # the session is ours; following its match flow
ENDED = "ended"  # the replay is over; nothing has been done about it yet
DONE = "done"  # acted on: never fires again
INERT = "inert"  # something did not add up: never fires


class ReplayEndDetector:
    """Pure state machine over log lines; no I/O, no clock, no Windows.

    :meth:`reset` is called when the log is rewritten (a new game session),
    :meth:`feed` with each complete line. :meth:`feed` returns True exactly
    once, on the line that proves the replay is over.
    """

    def __init__(self, playback_name: str) -> None:
        self.state = AWAIT_RESET
        self._token = f"playback:{playback_name}"
        if not _PLAYBACK_NAME.fullmatch(playback_name or ""):
            self.state = INERT
        self._header_lines = 0
        self._in_match = False
        self._leaving = False

    def reset(self) -> None:
        """The log was rewritten. Only the *first* rewrite after we started is
        our game starting; any later one (the game relaunched, a second
        instance, a crash and restart) is a session we did not observe from
        its beginning, so it is never trusted."""
        if self.state == AWAIT_RESET:
            self.state = HEADER
            self._header_lines = 0
        elif self.state not in (DONE, INERT):
            self.state = INERT

    def feed(self, line: str) -> bool:
        if self.state == HEADER:
            self._read_header(line)
            return False
        if self.state == BOUND:
            return self._follow_match(line)
        if self.state == ENDED and (
            _MATCH_BEGIN in line or _MATCH_START in line or _QUIT in line
        ):
            # During the grace period a new match started (the player picked
            # something from the menu) or the game began quitting on its own:
            # either way closing it is no longer ours to do.
            self.state = INERT
        return False

    @property
    def should_close(self) -> bool:
        return self.state == ENDED

    def consume(self) -> None:
        """Mark the end as acted upon; the detector never fires again."""
        if self.state == ENDED:
            self.state = DONE

    # -- internals ---------------------------------------------------------------

    def _read_header(self, line: str) -> None:
        self._header_lines += 1
        if line.startswith(_RUN_OPTIONS):
            tokens = line[len(_RUN_OPTIONS):].strip().removesuffix("]").split()
            # Whole tokens only: "playback:replay_ab.rec" must not match
            # "playback:replay_ab.rec.bak" or "xplayback:replay_ab.rec".
            ours = "-replay" in tokens and self._token in tokens
            self.state = BOUND if ours else INERT
        elif self._header_lines >= _HEADER_LINES:
            self.state = INERT

    def _follow_match(self, line: str) -> bool:
        if _QUIT in line:
            # The game is already closing (the player's Alt+F4, or Steam). It
            # needs no help, and whatever it logs on the way out is not a
            # replay ending.
            self.state = INERT
            return False
        if _MATCH_BEGIN in line:
            # A match (re)started; a restart lands here too, so any earlier
            # "leaving" no longer applies.
            self._in_match = True
            self._leaving = False
            return False
        if _LEAVE_MATCH in line and self._in_match:
            # Only a match we saw begin can be left; this is the one place the
            # whole begin -> leave order is enforced.
            self._leaving = True
            return False
        if _TO_FRONTEND in line and self._leaving:
            self.state = ENDED
            return True
        return False


def _identity(st: os.stat_result) -> tuple[int, float | None]:
    """What makes this file *this* file: its index and creation time. A
    rewrite that replaces the file changes them even when the new one is
    already bigger than the old one was."""
    return (st.st_ino, getattr(st, "st_birthtime", None))


class LogTail:
    """Reads what a log file gains, noticing when it is rewritten.

    Created before the game is launched, it starts at the file's current end, so
    nothing already there — the previous session — is ever returned. The file
    is opened, read and closed on each poll; no handle is held against the game.

    A rewrite is recognised by any of three things, because each alone has a
    blind spot: the file's identity (replaced by a new file), its size (shrank
    below what was already read), and its first bytes — the header, rewritten on
    every launch — for a rewrite in place that has already outgrown the old size
    by the next poll. Should all three somehow miss, no reset is reported and
    the detector never binds: the game is left open, nothing worse.
    """

    MAX_READ = 8 << 20  # per poll; the log grows by kilobytes a second at most
    MAX_PARTIAL = 1 << 20  # an unterminated "line" longer than this is noise
    HEAD = 256  # the start of the header: launch time, then machine state

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        st = self._stat()
        self._ident = _identity(st) if st else None
        self._offset = st.st_size if st else 0
        self._head = (self._read_head() or b"") if st else b""
        self._partial = b""

    def poll(self) -> tuple[bool, list[str]]:
        """``(was_reset, new_complete_lines)`` since the previous poll."""
        st = self._stat()
        if st is None:
            return False, []  # mid-rewrite (deleted, not yet recreated): next poll
        ident = _identity(st)
        if ident == self._ident and st.st_size == self._offset:
            return False, []  # nothing new: the common case, a single stat
        head = self._read_head()
        reset = (
            ident != self._ident
            or st.st_size < self._offset
            or self._head_rewritten(head)
        )
        if reset:
            self._ident = ident
            self._offset = 0
            self._partial = b""
            self._head = head or b""
        elif head is not None and len(self._head) < self.HEAD:
            self._head = head  # same start, just longer now: remember more of it
        if st.st_size <= self._offset:
            return reset, []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(min(st.st_size - self._offset, self.MAX_READ))
        except OSError:
            return reset, []
        self._offset += len(chunk)
        *complete, partial = (self._partial + chunk).split(b"\n")
        self._partial = partial if len(partial) <= self.MAX_PARTIAL else b""
        return reset, [raw.decode("utf-8", "replace").rstrip("\r") for raw in complete]

    def _head_rewritten(self, head: bytes | None) -> bool:
        if head is None:
            return False  # unreadable right now: decide on the next poll
        if not self._head:
            # The file was empty (or absent) when we started; anything in it now
            # was written from its first byte, i.e. by a session that began
            # after we did.
            return bool(head)
        return head[: len(self._head)] != self._head

    def _read_head(self) -> bytes | None:
        try:
            with self.path.open("rb") as fh:
                return fh.read(self.HEAD)
        except OSError:
            return None

    def _stat(self) -> os.stat_result | None:
        try:
            return self.path.stat()
        except OSError:
            return None


class ReplayEndWatcher:
    """Background thread: follow the log, and close the game once when its
    replay is over.

    Create it **before** launching the game, so the log's previous contents are
    behind its starting point; :meth:`stop` it once the game has exited. All
    callbacks run on this thread and are guarded: a failing callback is
    reported and swallowed, and never turns into a retry.
    """

    POLL = 0.25  # s; a stat per poll, and a read only when the file grew
    GRACE = 2.0  # s between the menu appearing and the close request
    CLOSE_WAIT = 120.0  # s for Steam to report the game gone (measured: up to 55)

    def __init__(
        self,
        log_path: Path,
        playback_name: str,
        close_game: Callable[[], bool],
        on_end: Callable[[], None] | None = None,
        on_close_failed: Callable[[], None] | None = None,
    ) -> None:
        self._tail = LogTail(log_path)  # snapshot now: before the game exists
        self._detector = ReplayEndDetector(playback_name)
        self._close_game = close_game
        self._on_end = on_end
        self._on_close_failed = on_close_failed
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="replay-end-watcher", daemon=True
        )

    @property
    def state(self) -> str:
        return self._detector.state

    def start(self) -> ReplayEndWatcher:
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    # -- thread body -------------------------------------------------------------

    def _pump(self) -> bool:
        reset, lines = self._tail.poll()
        if reset:
            self._detector.reset()
        ended = False
        for line in lines:
            if self._detector.feed(line):
                ended = True
        return ended

    def _run(self) -> None:
        try:
            self._watch()
        except Exception as exc:  # noqa: BLE001 - a watcher bug must never act
            print(f"Replay-end watcher stopped ({exc}); the game is left as it is.")

    def _watch(self) -> None:
        while not self._stop.wait(self.POLL):
            if self._pump():
                break
        else:
            return  # stopped: the game exited (or never started) first

        # Let the menu finish appearing, and keep reading meanwhile: if a new
        # match starts or the game starts quitting, the detector stands down.
        deadline = time.monotonic() + self.GRACE
        while (left := deadline - time.monotonic()) > 0:
            if self._stop.wait(min(self.POLL, left)):
                return
            self._pump()
            if not self._detector.should_close:
                return
        if not self._detector.should_close:
            return
        self._detector.consume()  # one shot, whatever happens next

        print("Replay finished (the game returned to its main menu); closing the game.")
        _call(self._on_end)
        if not _call(self._close_game):
            print("Could not ask the game to close; it is left open.")
            _call(self._on_close_failed)
            return
        if not self._stop.wait(self.CLOSE_WAIT):
            # Asked once, politely; never again and never by force. The game
            # may be showing its own prompt, or ignoring the request.
            print("The game did not close after the request; it is left open.")
            _call(self._on_close_failed)


def _call(callback: Callable[[], object] | None) -> object:
    """Run a callback, reporting and swallowing anything it raises. A failure
    reads as ``None`` — which, for ``close_game``, means "not closed"."""
    if callback is None:
        return None
    try:
        return callback()
    except Exception as exc:  # noqa: BLE001 - a callback must not end the watch
        print(f"Replay-end watcher: {getattr(callback, '__name__', 'callback')} failed ({exc}).")
        return None
