"""customtkinter desktop panel: find, download and play head-to-head replays.

Double-clicking AoE4-Replay-Launcher.vbs opens this. Enter two profile
ids (or find them by name), list every head-to-head match, download a replay
(tried from both players' perspectives), and play any downloaded replay through
the normal watch flow. The panel stays open across game launches.
"""

from __future__ import annotations

import calendar as _calendar
import contextlib
import hashlib
import io
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from .config import Config


def _ensure_tcl_tk() -> None:
    """Point Tcl/Tk at the base Python install.

    A Windows venv's pythonw.exe frequently can't locate Tcl/Tk on its own, so
    importing tkinter/customtkinter crashes (silently, under pythonw). Set the
    library paths from the base interpreter before any Tk window is created.
    """
    if os.environ.get("TCL_LIBRARY"):
        return
    if getattr(sys, "frozen", False):
        # In the packaged build Tcl/Tk ships under _tcl_data / _tk_data next to
        # the bundled modules; point Tk at them so it can find its init scripts.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        tcl, tk = base / "_tcl_data", base / "_tk_data"
        if tcl.is_dir():
            os.environ["TCL_LIBRARY"] = str(tcl)
        if tk.is_dir():
            os.environ["TK_LIBRARY"] = str(tk)
        return
    tcl_root = Path(sys.base_prefix) / "tcl"
    if not tcl_root.is_dir():
        return
    for tcl in sorted(tcl_root.glob("tcl8.*"), reverse=True):
        os.environ["TCL_LIBRARY"] = str(tcl)
        tk = tcl_root / tcl.name.replace("tcl", "tk", 1)
        if tk.is_dir():
            os.environ["TK_LIBRARY"] = str(tk)
        break


_ensure_tcl_tk()

import tkinter as tk  # noqa: E402
from tkinter import filedialog  # noqa: E402

import customtkinter as ctk  # noqa: E402 - must follow _ensure_tcl_tk()
from PIL import Image  # noqa: E402

from . import (  # noqa: E402
    __version__,
    aoe4world,
    buildcache,
    config,
    launch,
    replay,
    service,
    steamqr,
)

# ---- palette / glyphs -----------------------------------------------------

BG = "#0e1420"  # window background
PANEL = "#141b29"  # tab / scroll-frame background
CARD = "#1a2230"  # row card background
ACCENT = "#2f6bf2"  # primary blue
ACCENT_HOVER = "#2657cc"
DANGER = "#762f32"  # muted delete red
DANGER_HOVER = "#8d393c"
NEUTRAL = "#222c3c"  # subtle button (load more)
NEUTRAL_HOVER = "#2a3547"
GREEN = "#41c463"  # winner / success
PLAY_GREEN = "#2d7650"
PLAY_GREEN_HOVER = "#378d61"
LOSS = "#ef5b5b"
TEXT = "#e7eaf0"
MUTED = "#8a93a6"
DIVIDER = "#2a3344"

ICON_CAL = "📅"
ICON_KIND = "⚔"
ICON_CLOCK = "🕐"
ICON_GAMES = "🎮"
ICON_DL = "⬇"
ICON_FILE = "📄"
ICON_PLAY = "▶"
ICON_TRASH = "🗑"
ICON_RELOAD = "↻"
ICON_SEARCH = "🔍"
ICON_CHECK = "✓"

ASSET_DIR = Path(__file__).with_name("assets")

INFO_GITHUB_URL = "https://github.com/EKYavsil/AoE4-Replay-Launcher"
INFO_DISCORD_URL = "https://discord.gg/HsmQ8wQFA5"
INFO_GMAIL_URL = "https://mail.google.com/mail/?view=cm&fs=1&to=eyavsil44@gmail.com"

CIV_IMAGES = {
    "ABB": "Abbasid_Dynasty_AoE4.png",
    "AYY": "Ayyubids_AoE4.png",
    "BYZ": "Byzantines_AoE4.png",
    "CHI": "Chinese_AoE4.png",
    "DEL": "Delhi_Sultanate_AoE4.png",
    "ENG": "English_AoE4.png",
    "FRE": "French_AoE4.png",
    "GOL": "Golden_Horde_AoE4.png",
    "HRE": "HRE_AoE4.png",
    "JDA": "Jeanne_d_Arc_AoE4.png",
    "JIN": "Jin_Dynasty_AoE4.png",
    "JPN": "Japanese_AoE4.png",
    "LAN": "House_of_Lancaster_AoE4.png",
    "MAC": "Macedonian_Dynasty_AoE4.png",
    "MAL": "Malians_AoE4.png",
    "MON": "Mongols_AoE4.png",
    "OOD": "Order_of_the_Dragon_AoE4.png",
    "OTT": "Ottomans_AoE4.png",
    "RUS": "Rus_AoE4.png",
    "SEN": "Sengoku_Daimyo_AoE4.png",
    "TMP": "Knights_Templar_campaign_AoE4.png",
    "TUG": "Tughlaq_Dynasty_AoE4.png",
    "ZXL": "Zhu_Xis_Legacy_AoE4.png",
}

LEAGUE_IMAGES = {
    "bronze": "bronze",
    "silver": "silver",
    "gold": "gold",
    "platinum": "plat",
    "diamond": "dia",
    "conqueror": "conq",
}


def _fmt_duration(seconds: object) -> str:
    try:
        return f"{max(1, round(int(seconds) / 60))} min"
    except (TypeError, ValueError):
        return "?"


def _match_text(s: dict) -> str:
    when = s["started_at"].strftime("%Y-%m-%d %H:%M") if s["started_at"] else "?"
    c1 = f" [{s['civ1']}]" if s.get("civ1") else ""
    c2 = f" [{s['civ2']}]" if s.get("civ2") else ""
    mark1 = " ✓" if s["winner"] == s.get("_id1") else ""
    mark2 = " ✓" if s["winner"] == s.get("_id2") else ""
    return (
        f"{when} UTC  |  {s['kind']}  |  {s['map']}  |  {_fmt_duration(s['duration'])}"
        f"   —   {s['name1']}{c1}{mark1}  vs  {s['name2']}{c2}{mark2}"
    )


class CalendarPopup(ctk.CTkToplevel):
    """A small dark-themed month calendar; clicking a day calls on_pick(date)."""

    WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

    def __init__(self, parent, title: str, on_pick, initial: date | None = None) -> None:
        super().__init__(parent)
        self.title(title)
        self.configure(fg_color=PANEL)
        self.resizable(False, False)
        self.on_pick = on_pick
        self._view = (initial or date.today()).replace(day=1)
        self._bold = ctk.CTkFont(size=13, weight="bold")
        self._norm = ctk.CTkFont(size=12)

        head = ctk.CTkFrame(self, fg_color=PANEL)
        head.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkButton(
            head, text="‹", width=34, height=30, fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOVER, command=self._prev,
        ).pack(side="left")
        self._title = ctk.CTkLabel(head, text="", text_color=TEXT, font=self._bold, width=150)
        self._title.pack(side="left", expand=True)
        ctk.CTkButton(
            head, text="›", width=34, height=30, fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOVER, command=self._next,
        ).pack(side="right")
        self._grid = ctk.CTkFrame(self, fg_color=PANEL)
        self._grid.pack(padx=10, pady=(0, 10))

        self._render()
        self.transient(parent)
        self.after(80, self._center_on_parent)
        self.after(150, self._safe_grab)

    def _safe_grab(self) -> None:
        with contextlib.suppress(Exception):
            self.grab_set()

    def _render(self) -> None:
        for child in self._grid.winfo_children():
            child.destroy()
        self._title.configure(text=self._view.strftime("%B %Y"))
        for col, name in enumerate(self.WEEKDAYS):
            ctk.CTkLabel(
                self._grid, text=name, width=34, text_color=MUTED, font=self._norm
            ).grid(row=0, column=col, padx=1, pady=1)
        weeks = _calendar.Calendar(firstweekday=0).monthdayscalendar(
            self._view.year, self._view.month
        )
        today = date.today()
        for row, week in enumerate(weeks, start=1):
            for col, day in enumerate(week):
                if day == 0:
                    continue
                picked = date(self._view.year, self._view.month, day)
                ctk.CTkButton(
                    self._grid, text=str(day), width=34, height=30, corner_radius=6,
                    fg_color=ACCENT if picked == today else CARD,
                    hover_color=ACCENT_HOVER, text_color=TEXT, font=self._norm,
                    command=lambda d=picked: self._pick(d),
                ).grid(row=row, column=col, padx=1, pady=1)

    def _shift_month(self, delta: int) -> None:
        month = self._view.month - 1 + delta
        self._view = date(self._view.year + month // 12, month % 12 + 1, 1)
        self._render()

    def _prev(self) -> None:
        self._shift_month(-1)

    def _next(self) -> None:
        self._shift_month(1)

    def _pick(self, picked: date) -> None:
        # Hide now, but destroy a little later so customtkinter's own scheduled
        # callbacks (focus/lift) don't fire on an already-destroyed window.
        with contextlib.suppress(Exception):
            self.grab_release()
        self.withdraw()
        self.after(400, self.destroy)
        self.on_pick(picked)

    def _center_on_parent(self) -> None:
        with contextlib.suppress(Exception):
            self.update_idletasks()
            px, py = self.master.winfo_rootx(), self.master.winfo_rooty()
            pw = self.master.winfo_width()
            self.geometry(f"+{px + pw // 2 - self.winfo_width() // 2}+{py + 90}")


class Panel(ctk.CTk):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.title("AoE4 Replay Launcher")
        self._apply_window_icon()
        self.geometry("1600x900")
        ctk.set_appearance_mode("dark")

        self._searching = False
        self._playing = False
        self._cancel_event: threading.Event | None = None  # set while a download runs
        self._play_buttons: list[ctk.CTkButton] = []
        self._info_cache: dict[int, dict] = {}  # game_id -> match summary
        self._images: dict[tuple[str, tuple[int, int]], ctk.CTkImage] = {}
        self._country_sources: dict[str, Image.Image] = {}
        self._country_images: dict[str, ctk.CTkImage] = {}
        self._country_failures: set[str] = set()
        self._info_window: ctk.CTkToplevel | None = None
        self._active_tab = "Profile games"
        # profile-games tab paging state
        self._pg_profile: int | None = None
        self._pg_page = 0
        self._pg_total: int | None = None
        self._pg_loaded = 0
        self._pg_searching = False
        self._pg_since: str | None = None  # API server-side lower bound (YYYY-MM-DD)
        self._pg_until: date | None = None  # client-side upper bound
        self._h2h_ids: tuple[int, int] | None = None
        self._h2h_since: str | None = None
        self._h2h_until: date | None = None
        self._split_initialized = False
        self._grip_drag_offset = 0
        self._grip_target_y = 0

        # restore mods if a previous session was force-closed while a replay ran
        with contextlib.suppress(Exception):
            launch.recover_user_mods(self.cfg)
        # if the app was moved/updated, re-point the persistent Steam shim at the live exe
        with contextlib.suppress(Exception):
            launch.heal_wrapper_paths(self.cfg)
        # drop an orphaned replay request from a crashed session, so a stale request
        # can't make a later normal Play open the wrong (old) build
        with contextlib.suppress(Exception):
            launch._clear_active_replay_request(self.cfg)
        # drop composed builds left over from previous sessions (keeps saved ones)
        with contextlib.suppress(Exception):
            buildcache.cleanup(self.cfg)
        # also sweep transient restore/download scratch dirs every launch — with the
        # build cache reconstructions are rarer, so they'd otherwise pile up
        with contextlib.suppress(Exception):
            service.clean_workspace(self.cfg)
        self._keep_asked: set[str] = set()  # builds we've offered to keep this session
        # clean unsaved composed builds when the panel closes too (frees disk sooner;
        # buildcache.cleanup itself skips while a game is running)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.refresh_downloads()
        # learn builds released while the panel was closed (background, best-effort)
        threading.Thread(
            target=lambda: service.sync_build_map(self.cfg), daemon=True
        ).start()
        # check for a newer app release in the background (Velopack auto-update)
        threading.Thread(target=self._check_for_updates, daemon=True).start()

    # ---- Steam sign-in ----------------------------------------------------

    def _prompt_steam_login(self, on_success=None) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Connect your Steam account")
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.transient(self)
        # Center over the main window (like the other modals) instead of the
        # window manager's default top-left placement.
        win.update_idletasks()
        cx = self.winfo_rootx() + (self.winfo_width() - 460) // 2
        cy = self.winfo_rooty() + (self.winfo_height() - 580) // 2
        win.geometry(f"460x580+{max(cx, 0)}+{max(cy, 0)}")
        win.after(200, win.grab_set)
        win.protocol("WM_DELETE_WINDOW", self._close_login)  # the X just closes this dialog
        self._login_win = win
        self._login_on_success = on_success
        self._login_cancel = threading.Event()
        self._awaiting_guard = False
        self._qr_image = None  # keep a reference so the CTkImage isn't garbage-collected

        ctk.CTkLabel(
            win, text="Connect your Steam account", text_color=TEXT,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(22, 2))
        ctk.CTkLabel(
            win,
            text="Replays only play on the exact build they were recorded on, so the\n"
                 "tool downloads that build from Steam using your own account.",
            text_color=MUTED, font=self.font_meta, justify="center",
        ).pack(pady=(0, 10))

        # Swappable area: the QR view or the username/password view live here.
        self._login_body = ctk.CTkFrame(win, fg_color="transparent")
        self._login_body.pack(fill="both", expand=True, padx=20)

        self._login_status = ctk.CTkLabel(
            win, text="", text_color=MUTED, font=self.font_meta,
            justify="center", wraplength=400,
        )
        self._login_status.pack(pady=(6, 6))

        ctk.CTkButton(
            win, text="Why is this needed?", width=170, height=34, corner_radius=8,
            fg_color=NEUTRAL, hover_color=NEUTRAL_HOVER, font=self.font_meta,
            command=self._show_steam_privacy,
        ).pack(pady=(0, 16))

        self._login_show_qr()

    def _login_set_status(self, text: str, color: str) -> None:
        with contextlib.suppress(Exception):
            self._login_status.configure(text=text, text_color=color)

    def _clear_login_body(self) -> None:
        for child in self._login_body.winfo_children():
            child.destroy()

    # ---- QR sign-in (default) --------------------------------------------

    def _login_show_qr(self) -> None:
        self._login_cancel.set()  # stop any previous sign-in attempt
        self._login_cancel = threading.Event()
        self._awaiting_guard = False
        self._clear_login_body()

        ctk.CTkLabel(
            self._login_body,
            text="Scan this with the Steam Mobile App and approve the sign-in.",
            text_color=TEXT, font=self.font_normal, justify="center", wraplength=400,
        ).pack(pady=(4, 10))
        self._qr_label = ctk.CTkLabel(
            self._login_body, text="Preparing QR code…", text_color=MUTED,
            font=self.font_meta, width=259, height=259,
        )
        self._qr_label.pack(pady=4)
        ctk.CTkButton(
            self._login_body, text="Use username & password instead",
            width=260, height=38, corner_radius=8, fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOVER, font=self.font_bold,
            command=self._login_show_password,
        ).pack(pady=(14, 4))

        self._login_set_status("Waiting for you to scan the code…", GREEN)
        self._login_start_qr()

    def _login_start_qr(self) -> None:
        cancel = self._login_cancel

        def on_qr(matrix) -> None:
            self.after(0, lambda m=matrix: self._login_render_qr(m))

        def on_approved() -> None:
            self.after(0, lambda: self._login_set_status("Approved! Finishing sign-in…", GREEN))

        def worker() -> None:
            error = None
            account = None
            try:
                account = service.steam_login_qr(self.cfg, on_qr, on_approved, cancel)
            except Exception as exc:  # noqa: BLE001 - report sign-in failures
                error = str(exc)
            if cancel.is_set():
                return  # dialog was closed or switched to password — ignore
            self.after(0, lambda: self._login_qr_done(error, account))

        threading.Thread(target=worker, daemon=True).start()

    def _login_render_qr(self, matrix) -> None:
        with contextlib.suppress(Exception):
            img = steamqr.render_image(matrix, scale=7).convert("RGB")
            self._qr_image = ctk.CTkImage(img, size=(img.width, img.height))
            self._qr_label.configure(image=self._qr_image, text="")

    def _login_qr_done(self, error: str | None, account: str | None) -> None:
        if not error:
            self._connect_succeeded(account or "")
            return
        self._clear_login_body()
        ctk.CTkLabel(
            self._login_body, text="The QR sign-in didn't complete.",
            text_color=TEXT, font=self.font_normal,
        ).pack(pady=(24, 10))
        ctk.CTkButton(
            self._login_body, text="Show a new QR code", width=240, height=40,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=self.font_bold, command=self._login_show_qr,
        ).pack(pady=6)
        ctk.CTkButton(
            self._login_body, text="Use username & password instead", width=240, height=38,
            corner_radius=8, fg_color=NEUTRAL, hover_color=NEUTRAL_HOVER,
            font=self.font_bold, command=self._login_show_password,
        ).pack(pady=6)
        self._login_set_status(error, LOSS)

    # ---- Username / password fallback ------------------------------------

    def _login_show_password(self) -> None:
        self._login_cancel.set()  # stop the QR attempt
        self._login_cancel = threading.Event()
        self._awaiting_guard = False
        self._clear_login_body()

        self._login_user = ctk.CTkEntry(
            self._login_body, placeholder_text="Username", width=300, height=42,
            corner_radius=8, fg_color=CARD, font=self.font_normal,
        )
        self._login_user.pack(pady=6)
        self._login_pass = ctk.CTkEntry(
            self._login_body, placeholder_text="Password", show="*", width=300, height=42,
            corner_radius=8, fg_color=CARD, font=self.font_normal,
        )
        self._login_pass.pack(pady=6)
        # Steam Guard code field — created hidden, revealed only when Steam asks.
        self._login_guard = ctk.CTkEntry(
            self._login_body, placeholder_text="Code", width=300, height=42,
            corner_radius=8, fg_color=CARD, font=self.font_normal,
        )

        self._login_btnrow = ctk.CTkFrame(self._login_body, fg_color="transparent")
        self._login_btnrow.pack(pady=(12, 0))
        ctk.CTkButton(
            self._login_btnrow, text="Back to QR", width=145, height=42, corner_radius=8,
            fg_color=NEUTRAL, hover_color=NEUTRAL_HOVER, font=self.font_bold,
            command=self._login_show_qr,
        ).pack(side="left", padx=6)
        self._login_btn = ctk.CTkButton(
            self._login_btnrow, text="Connect", width=145, height=42, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, font=self.font_bold,
            command=self._connect_steam,
        )
        self._login_btn.pack(side="left", padx=6)
        self._login_user.after(250, self._login_user.focus)
        self._login_pass.bind("<Return>", lambda _e: self._connect_steam())
        self._login_guard.bind("<Return>", lambda _e: self._connect_steam())
        self._login_set_status("Your password is sent only to Steam and never saved.", MUTED)

    def _connect_steam(self) -> None:
        if self._awaiting_guard:  # waiting for a 2FA code -> submit it
            code = self._login_guard.get().strip()
            if not code:
                return
            self._awaiting_guard = False
            self._guard_code = code
            self._login_btn.configure(state="disabled")
            self._login_guard.configure(state="disabled")
            self._login_set_status("Verifying…", GREEN)
            self._guard_event.set()
            return

        username = self._login_user.get().strip()
        password = self._login_pass.get()
        if not username or not password:
            self._login_set_status("Enter your username and password.", LOSS)
            return
        for widget in (self._login_btn, self._login_user, self._login_pass):
            widget.configure(state="disabled")
        self._login_set_status("Connecting to Steam…", GREEN)
        cancel = self._login_cancel

        def worker() -> None:
            error = None
            try:
                service.steam_login(self.cfg, username, password, self._on_2fa, cancel)
            except Exception as exc:  # noqa: BLE001 - report sign-in failures
                error = str(exc)
            if cancel.is_set():
                return
            self.after(0, lambda: self._connect_done(error, username))

        threading.Thread(target=worker, daemon=True).start()

    def _on_2fa(self, kind: str, email: str | None, incorrect: bool) -> str | None:
        """Called from the sign-in worker thread for each Steam Guard challenge.

        For a phone push there is nothing to type, so we only update the UI and
        return ``None``; for an authenticator/email code we reveal the field and
        block until the user submits it.
        """
        if kind == "confirm":
            self.after(0, lambda: self._login_set_status(
                "Check your phone: approve the sign-in in your Steam Mobile App.", GREEN
            ))
            return None
        self._guard_event = threading.Event()
        self._guard_code = None
        self.after(0, lambda: self._show_guard_field(kind, email, incorrect))
        self._guard_event.wait()
        return self._guard_code

    def _show_guard_field(self, kind: str, email: str | None, incorrect: bool) -> None:
        self._awaiting_guard = True
        if not self._login_guard.winfo_ismapped():
            self._login_guard.pack(pady=6, before=self._login_btnrow)
        self._login_guard.configure(state="normal")
        self._login_guard.delete(0, "end")
        if kind == "email":
            where = f" sent to {email}" if email else " sent to your email"
            msg = f"Enter the Steam Guard code{where}."
            self._login_guard.configure(placeholder_text="Email code")
        else:  # device / authenticator
            msg = "Enter the 6-digit code from your Steam Mobile App authenticator."
            self._login_guard.configure(placeholder_text="Authenticator code")
        if incorrect:
            msg = "That code wasn't accepted. " + msg
        self._login_btn.configure(state="normal", text="Submit code")
        self._login_set_status(msg, LOSS if incorrect else GREEN)
        self._login_guard.focus()

    def _connect_done(self, error: str | None, username: str) -> None:
        self._awaiting_guard = False
        if error:
            for widget in (self._login_btn, self._login_user, self._login_pass):
                with contextlib.suppress(Exception):
                    widget.configure(state="normal")
            with contextlib.suppress(Exception):
                self._login_btn.configure(text="Connect")
                self._login_guard.delete(0, "end")
                self._login_guard.pack_forget()
            self._login_set_status("Sign-in failed.", LOSS)
            self._error("Steam sign-in", error)  # show the real reason
            return
        self._connect_succeeded(username)

    def _connect_succeeded(self, username: str) -> None:
        if username:
            config.set_steam_username(self.cfg.project_root, username)  # for silent downloads
            self.cfg = config.load()
        self._close_login()
        if self._login_on_success is not None:
            self._login_on_success()

    def _close_login(self) -> None:
        with contextlib.suppress(Exception):
            self._login_cancel.set()  # kill any running DepotDownloader sign-in
        # If a worker is blocked waiting for a 2FA code, release it as cancelled so
        # the thread doesn't hang forever on a window that no longer exists.
        self._awaiting_guard = False
        self._guard_code = None
        with contextlib.suppress(Exception):
            self._guard_event.set()
        with contextlib.suppress(Exception):
            self._login_win.grab_release()
            self._login_win.destroy()

    def _show_steam_privacy(self) -> None:
        self._info(
            "Why is this needed?",
            "Replays only play on the exact game build they were recorded on, so the "
            "tool downloads that historical build from Steam — which needs you to sign "
            "in with your own Steam account that owns the game.\n\n"
            "The easiest way is the QR code: scan it in the Steam Mobile App and approve "
            "— nothing is typed. You can also use a username and password instead.\n\n"
            "Everything stays on your PC:\n\n"
            "• Signing in goes through Steam's own official downloader. With QR no "
            "password is entered at all; with a password it is sent only to Steam to "
            "sign in once and is never written to disk or shared.\n\n"
            "• Only Steam's remembered-login token and your Steam account name are "
            "cached locally (in config.local.toml) so future downloads are silent.\n\n"
            "• This is the normal way open-source tools download game files you "
            "already own.",
        )

    # ---- layout -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.configure(fg_color=BG)
        self.font_normal = ctk.CTkFont(size=13)
        self.font_bold = ctk.CTkFont(size=13, weight="bold")
        self.font_head = ctk.CTkFont(size=15, weight="bold")
        self.font_meta = ctk.CTkFont(size=12)

        self.splitter = tk.PanedWindow(
            self,
            orient=tk.VERTICAL,
            bg=DIVIDER,
            bd=0,
            borderwidth=0,
            sashwidth=12,
            sashrelief=tk.FLAT,
            sashcursor="sb_v_double_arrow",
            opaqueresize=False,
        )
        self.splitter.pack(fill="both", expand=True, padx=14, pady=(14, 4))
        self.main_pane = ctk.CTkFrame(self.splitter, fg_color=BG, corner_radius=0)
        self.download_pane = ctk.CTkFrame(self.splitter, fg_color=BG, corner_radius=0)
        self.splitter.add(self.main_pane, minsize=260, stretch="always")
        self.splitter.add(self.download_pane, minsize=150, stretch="always")
        self.splitter.bind("<Configure>", self._on_splitter_configure)
        self.splitter.bind("<ButtonPress-1>", self._on_splitter_press)
        self.splitter.bind("<ButtonRelease-1>", self._on_splitter_release)

        self.split_grip = tk.Canvas(
            self.splitter,
            width=38,
            height=16,
            bg=DIVIDER,
            bd=0,
            highlightthickness=0,
            cursor="sb_v_double_arrow",
        )
        for y in (5, 8, 11):
            self.split_grip.create_line(11, y, 27, y, fill=TEXT, width=1)
        self.split_grip.bind("<ButtonPress-1>", self._start_grip_drag)
        self.split_grip.bind("<B1-Motion>", self._drag_split_grip)
        self.split_grip.bind("<ButtonRelease-1>", self._finish_grip_drag)

        self.tabs_shell = ctk.CTkFrame(self.main_pane, fg_color=PANEL, corner_radius=12)
        self.tabs_shell.pack(fill="both", expand=True)

        header = ctk.CTkFrame(self.tabs_shell, fg_color="transparent", height=94)
        header.pack(fill="x")
        header.pack_propagate(False)

        nav = ctk.CTkFrame(header, fg_color="transparent", width=560, height=62)
        nav.place(relx=0.5, rely=0.5, anchor="center")
        nav.grid_propagate(False)
        # all three columns equal width so the gaps between tabs are even
        nav.grid_columnconfigure((0, 1, 2), weight=1, uniform="tabs")
        nav.grid_rowconfigure(0, weight=1)

        info_icon = self._asset_image("info.png", (12, 27))
        self.info_button = ctk.CTkButton(
            header,
            text="",
            image=info_icon,
            width=34,
            height=34,
            corner_radius=4,
            fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._show_info,
        )
        self.info_button.place(x=8, y=8)

        self._tab_buttons: dict[str, ctk.CTkButton] = {}
        self._tab_lines: dict[str, ctk.CTkFrame] = {}
        for column, name in enumerate(("Profile games", "Head-to-head", "Saved builds")):
            slot = ctk.CTkFrame(nav, fg_color="transparent")
            slot.grid(row=0, column=column, sticky="nsew", padx=16)
            button = ctk.CTkButton(
                slot,
                text=name,
                height=48,
                corner_radius=0,
                fg_color="transparent",
                hover_color=NEUTRAL,
                text_color=MUTED,
                font=ctk.CTkFont(size=16),
                command=lambda selected=name: self._switch_tab(selected),
            )
            button.pack(fill="x")
            line = ctk.CTkFrame(slot, height=4, corner_radius=0, fg_color="transparent")
            line.pack(fill="x")
            self._tab_buttons[name] = button
            self._tab_lines[name] = line

        logo = self._asset_image("logo.png", (76, 76))  # square logo; keep 1:1 to avoid stretching
        if logo:
            self.logo_label = ctk.CTkLabel(
                header,
                text="",
                image=logo,
                width=84,
                height=88,
                fg_color="transparent",
            )
            self.logo_label.pack(side="right", padx=(10, 18), pady=3)

        ctk.CTkFrame(self.tabs_shell, height=1, fg_color=DIVIDER).pack(fill="x")
        # Shared, always-visible notification bar — operation status/progress is
        # global (not tab-specific), so it lives here and shows on every tab.
        notify_bar = ctk.CTkFrame(self.tabs_shell, fg_color=PANEL, height=30)
        notify_bar.pack(fill="x", padx=14, pady=(4, 0))
        notify_bar.pack_propagate(False)
        self.status = ctk.CTkLabel(
            notify_bar, text="", anchor="w", text_color=MUTED, font=self.font_normal
        )
        self.status.pack(side="left")
        notify_right = ctk.CTkFrame(notify_bar, fg_color="transparent")
        notify_right.pack(side="right", fill="y")
        self.notify = ctk.CTkLabel(
            notify_right, text="", anchor="e", text_color=LOSS, font=self.font_bold
        )
        self.notify.pack(side="left")
        # Tiny "✕" box (info-button style) right next to the download progress;
        # hidden until a cancellable download is running (see _show_cancel).
        self._cancel_button = ctk.CTkButton(
            notify_right, text="✕", width=24, height=24, corner_radius=4,
            fg_color=DANGER, hover_color=DANGER_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=13, weight="bold"), command=self._cancel_download,
        )
        self.tab_content = ctk.CTkFrame(self.tabs_shell, fg_color=PANEL)
        self.tab_content.pack(fill="both", expand=True)
        self.profile_tab = ctk.CTkFrame(self.tab_content, fg_color=PANEL)
        self.h2h_tab = ctk.CTkFrame(self.tab_content, fg_color=PANEL)
        self.saved_tab = ctk.CTkFrame(self.tab_content, fg_color=PANEL)
        self._build_profile_tab(self.profile_tab)
        self._build_h2h_tab(self.h2h_tab)
        self._build_saved_tab(self.saved_tab)
        self._switch_tab("Profile games")

        dl_header = ctk.CTkFrame(self.download_pane, fg_color="transparent")
        dl_header.pack(fill="x", padx=6, pady=(6, 0))
        ctk.CTkButton(
            dl_header,
            text="➕  Add replay file",
            width=152,
            height=34,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=self.font_bold,
            command=self._import_replay,
        ).pack(side="left")

        self.downloads = ctk.CTkScrollableFrame(
            self.download_pane,
            label_text=f"{ICON_DL}  Downloaded replays",
            fg_color=PANEL,
            label_fg_color=PANEL,
            label_text_color=TEXT,
            label_font=self.font_head,
        )
        self.downloads.pack(fill="both", expand=True)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=18, pady=(0, 6))
        ctk.CTkLabel(
            footer,
            text=f"v{__version__}",
            text_color=MUTED,
            font=ctk.CTkFont(size=10),
        ).pack(side="left")
        ctk.CTkLabel(
            footer,
            text="🔹by Zavarash",
            text_color=MUTED,
            font=ctk.CTkFont(size=10),
        ).pack(side="right")
        self.after(100, self._set_initial_split)

    def _set_initial_split(self) -> None:
        self.update_idletasks()
        height = self.splitter.winfo_height()
        if self._split_initialized:
            self._position_split_grip()
            return
        if height <= 100:
            self.after(100, self._set_initial_split)
            return
        self.splitter.sash_place(0, 0, int(height * 0.70))
        self._split_initialized = True
        self._position_split_grip()

    def _on_splitter_configure(self, _event: tk.Event) -> None:
        if self._split_initialized:
            self.after_idle(self._position_split_grip)

    def _on_splitter_press(self, event: tk.Event) -> None:
        with contextlib.suppress(tk.TclError):
            _x, sash_y = self.splitter.sash_coord(0)
            if abs(event.y - sash_y) <= 12:
                self.split_grip.place_forget()

    def _on_splitter_release(self, _event: tk.Event) -> None:
        self.after_idle(self._position_split_grip)

    def _position_split_grip(self) -> None:
        with contextlib.suppress(tk.TclError):
            _x, y = self.splitter.sash_coord(0)
            self.split_grip.place(relx=0.5, y=y + 6, anchor="center")
            self.split_grip.tk.call("raise", self.split_grip._w)

    def _start_grip_drag(self, event: tk.Event) -> None:
        _x, sash_y = self.splitter.sash_coord(0)
        self._grip_drag_offset = event.y_root - self.splitter.winfo_rooty() - sash_y
        self._grip_target_y = sash_y

    def _drag_split_grip(self, event: tk.Event) -> None:
        y = event.y_root - self.splitter.winfo_rooty() - self._grip_drag_offset
        max_y = self.splitter.winfo_height() - 150 - int(self.splitter.cget("sashwidth"))
        self._grip_target_y = max(260, min(y, max_y))
        self.split_grip.place(relx=0.5, y=self._grip_target_y + 6, anchor="center")
        self.split_grip.tk.call("raise", self.split_grip._w)

    def _finish_grip_drag(self, _event: tk.Event) -> None:
        self.splitter.sash_place(0, 0, self._grip_target_y)
        self._position_split_grip()

    def _switch_tab(self, name: str) -> None:
        self._active_tab = name
        if name == "Saved builds":
            self._refresh_saved_builds()
        for tab_name, frame in (
            ("Profile games", self.profile_tab),
            ("Head-to-head", self.h2h_tab),
            ("Saved builds", self.saved_tab),
        ):
            if tab_name == name:
                frame.pack(fill="both", expand=True)
                self._tab_buttons[tab_name].configure(text_color=TEXT)
                self._tab_lines[tab_name].configure(fg_color=ACCENT)
            else:
                frame.pack_forget()
                self._tab_buttons[tab_name].configure(text_color=MUTED)
                self._tab_lines[tab_name].configure(fg_color="transparent")

    def _show_info(self) -> None:
        if self._info_window is not None and self._info_window.winfo_exists():
            self._info_window.focus()
            self._info_window.lift()
            return

        window = ctk.CTkToplevel(self)
        self._info_window = window
        window.title("About")
        window.geometry("560x600")
        window.resizable(False, False)
        window.configure(fg_color=BG)
        window.transient(self)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", self._close_info)

        ctk.CTkLabel(
            window,
            text="AoE4 Replay Launcher",
            text_color=TEXT,
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(pady=(24, 18))
        ctk.CTkLabel(
            window,
            text=(
                "AoE4 Replay Launcher is a free, unofficial, and open-source program "
                "that allows you to download and watch all available Age of "
                "Empires IV replays.\n\n"
                "Under normal circumstances, game replays become unusable after "
                "a patch. This program plays these replays by reconstructing the "
                "older game versions whenever necessary.\n\n"
                "You must own a legitimate Steam copy of Age of Empires IV to "
                "use it. External tools and files are downloaded automatically "
                "when needed.\n\n"
                "For any issues, suggestions, or contact, you can use the "
                "buttons below."
            ),
            text_color=MUTED,
            font=ctk.CTkFont(size=14),
            wraplength=500,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=30, pady=(0, 20))

        links = ctk.CTkFrame(window, fg_color="transparent")
        links.pack()
        for icon_path, icon_size, url in (
            ("social/github.png", (42, 42), INFO_GITHUB_URL),
            ("social/discord.png", (42, 42), INFO_DISCORD_URL),
            ("social/gmail.png", (36, 27), INFO_GMAIL_URL),
        ):
            icon = self._asset_image(icon_path, icon_size)
            ctk.CTkButton(
                links,
                text="",
                image=icon,
                width=66,
                height=66,
                corner_radius=12,
                fg_color=CARD,
                hover_color=NEUTRAL_HOVER,
                command=lambda target=url: webbrowser.open(target),
            ).pack(side="left", padx=8)

        # Decorative logo below the links — larger than the icons, not clickable.
        dclogo = self._asset_image("dclogo.png", (120, 120))
        if dclogo:
            ctk.CTkLabel(window, text="", image=dclogo, fg_color="transparent").pack(
                pady=(48, 26)
            )

        window.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - window.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - window.winfo_height()) // 2
        window.geometry(f"+{x}+{y}")
        window.focus()

    def _close_info(self) -> None:
        if self._info_window is not None:
            self._info_window.grab_release()
            self._info_window.destroy()
            self._info_window = None

    # ---- themed modal dialogs (replace the native white messageboxes) -------

    def _modal(self, title, message, *, buttons, danger=False, title_color=TEXT):
        """A panel-themed modal matching the sign-in / info dialogs.

        ``buttons`` is a list of ``(label, value)``; the last one is the primary
        action (accent, or red when ``danger``). Blocks until the user chooses
        and returns that value (``None`` if the window is just closed)."""
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.transient(self)
        prev_grab = None
        with contextlib.suppress(Exception):
            prev_grab = win.grab_current()  # e.g. the sign-in modal, to restore after
        result = {"value": None}

        ctk.CTkLabel(
            win, text=title, text_color=title_color,
            font=ctk.CTkFont(size=18, weight="bold"), wraplength=440, justify="left",
        ).pack(padx=28, pady=(24, 8), anchor="w")
        ctk.CTkLabel(
            win, text=message, text_color=MUTED, font=ctk.CTkFont(size=14),
            wraplength=440, justify="left",
        ).pack(padx=28, pady=(0, 20), anchor="w")

        def _choose(value):
            result["value"] = value
            with contextlib.suppress(Exception):
                win.grab_release()
            win.destroy()
            if prev_grab is not None:
                with contextlib.suppress(Exception):
                    if prev_grab.winfo_exists():
                        prev_grab.grab_set()

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(padx=28, pady=(0, 22), anchor="e")
        for index, (label, value) in enumerate(buttons):
            primary = index == len(buttons) - 1
            ctk.CTkButton(
                row, text=label, width=120, height=40, corner_radius=8,
                fg_color=(DANGER if danger else ACCENT) if primary else NEUTRAL,
                hover_color=(DANGER_HOVER if danger else ACCENT_HOVER)
                if primary else NEUTRAL_HOVER,
                text_color=TEXT, font=self.font_bold,
                command=lambda v=value: _choose(v),
            ).pack(side="left", padx=(8, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: _choose(None))

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        win.after(120, win.grab_set)
        win.focus()
        win.wait_window()
        return result["value"]

    def _info(self, title: str, message: str) -> None:
        self._modal(title, message, buttons=[("OK", True)])

    def _error(self, title: str, message: str) -> None:
        self._modal(title, message, buttons=[("OK", True)], title_color=LOSS)

    def _confirm(
        self, title: str, message: str, *,
        ok: str = "Yes", cancel: str = "Cancel", danger: bool = False,
    ) -> bool:
        return self._modal(
            title, message, buttons=[(cancel, False), (ok, True)], danger=danger,
        ) is True

    def _apply_window_icon(self) -> None:
        """Show the app logo on the window title bar / taskbar.

        customtkinter installs its own default icon shortly after the window is
        created, so apply ours now and again after a short delay so it sticks.
        """
        icon = ASSET_DIR / "app.ico"
        if not icon.is_file():
            return

        def _set() -> None:
            with contextlib.suppress(Exception):
                self.iconbitmap(str(icon))

        _set()
        self.after(250, _set)

    def _asset_image(
        self, relative_path: str, size: tuple[int, int]
    ) -> ctk.CTkImage | None:
        key = (relative_path, size)
        if key in self._images:
            return self._images[key]
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            image = ctk.CTkImage(Image.open(path), size=size)
        except (OSError, ValueError):
            return None
        self._images[key] = image
        return image

    def _civ_image(self, civ: str | None) -> ctk.CTkImage | None:
        filename = CIV_IMAGES.get(civ or "")
        return self._asset_image(f"flags/{filename}", (34, 19)) if filename else None

    def _league_image(self, rank: str) -> ctk.CTkImage | None:
        try:
            league, division = rank.lower().rsplit("_", 1)
        except ValueError:
            return None
        prefix = LEAGUE_IMAGES.get(league)
        if not prefix or division not in {"1", "2", "3"}:
            return None
        return self._asset_image(f"leagues/{prefix}{division}.png", (26, 39))

    def _country_image(self, country: str) -> ctk.CTkImage | None:
        code = country.strip().lower()
        if code in self._country_images:
            return self._country_images[code]
        source = self._country_sources.get(code)
        if source is None:
            return None
        image = ctk.CTkImage(source, size=(24, 18))
        self._country_images[code] = image
        return image

    def _load_country_flags(self, players: list[dict]) -> None:
        codes = {
            str(player.get("country") or "").strip().lower()
            for player in players
            if len(str(player.get("country") or "").strip()) == 2
        }
        for code in codes:
            if code in self._country_sources or code in self._country_failures:
                continue
            try:
                request = urllib.request.Request(
                    f"https://flagcdn.com/w40/{code}.png",
                    headers={"User-Agent": "aoe4-replay-launcher"},
                )
                with (
                    urllib.request.urlopen(request, timeout=5) as response,  # noqa: S310
                    Image.open(io.BytesIO(response.read())) as image,
                ):
                    self._country_sources[code] = image.convert("RGBA").copy()
            except (OSError, ValueError):
                self._country_failures.add(code)

    def _entry(self, parent: ctk.CTkFrame, placeholder: str, width: int) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            parent,
            placeholder_text=placeholder,
            width=width,
            height=38,
            corner_radius=8,
            border_color=DIVIDER,
            fg_color=CARD,
            font=self.font_normal,
        )

    def _accent_button(self, parent, text, width, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            width=width,
            height=38,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=self.font_bold,
            command=command,
        )

    def _build_profile_tab(self, parent: ctk.CTkFrame) -> None:
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(8, 8))
        self.p_entry = self._entry(top, "Profile ID or name", 240)
        self.p_entry.pack(side="left", padx=(6, 8), pady=4)
        self.p_entry.bind("<Return>", lambda _e: self.on_profile_search())
        self.p_search_btn = self._accent_button(
            top, f"{ICON_SEARCH}  Search", 110, self.on_profile_search
        )
        self.p_search_btn.pack(side="left", padx=(0, 8), pady=4)
        self.p_filter_box, self.p_filter_btn, self.p_filter_clear = self._make_filter_box(
            top, self._open_profile_filter, self._reset_profile
        )
        self.p_status = ctk.CTkLabel(
            top, text="", anchor="w", text_color=MUTED, font=self.font_normal
        )
        self.p_status.pack(side="left", padx=12)

        self.games = ctk.CTkScrollableFrame(
            parent,
            label_text=f"{ICON_GAMES}  Player games",
            fg_color=PANEL,
            label_fg_color=PANEL,
            label_text_color=TEXT,
            label_font=self.font_head,
        )
        self.games.pack(fill="both", expand=True, padx=4, pady=6)
        self.load_more_btn = ctk.CTkButton(
            parent,
            text=f"{ICON_RELOAD}  Load more",
            height=36,
            corner_radius=8,
            fg_color=NEUTRAL,
            hover_color=NEUTRAL_HOVER,
            font=self.font_bold,
            command=self.on_load_more,
        )

    def _build_h2h_tab(self, parent: ctk.CTkFrame) -> None:
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=(8, 8))
        self.entry1 = self._entry(top, "Profile ID or name 1", 190)
        self.entry2 = self._entry(top, "Profile ID or name 2", 190)
        self.entry1.pack(side="left", padx=(6, 8), pady=4)
        self.entry2.pack(side="left", padx=(0, 8), pady=4)
        self.search_btn = self._accent_button(top, f"{ICON_SEARCH}  Search", 110, self.on_search)
        self.search_btn.pack(side="left", padx=(0, 8), pady=4)
        self.h_filter_box, self.h_filter_btn, self.h_filter_clear = self._make_filter_box(
            top, self._open_h2h_filter, self._reset_h2h
        )

        self.matches = ctk.CTkScrollableFrame(
            parent,
            label_text=f"{ICON_KIND}  Head-to-head matches",
            fg_color=PANEL,
            label_fg_color=PANEL,
            label_text_color=TEXT,
            label_font=self.font_head,
        )
        self.matches.pack(fill="both", expand=True, padx=4, pady=6)

    # ---- row cards --------------------------------------------------------

    def _divider(self, parent: ctk.CTkFrame) -> None:
        ctk.CTkFrame(parent, width=1, height=22, fg_color=DIVIDER).pack(
            side="left", padx=6, pady=12
        )

    def _cell(self, parent, text, width, *, color=TEXT, anchor="w") -> ctk.CTkLabel:
        lbl = ctk.CTkLabel(
            parent, text=text, width=width, anchor=anchor, text_color=color, font=self.font_normal
        )
        lbl.pack(side="left", padx=(10, 2), pady=10)
        return lbl

    def _matchup(self, parent: ctk.CTkFrame, s: dict) -> ctk.CTkFrame:
        mf = ctk.CTkFrame(parent, fg_color="transparent")
        winner = s.get("winner")
        left_won = winner == s.get("_id1")
        right_won = winner == s.get("_id2")
        result_known = left_won or right_won

        self._match_player(
            mf, s["name1"], s.get("civ1"), left_won, result_known, side="left"
        )
        vs_image = self._asset_image("vs.png", (24, 24))
        ctk.CTkLabel(
            mf,
            text="" if vs_image else "vs",
            image=vs_image,
            text_color=MUTED,
            font=self.font_normal,
        ).pack(side="left", padx=8)
        self._match_player(
            mf, s["name2"], s.get("civ2"), right_won, result_known, side="right"
        )
        return mf

    def _match_player(
        self,
        parent: ctk.CTkFrame,
        name: str,
        civ: str | None,
        won: bool,
        result_known: bool,
        *,
        side: str,
    ) -> None:
        color = GREEN if won else LOSS if result_known else TEXT
        win_image = self._asset_image("win.png", (28, 25)) if won else None
        civ_image = self._civ_image(civ)

        if side == "left" and won:
            ctk.CTkLabel(
                parent, text="" if win_image else "WIN", image=win_image
            ).pack(side="left", padx=(0, 5))
        if side == "right" and civ:
            ctk.CTkLabel(
                parent,
                text="" if civ_image else civ,
                image=civ_image,
                width=38,
                text_color=MUTED,
                font=self.font_normal,
            ).pack(side="left", padx=(0, 5))

        ctk.CTkLabel(parent, text=name, text_color=color, font=self.font_bold).pack(side="left")

        if side == "left" and civ:
            ctk.CTkLabel(
                parent,
                text="" if civ_image else civ,
                image=civ_image,
                width=38,
                text_color=MUTED,
                font=self.font_normal,
            ).pack(side="left", padx=(5, 0))
        if side == "right" and won:
            ctk.CTkLabel(
                parent, text="" if win_image else "WIN", image=win_image
            ).pack(side="left", padx=(5, 0))

    def _game_card(self, parent: ctk.CTkFrame, summary: dict) -> None:
        row = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        row.pack(fill="x", padx=6, pady=4)
        when = summary["started_at"].strftime("%Y-%m-%d %H:%M") if summary["started_at"] else "?"
        self._cell(row, f"{ICON_CAL}  {when} UTC", 190)
        self._divider(row)
        self._cell(row, f"{ICON_KIND}  {summary['kind']}", 96)
        self._divider(row)
        self._cell(row, summary["map"] or "?", 120)
        self._divider(row)
        self._cell(row, f"{ICON_CLOCK}  {_fmt_duration(summary['duration'])}", 92)
        self._divider(row)
        warning = self.notify
        btn = self._accent_button(row, f"{ICON_DL}  Download", 126, command=lambda: None)
        btn.configure(command=lambda s=summary, b=btn, w=warning: self._download(s, b, w))
        btn.pack(side="right", padx=10, pady=8)
        self._matchup(row, summary).pack(side="left", fill="x", expand=True, padx=(4, 8))

    # ---- profile resolution (id directly, or pick from name suggestions) ---

    def _suggest(self, query, frame, status, target_entry, after_pick, on_ready=None) -> None:
        """Show name-search suggestions inline in ``frame``; picking one fills
        ``target_entry`` with the chosen id and (optionally) runs ``after_pick``.
        ``on_ready`` (if given) fires once fetching stops — the list is shown, or
        the search ended with an error / no results — so the Search button can be
        re-enabled without leaving a window for an overlapping search."""
        if len(query) < 2:
            self._info("Invalid", "Enter a profile ID, or a name (2+ characters).")
            if on_ready is not None:
                on_ready()
            return
        for child in frame.winfo_children():
            child.destroy()
        status.configure(text=f"Searching '{query}'…")
        args = (query, frame, status, target_entry, after_pick, on_ready)
        threading.Thread(target=self._suggest_worker, args=args, daemon=True).start()

    @staticmethod
    def _api_error_text(exc: Exception) -> str:
        # A failed request must read as a failure, not as "no results found".
        if isinstance(exc, aoe4world.Aoe4WorldError):
            return str(exc)
        return "Something went wrong contacting aoe4world."

    def _h2h_failed(self, message: str) -> None:
        self._set_h2h_busy(False)
        self.status.configure(text=message)

    def _profile_failed(self, message: str) -> None:
        self._set_profile_busy(False)
        with contextlib.suppress(Exception):
            self.load_more_btn.configure(state="normal")
        self.p_status.configure(text=message)

    def _suggest_worker(
        self, query, frame, status, target_entry, after_pick, on_ready=None
    ) -> None:
        try:
            players = aoe4world.search_players(query)
        except Exception as exc:  # noqa: BLE001 - never leave the suggestion box hung
            msg = self._api_error_text(exc)

            def fail() -> None:
                status.configure(text=msg)
                if on_ready is not None:
                    on_ready()

            self.after(0, fail)
            return
        self._load_country_flags(players)

        def render() -> None:
            self._render_suggestions(
                players, query, frame, status, target_entry, after_pick, on_ready
            )

        self.after(0, render)

    def _render_suggestions(
        self, players, query, frame, status, target_entry, after_pick, on_ready=None
    ) -> None:
        for child in frame.winfo_children():
            child.destroy()
        self.after(0, lambda: self._scroll_to_top(frame))
        if on_ready is not None:
            on_ready()  # waiting for the user now -> a fresh search is allowed
        if not players:
            status.configure(text=f"No profile named '{query}'.")
            return
        status.configure(text=f"{len(players)} match(es) for '{query}' - pick one")
        for player in players:
            rank = player["rank"] if player["rank"] != "unranked" else "-"
            rnum = f" #{player['rank_num']}" if player["rank_num"] else ""
            row = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=10)
            row.pack(fill="x", padx=6, pady=4)
            pid = player["profile_id"]
            select = self._accent_button(
                row,
                "Select",
                80,
                command=lambda p=pid: self._pick_suggestion(p, frame, target_entry, after_pick),
            )
            select.pack(side="right", padx=10, pady=8)

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=12, pady=6)
            ctk.CTkLabel(
                info, text=player["name"], text_color=TEXT, font=self.font_bold
            ).pack(side="left")

            country = player["country"] or ""
            country_image = self._country_image(country)
            ctk.CTkLabel(
                info,
                text="" if country_image else (country or "-"),
                image=country_image,
                text_color=MUTED,
                font=self.font_meta,
            ).pack(side="left", padx=(7, 0))
            ctk.CTkLabel(
                info,
                text=f"  |  id: {pid}  |  last: {player['last_game'] or '--'}",
                text_color=MUTED,
                font=self.font_normal,
            ).pack(side="left")

            league_image = self._league_image(rank)
            ctk.CTkLabel(
                info,
                text=rnum if league_image else f"  {rank}{rnum}",
                image=league_image,
                compound="left",
                text_color=MUTED,
                font=self.font_meta,
            ).pack(side="left", padx=(7, 0))

    def _pick_suggestion(self, profile_id, frame, target_entry, after_pick) -> None:
        target_entry.delete(0, "end")
        target_entry.insert(0, str(profile_id))
        for child in frame.winfo_children():
            child.destroy()
        if after_pick is not None:
            after_pick(profile_id)

    # ---- head-to-head search ----------------------------------------------

    @staticmethod
    def _resolve_value(value: str) -> tuple[str, object]:
        """Disambiguate an input field: a numeric string is only treated as a
        profile id if such a profile actually exists (a player's name can be all
        digits), otherwise it's resolved as a name."""
        if value.isdigit() and aoe4world.validate_profile(int(value)) is not None:
            return ("id", int(value))
        return ("name", value)

    def _set_h2h_busy(self, busy: bool) -> None:
        """Disable the head-to-head Search button while a request is running, so a
        second search can't overlap and let a stale result open over a newer one.
        The button is left enabled while waiting for the user to pick a profile."""
        self._searching = busy
        self.search_btn.configure(state="disabled" if busy else "normal")

    def _set_profile_busy(self, busy: bool) -> None:
        """Same, for the player-games (profile) tab's Search button."""
        self._pg_searching = busy
        self.p_search_btn.configure(state="disabled" if busy else "normal")

    def _scroll_to_top(self, scroll_frame) -> None:
        """Jump a CTkScrollableFrame back to the top, so a fresh result set isn't
        left scrolled where a previous load-more'd list ended."""
        with contextlib.suppress(Exception):
            scroll_frame._parent_canvas.yview_moveto(0.0)

    def on_search(self) -> None:
        if self._searching:
            return
        v1 = self.entry1.get().strip()
        v2 = self.entry2.get().strip()
        self._drop_h2h_filter()  # a fresh search isn't bound by an old date range
        self._set_h2h_busy(True)
        self.status.configure(text="Resolving…")
        threading.Thread(target=self._h2h_resolve, args=(v1, v2), daemon=True).start()

    def _drop_h2h_filter(self) -> None:
        self._h2h_since = None
        self._h2h_until = None
        self.h_filter_btn.configure(text=f"{ICON_CAL}  Date filter")
        self.h_filter_clear.pack_forget()
        self.h_filter_box.pack_forget()
        self.notify.configure(text="")

    def _h2h_resolve(self, v1: str, v2: str) -> None:
        try:
            r1, r2 = self._resolve_value(v1), self._resolve_value(v2)
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._h2h_failed(msg))
            return
        self.after(0, lambda: self._h2h_after_resolve(r1, r2))

    def _h2h_after_resolve(self, r1: tuple, r2: tuple) -> None:
        # Stay busy (Search disabled) through the name-suggestion fetch; the button
        # re-enables only once the list is shown (waiting for a pick) or at a
        # terminal state, so a second search can't overlap and clobber the results.
        # One Search click chains the whole flow: pick field 1 (if it's a name),
        # then field 2 (if a name), then list the matches automatically.
        if r1[0] == "name":  # field 1 is a name (or numeric non-profile) -> pick one
            self._suggest(
                r1[1], self.matches, self.status, self.entry1,
                after_pick=lambda pid: self._h2h_pick2(pid, r2),
                on_ready=lambda: self._set_h2h_busy(False),
            )
            return
        self._h2h_pick2(r1[1], r2)

    def _h2h_pick2(self, id1: int, r2: tuple) -> None:
        """Field 1 is resolved to ``id1``; now resolve field 2 (pick one if it's a
        name) and then list the head-to-head matches."""
        if r2[0] == "name":
            self._set_h2h_busy(True)  # disable again during the field-2 fetch
            self._suggest(
                r2[1], self.matches, self.status, self.entry2,
                after_pick=lambda pid: self._h2h_begin(id1, pid),
                on_ready=lambda: self._set_h2h_busy(False),
            )
            return
        self._h2h_begin(id1, r2[1])

    def _h2h_begin(self, id1: int, id2: int) -> None:
        if id1 == id2:
            self._set_h2h_busy(False)
            self._info("Invalid", "Enter two different profiles.")
            return
        self._start_h2h(id1, id2)

    def _start_h2h(self, id1: int, id2: int) -> None:
        self._h2h_ids = (id1, id2)
        self._set_h2h_busy(True)
        for child in self.matches.winfo_children():
            child.destroy()
        self.status.configure(text="Searching…")
        threading.Thread(target=self._search_worker, args=(id1, id2), daemon=True).start()

    def _search_worker(self, id1: int, id2: int) -> None:
        try:
            games = aoe4world.h2h_games(id1, id2, since=self._h2h_since)
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._h2h_failed(msg))
            return
        until = self._h2h_until
        summaries = []
        for game in games:
            s = aoe4world.match_summary(game, id1, id2)
            if not s["game_id"]:
                continue
            if until and s["started_at"] and s["started_at"].date() > until:
                continue
            s["_id1"], s["_id2"] = id1, id2
            s["_all_ids"] = [id1, id2] + [
                p for p in aoe4world._player_ids(game) if p not in (id1, id2)
            ]
            summaries.append(s)
        self.after(0, lambda: self._show_matches(summaries))

    def _show_matches(self, summaries: list[dict]) -> None:
        self._set_h2h_busy(False)
        self.status.configure(
            text=f"{len(summaries)} match(es)." if summaries else "No matches found."
        )
        for summary in summaries:
            self._game_card(self.matches, summary)
        # the date filter becomes available once matches are listed
        self.h_filter_box.pack(side="left", padx=(0, 6), pady=4)
        self.after(0, lambda: self._scroll_to_top(self.matches))

    # ---- date-range filter (calendar) -------------------------------------

    def _make_filter_box(self, parent, on_open, on_reset):
        """A (hidden) box holding the 'Date filter' button and, to its right, a
        reset (✕) button. Same height as the search/entry widgets."""
        box = ctk.CTkFrame(parent, fg_color="transparent")
        btn = ctk.CTkButton(
            box, text=f"{ICON_CAL}  Date filter", height=38, corner_radius=8,
            fg_color=NEUTRAL, hover_color=NEUTRAL_HOVER, font=self.font_bold, command=on_open,
        )
        btn.pack(side="left")
        clear = ctk.CTkButton(
            box, text="✕", width=38, height=38, corner_radius=8,
            fg_color=DANGER, hover_color=DANGER_HOVER, font=self.font_bold, command=on_reset,
        )
        return box, btn, clear

    def _pick_date_range(self, on_done) -> None:
        """Open a 'From' calendar, then a 'To' calendar; call on_done(from, to)."""
        CalendarPopup(
            self,
            "From date",
            lambda f: CalendarPopup(self, "To date", lambda t: on_done(f, t), initial=f),
        )

    def _open_profile_filter(self) -> None:
        self._pick_date_range(self._apply_profile_filter)

    def _apply_profile_filter(self, from_date: date, to_date: date) -> None:
        if to_date < from_date:
            from_date, to_date = to_date, from_date
        self._pg_since = from_date.isoformat()
        self._pg_until = to_date
        self.p_filter_btn.configure(text=f"{ICON_CAL}  {from_date} → {to_date}")
        self.p_filter_clear.pack(side="left", padx=(6, 0))  # to the right of the date text
        if self._pg_profile is not None:
            self._start_profile(self._pg_profile)

    def _reset_profile(self) -> None:
        """✕ resets the tab's top to its initial (just-opened) state."""
        self._pg_profile = None
        self._pg_since = None
        self._pg_until = None
        self._pg_page = 0
        self._pg_total = None
        self._pg_loaded = 0
        self.p_entry.delete(0, "end")
        self.p_filter_btn.configure(text=f"{ICON_CAL}  Date filter")
        self.p_filter_clear.pack_forget()
        self.p_filter_box.pack_forget()
        self.load_more_btn.pack_forget()
        for child in self.games.winfo_children():
            child.destroy()
        self.p_status.configure(text="")
        self.notify.configure(text="")

    def _open_h2h_filter(self) -> None:
        self._pick_date_range(self._apply_h2h_filter)

    def _apply_h2h_filter(self, from_date: date, to_date: date) -> None:
        if to_date < from_date:
            from_date, to_date = to_date, from_date
        self._h2h_since = from_date.isoformat()
        self._h2h_until = to_date
        self.h_filter_btn.configure(text=f"{ICON_CAL}  {from_date} → {to_date}")
        self.h_filter_clear.pack(side="left", padx=(6, 0))  # to the right of the date text
        if self._h2h_ids:
            self._start_h2h(*self._h2h_ids)

    def _reset_h2h(self) -> None:
        """✕ resets the tab's top to its initial (just-opened) state."""
        self._h2h_ids = None
        self._h2h_since = None
        self._h2h_until = None
        self.entry1.delete(0, "end")
        self.entry2.delete(0, "end")
        self.h_filter_btn.configure(text=f"{ICON_CAL}  Date filter")
        self.h_filter_clear.pack_forget()
        self.h_filter_box.pack_forget()
        for child in self.matches.winfo_children():
            child.destroy()
        self.status.configure(text="")
        self.notify.configure(text="")

    # ---- profile games (paginated on demand) ------------------------------

    def on_profile_search(self) -> None:
        if self._pg_searching:
            return
        value = self.p_entry.get().strip()
        self._drop_profile_filter()  # a fresh search isn't bound by an old date range
        self._set_profile_busy(True)
        self.p_status.configure(text="Resolving…")
        threading.Thread(target=self._profile_resolve, args=(value,), daemon=True).start()

    def _drop_profile_filter(self) -> None:
        self._pg_since = None
        self._pg_until = None
        self.p_filter_btn.configure(text=f"{ICON_CAL}  Date filter")
        self.p_filter_clear.pack_forget()
        self.p_filter_box.pack_forget()
        self.notify.configure(text="")

    def _profile_resolve(self, value: str) -> None:
        try:
            kind, resolved = self._resolve_value(value)
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._profile_failed(msg))
            return
        self.after(0, lambda: self._profile_after_resolve(kind, resolved))

    def _profile_after_resolve(self, kind: str, resolved: object) -> None:
        if kind == "id":
            self._start_profile(resolved)
            return
        # a name (or a numeric non-profile) -> pick one inline (Search re-enables
        # once the list is shown), then list its games.
        self.load_more_btn.pack_forget()
        self._suggest(
            resolved, self.games, self.p_status, self.p_entry,
            after_pick=self._start_profile,
            on_ready=lambda: self._set_profile_busy(False),
        )

    def _start_profile(self, pid: int) -> None:
        self.p_entry.delete(0, "end")
        self.p_entry.insert(0, str(pid))
        self._pg_profile = pid
        self._pg_page = 0
        self._pg_total = None
        self._pg_loaded = 0
        for child in self.games.winfo_children():
            child.destroy()
        self.load_more_btn.pack_forget()
        # the date filter becomes available once a player is selected
        self.p_filter_box.pack(side="left", padx=(0, 6), pady=4, before=self.p_status)
        self._set_profile_busy(True)
        self.p_status.configure(text="Loading…")
        threading.Thread(target=self._profile_begin, args=(pid,), daemon=True).start()

    def _profile_begin(self, pid: int) -> None:
        """First page of a profile. With an upper bound the API can't express,
        jump straight to the page where the [from, to] window begins instead of
        paging through everything newer than 'to'."""
        try:
            start_page = 1
            if self._pg_until:
                since = (self._pg_until + timedelta(days=1)).isoformat()
                _, prefix = aoe4world.player_games(pid, 1, since=since)
                if prefix:
                    start_page = prefix // 50 + 1
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._profile_failed(msg))
            return
        self._profile_worker(pid, start_page, append=False)

    def on_load_more(self) -> None:
        if self._pg_searching or self._pg_profile is None:
            return
        self._fetch_profile_page(append=True)

    def _fetch_profile_page(self, append: bool) -> None:
        self._pg_searching = True
        self.p_search_btn.configure(state="disabled")
        self.load_more_btn.configure(state="disabled")
        self.p_status.configure(text="Loading…")
        pid, page = self._pg_profile, self._pg_page + 1
        threading.Thread(
            target=self._profile_worker, args=(pid, page, append), daemon=True
        ).start()

    def _profile_worker(self, pid: int, page: int, append: bool) -> None:
        try:
            games, total = aoe4world.player_games(pid, page, since=self._pg_since)
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._profile_failed(msg))
            return
        until = self._pg_until
        summaries = []
        for game in games:
            opponent = aoe4world._opponent_of(game, pid)
            if opponent is None:
                continue
            s = aoe4world.match_summary(game, pid, opponent)
            if not s["game_id"]:
                continue
            # client-side upper bound (the API has no 'until'); since handles the lower
            if until and s["started_at"] and s["started_at"].date() > until:
                continue
            s["_id1"], s["_id2"] = pid, opponent
            s["_all_ids"] = [pid, opponent] + [
                p for p in aoe4world._player_ids(game) if p not in (pid, opponent)
            ]
            summaries.append(s)
        raw_count = len(games)
        self.after(0, lambda: self._show_games(summaries, total, raw_count, page, append))

    def _show_games(
        self, summaries: list[dict], total: int | None, raw_count: int, page: int, append: bool
    ) -> None:
        self._pg_searching = False
        self._pg_page = page
        self._pg_total = total
        self.p_search_btn.configure(state="normal")
        if not append:
            for child in self.games.winfo_children():
                child.destroy()
            self._pg_loaded = 0
        for summary in summaries:
            self._game_card(self.games, summary)
        self._pg_loaded += len(summaries)
        if not append:  # a fresh search -> show it from the top, not where load-more left off
            self.after(0, lambda: self._scroll_to_top(self.games))
        if self._pg_until:  # date-range filter active
            text = f"{self._pg_loaded} in range" if self._pg_loaded else "No games in range."
        else:
            total_txt = f" / {total} total" if total else ""
            text = f"{self._pg_loaded} shown{total_txt}" if self._pg_loaded else "No games."
        self.p_status.configure(text=text)
        # a full page (50) means more pages remain
        if raw_count >= 50:
            self.load_more_btn.configure(state="normal")
            self.load_more_btn.pack(padx=12, pady=(0, 8))
        else:
            self.load_more_btn.pack_forget()

    # ---- download ---------------------------------------------------------

    def _download(self, summary: dict, btn: ctk.CTkButton, warning: ctk.CTkLabel) -> None:
        btn.configure(state="disabled", text="Downloading…")
        game_id = summary["game_id"]
        dest = self.cfg.downloads_dir / f"AgeIV_Replay_{game_id}.rec"

        def worker() -> None:
            try:
                ok = aoe4world.download_replay(
                    game_id,
                    summary.get("_all_ids") or [summary["_id1"], summary["_id2"]],
                    dest,
                )
            except Exception as exc:  # noqa: BLE001 - surface any download failure to the user
                message = str(exc)
                self.after(0, lambda: self._download_error(btn, message))
                return
            if ok:
                self._info_cache[game_id] = summary  # show its details in the list
                # persist who was searched so the right player shows after a restart
                self._save_search_context(game_id, summary["_id1"], summary["_id2"])
            self.after(0, lambda: self._download_done(btn, ok, warning))

        threading.Thread(target=worker, daemon=True).start()

    def _download_done(self, btn: ctk.CTkButton, ok: bool, warning: ctk.CTkLabel) -> None:
        # the button/row may have been rebuilt (new search/reset) during the download
        if ok:
            with contextlib.suppress(Exception):
                btn.configure(text=f"Downloaded {ICON_CHECK}", state="disabled")
            warning.configure(text="")  # a successful download clears any prior warning
            self.refresh_downloads()
        else:
            with contextlib.suppress(Exception):
                btn.destroy()
            self._show_deleted_warning(warning)

    def _show_deleted_warning(self, warning: ctk.CTkLabel) -> None:
        # clear first so a repeat failure visibly re-appears (re-written, not stale)
        with contextlib.suppress(Exception):
            warning.configure(text="")
            warning.after(
                80, lambda: warning.configure(text="Replay has been deleted!", text_color=LOSS)
            )

    def _download_error(self, btn: ctk.CTkButton, message: str) -> None:
        with contextlib.suppress(Exception):
            btn.configure(text=f"{ICON_DL}  Download", state="normal")
        self._error("Download failed", message)

    # ---- downloaded list + play ------------------------------------------

    def _existing_replay(self, data: bytes) -> str | None:
        """Name of a download with the same replay content as ``data``, else None.

        Compares decompressed content (a download may be stored gzip-compressed
        while an import is raw), so the same replay isn't added twice.
        """
        digest = hashlib.sha1(data).digest()
        folder = self.cfg.downloads_dir
        for existing in folder.glob("*.rec") if folder.exists() else []:
            try:
                if hashlib.sha1(replay._read_replay_bytes(existing)).digest() == digest:
                    return existing.name
            except Exception:  # noqa: BLE001 - a corrupt existing file (e.g. truncated
                continue       # gzip -> EOFError) must be skipped, not abort the import
        return None

    def _import_replay(self) -> None:
        """Let the user add one or more replays from disk (.rec or .gz); store as .rec."""
        paths = filedialog.askopenfilenames(
            title="Select replay file(s)",
            filetypes=[("AoE4 replay", "*.rec *.gz"), ("All files", "*.*")],
        )
        if not paths:
            return
        added = 0
        skipped: list[str] = []
        for path in paths:
            src = Path(path)
            try:
                data = replay._read_replay_bytes(src)  # transparently decompresses gzip
            except Exception as exc:  # noqa: BLE001 - report any read/decompress failure
                skipped.append(f"{src.name}: {exc}")
                continue
            if b"AOE4_RE" not in data[:256]:
                skipped.append(f"{src.name}: not a valid AoE4 replay (no AOE4_RE header)")
                continue
            duplicate = self._existing_replay(data)
            if duplicate:
                skipped.append(f"{src.name}: already added as {duplicate}")
                continue
            self.cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
            dest = self.cfg.downloads_dir / f"{src.stem}.rec"
            counter = 2
            while dest.exists():
                dest = self.cfg.downloads_dir / f"{src.stem}_{counter}.rec"
                counter += 1
            dest.write_bytes(data)
            added += 1
        if added:
            self.refresh_downloads()
            self.status.configure(text=f"Added {added} replay(s).")
        if skipped:
            detail = "\n".join(skipped)
            if added:
                self._info("Some replays were skipped", detail)
            else:
                self._error("Import failed", detail)

    def refresh_downloads(self) -> None:
        for child in self.downloads.winfo_children():
            child.destroy()
        self._play_buttons.clear()
        folder = self.cfg.downloads_dir
        files = sorted(folder.glob("*.rec"), reverse=True) if folder.exists() else []
        context = self._load_search_context()  # game_id -> [searched_id, opponent_id]
        if not files:
            ctk.CTkLabel(
                self.downloads,
                text="No downloaded replays yet.",
                text_color=MUTED,
                font=self.font_normal,
            ).pack(anchor="w", padx=10, pady=8)
            return
        for path in files:
            row = ctk.CTkFrame(self.downloads, fg_color=CARD, corner_radius=10)
            row.pack(fill="x", padx=6, pady=4)
            game_id = aoe4world.game_id_from_name(path.name)
            info = self._info_cache.get(game_id) if game_id else None
            delete = ctk.CTkButton(
                row,
                text=f"{ICON_TRASH}  Delete",
                width=108,
                height=36,
                corner_radius=8,
                fg_color=DANGER,
                hover_color=DANGER_HOVER,
                font=self.font_bold,
                command=lambda p=path: self._delete(p),
            )
            delete.pack(side="right", padx=(0, 10), pady=10)
            play = ctk.CTkButton(
                row,
                text=f"{ICON_PLAY}  Play",
                width=96,
                height=36,
                corner_radius=8,
                fg_color=PLAY_GREEN,
                hover_color=PLAY_GREEN_HOVER,
                font=self.font_bold,
                command=lambda p=path: self._play(p),
            )
            play.pack(side="right", padx=6, pady=10)
            self._play_buttons.append(play)
            box = ctk.CTkFrame(row, fg_color="transparent")
            box.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            if info:
                self._render_download_summary(box, info)
                continue
            ctk.CTkLabel(
                box,
                text=f"{ICON_FILE}  {path.name}",
                anchor="w",
                text_color=TEXT,
                font=self.font_bold,
            ).pack(anchor="w")
            meta = ctk.CTkLabel(
                box,
                text=_match_text(info) if info else "Loading details…",
                anchor="w",
                justify="left",
                text_color=MUTED,
                font=self.font_meta,
            )
            meta.pack(anchor="w")
            if game_id and info is None:
                ctx = context.get(str(game_id))
                ids = (int(ctx[0]), int(ctx[1])) if ctx and len(ctx) >= 2 else None
                args = (game_id, meta, ids)
                threading.Thread(target=self._fetch_info, args=args, daemon=True).start()
        count = len(files)
        ctk.CTkLabel(
            self.downloads,
            text=f"{count} replay{'s' if count != 1 else ''}",
            text_color=MUTED,
            font=self.font_meta,
        ).pack(pady=(8, 4))

    def _render_download_summary(self, parent: ctk.CTkFrame, summary: dict) -> None:
        for child in parent.winfo_children():
            child.destroy()
        when = (
            summary["started_at"].strftime("%Y-%m-%d %H:%M")
            if summary["started_at"]
            else "?"
        )
        ctk.CTkLabel(
            parent,
            text=f"{when} UTC  |  {summary['kind']}  |  {summary['map']}"
            f"  |  {_fmt_duration(summary['duration'])}",
            anchor="w",
            text_color=MUTED,
            font=self.font_meta,
        ).pack(anchor="w")
        self._matchup(parent, summary).pack(anchor="w", pady=(4, 0))

    # --- search context: remember which profiles the user searched for, so a
    # downloaded team/FFA replay still shows that player across sessions ---------

    def _search_context_path(self) -> Path:
        return self.cfg.downloads_dir / ".search-context.json"

    def _load_search_context(self) -> dict:
        with contextlib.suppress(Exception):
            data = json.loads(self._search_context_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        return {}

    def _save_search_context(self, game_id: int, id1: int, id2: int) -> None:
        with contextlib.suppress(Exception):
            path = self._search_context_path()
            data = self._load_search_context()
            data[str(game_id)] = [int(id1), int(id2)]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)

    def _fetch_info(
        self, game_id: int, label: ctk.CTkLabel, ids: tuple[int, int] | None = None
    ) -> None:
        summary = aoe4world.game_summary(game_id, ids)
        if not summary:
            return
        self._info_cache[game_id] = summary

        def update() -> None:
            # the row may have been rebuilt since the fetch started
            with contextlib.suppress(Exception):
                self._render_download_summary(label.master, summary)

        self.after(0, update)

    def _delete(self, path: Path) -> None:
        if self._playing:
            return
        if not self._confirm(
            "Delete replay", f"Delete {path.name}?", ok="Delete", danger=True
        ):
            return
        try:
            path.unlink()
        except OSError as exc:
            self._error("Delete failed", str(exc))
            return
        self.refresh_downloads()

    def _play(self, path: Path) -> None:
        if self._playing:
            return
        if launch.is_game_running():
            self._info(
                "Game running", "Age of Empires IV is already running. Close it first."
            )
            return
        # Make sure we know where the game is installed (auto-detect, else ask).
        if not self._ensure_game_install():
            return
        # Steam must be open and signed in to launch the game (for any build) —
        # check this before the connect dialog, otherwise (e.g. game not found)
        # we'd wrongly jump straight to the account-connect dialog.
        try:
            launch.ensure_steam_running(self.cfg)
        except Exception as exc:  # noqa: BLE001 - surface "open Steam" as a warning
            self._info("Steam not running", str(exc))
            return
        # An old build installs the persistent Steam wrapper, which needs a one-time
        # Steam restart. Warn the user (the account picker may appear) before it runs.
        restart_warn = False
        with contextlib.suppress(Exception):
            restart_warn = (
                not service._is_current_build(self.cfg, path)
                and launch.wrapper_restart_pending(self.cfg)
            )
        if restart_warn:
            if not self._confirm(
                "Steam will restart once",
                "To set up the replay connection, we'll restart Steam once. "
                "If the account picker appears, please select your account manually to "
                "continue.",
                ok="Continue", cancel="Cancel",
            ):
                return
            # Do the one-time restart NOW — before the download/sign-in — off the UI
            # thread, then continue to the (possibly needed) sign-in and play.
            self._setup_wrapper_then_play(path)
            return
        self._continue_play(path)

    def _continue_play(self, path: Path) -> None:
        # Connect a Steam account only when it's actually needed: an old build has
        # to be downloaded, but a replay on the installed build plays without it.
        if self._needs_steam_connection(path):
            self._prompt_steam_login(
                on_success=lambda: self._start_play(path, just_connected=True)
            )
            return
        self._start_play(path)

    def _setup_wrapper_then_play(self, path: Path) -> None:
        """Install the Steam wrapper (the one-time restart) up front, off the UI
        thread, then continue. Runs right after the user confirms — before any
        download or Steam sign-in — so the restart isn't deferred to the end."""
        self._playing = True  # block re-entry while Steam restarts
        for btn in self._play_buttons:
            btn.configure(state="disabled")
        self.status.configure(text="Setting up Steam for replays…")

        def worker() -> None:
            err = None
            try:
                launch.ensure_steam_wrapper(self.cfg)
            except Exception as exc:  # noqa: BLE001 - surface a restart failure
                err = str(exc)
            self.after(0, lambda: self._wrapper_setup_done(path, err))

        threading.Thread(target=worker, daemon=True).start()

    def _wrapper_setup_done(self, path: Path, err: str | None) -> None:
        self._playing = False  # _continue_play / _start_play re-acquires it
        for btn in self._play_buttons:
            btn.configure(state="normal")
        self.notify.configure(text="")
        if err:
            self.status.configure(text="Ready.")
            self._error("Steam setup failed", err)
            return
        self._continue_play(path)

    def _game_exe_at(self, path: Path) -> bool:
        with contextlib.suppress(Exception):
            launch.find_executable(path)
            return True
        return False

    def _ensure_game_install(self) -> bool:
        """Auto-detected install missing? Let the user pick it (saved to config)."""
        if self._game_exe_at(self.cfg.steam_install):
            return True
        self._info(
            "Find Age of Empires IV",
            "Couldn't locate your Age of Empires IV install automatically.\n\n"
            "Please select the game's install folder — the one that contains "
            "RelicCardinal.exe (e.g. ...\\steamapps\\common\\Age of Empires IV).",
        )
        picked = filedialog.askdirectory(title="Select the Age of Empires IV install folder")
        if not picked:
            self._info(
                "Game not found", "The Age of Empires IV install folder is required to play."
            )
            return False
        if not self._game_exe_at(Path(picked)):
            self._info(
                "Wrong folder",
                "That folder doesn't contain the game (RelicCardinal.exe). Try again.",
            )
            return False
        config.set_path_overrides(self.cfg.project_root, {"steam_install": picked})
        self.cfg = config.load()
        return True

    def _needs_steam_connection(self, path: Path) -> bool:
        if self.cfg.steam_username:  # already connected -> downloads are silent
            return False
        with contextlib.suppress(Exception):
            replay_version = replay.read_version(path)
            installed = launch.installed_game_version(self.cfg)
            if replay_version is not None and replay_version == installed:
                return False  # current build -> no download -> no connection needed
        # A build the user has saved locally plays with no download, so it needs no
        # Steam connection (offline / cached playback).
        with contextlib.suppress(Exception):
            if service.replay_build_is_saved_locally(self.cfg, path):
                return False
        return True  # old / unknown build -> will download -> needs a connection

    def _start_play(self, path: Path, just_connected: bool = False) -> None:
        self._playing = True
        self._cancel_event = threading.Event()
        for btn in self._play_buttons:
            btn.configure(state="disabled")
        self.status.configure(text=f"Playing: {path.name}")
        warning = self.notify
        # Fetching the manifest after sign-in can take a few minutes before any
        # download percentage appears, so show a reassuring message in that gap;
        # the first progress report below replaces it.
        warning.configure(
            text="Steam sign-in successful — preparing the replay…" if just_connected
            else "Preparing the replay…",
            text_color=GREEN,
        )

        def report(stage: str, pct: float | None) -> None:
            self.after(0, lambda s=stage, p=pct: self._play_progress(warning, s, p))

        def worker() -> None:
            error = None
            built = None
            cancelled = False
            auth_failed = False
            try:
                built = service.watch_replay(
                    self.cfg, path, progress=report, cancel=self._cancel_event
                )
            except service.DownloadCancelled:
                cancelled = True  # user cancelled — not a failure, no error modal
            except service.SteamAuthError:
                auth_failed = True  # saved login expired — clear it and reconnect
            except Exception as exc:  # noqa: BLE001 - report launch/build failures
                error = str(exc)
            self.after(
                0,
                lambda: self._play_finished(
                    error, warning, built, cancelled, auth_failed, path
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _show_cancel(self, show: bool) -> None:
        """Show the tiny ✕ cancel box next to the progress (only while downloading)."""
        if not hasattr(self, "_cancel_button"):
            return
        if show:
            if not self._cancel_button.winfo_ismapped():
                self._cancel_button.configure(state="normal")
                self._cancel_button.pack(side="left", padx=(6, 0))
        else:
            self._cancel_button.pack_forget()

    def _cancel_download(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._cancel_button.configure(state="disabled")  # stays a tiny ✕, just greyed
        self.status.configure(text="Cancelling download…")

    def _play_progress(self, warning: ctk.CTkLabel, stage: str, pct: float | None) -> None:
        # the download (live-file copy + network fetch) is the only cancellable phase
        self._show_cancel(stage in ("seed", "download"))
        # green, shown where the deleted-replay warning lives; removed when done
        if stage == "seed":
            # disk copy of reusable game files, before any download starts
            warning.configure(text="Preparing required files…", text_color=GREEN)
            return
        if stage == "rebuild":
            warning.configure(
                text="Saved build was out of date — rebuilding…", text_color=GREEN
            )
            return
        if stage == "store":  # download done, now verifying + saving (not cancellable)
            warning.configure(text="Saving downloaded files…", text_color=GREEN)
            return
        if pct is None:
            warning.configure(text="")
            return
        label = {
            "download": "Downloading required files",
            "build": "Building the launch build",
        }.get(stage, "Working")
        warning.configure(text=f"{label}… {pct:.0f}%", text_color=GREEN)

    @staticmethod
    def _fmt_size(num: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if num < 1024:
                return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
            num /= 1024
        return f"{num:.1f} TB"

    def _build_saved_tab(self, parent: ctk.CTkFrame) -> None:
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=18, pady=14)
        self.saved_total = ctk.CTkLabel(container, text="", text_color=MUTED, font=self.font_meta)
        self.saved_total.pack(anchor="w", pady=(0, 8))
        self.saved_list = ctk.CTkScrollableFrame(container, fg_color="transparent")
        self.saved_list.pack(fill="both", expand=True)

    def _refresh_saved_builds(self) -> None:
        if not hasattr(self, "saved_list"):
            return
        for child in self.saved_list.winfo_children():
            child.destroy()
        saved = buildcache.load_saved(self.cfg) or set()
        if not saved:
            self.saved_total.configure(text="")
            ctk.CTkLabel(
                self.saved_list,
                text="No saved builds yet.\n\nAfter watching an old replay you'll be "
                "asked if you want to keep its build for instant replays.",
                text_color=MUTED, font=self.font_normal, justify="left",
            ).pack(anchor="w", padx=6, pady=24)
            return
        self._saved_size_labels = {}
        for build_id in sorted(saved, reverse=True):
            row = ctk.CTkFrame(self.saved_list, fg_color=CARD, corner_radius=10)
            row.pack(fill="x", padx=6, pady=4)
            ctk.CTkButton(
                row, text="Remove", width=84, height=34, corner_radius=8,
                fg_color=NEUTRAL, hover_color=NEUTRAL_HOVER, font=self.font_bold,
                command=lambda b=build_id: self._delete_saved_build(b),
            ).pack(side="right", padx=10, pady=8)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            ctk.CTkLabel(
                info, text=build_id, text_color=TEXT, font=self.font_bold
            ).pack(side="left")
            size_label = ctk.CTkLabel(
                info, text="    calculating size…", text_color=MUTED, font=self.font_meta
            )
            size_label.pack(side="left", padx=(8, 0))
            self._saved_size_labels[build_id] = size_label
        self.saved_total.configure(text=f"{len(saved)} saved build(s) · calculating…")

        # Sizing rglob-stats ~14k files per build; do it off the UI thread so the
        # tab opens instantly even on a slow disk.
        ids = list(saved)

        def worker() -> None:
            sizes = {bid: (buildcache.delta_size(buildcache.build_dir(self.cfg, bid))
                           if buildcache.build_dir(self.cfg, bid).is_dir() else None)
                     for bid in ids}
            self.after(0, lambda: self._apply_saved_sizes(sizes))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_saved_sizes(self, sizes: dict) -> None:
        labels = getattr(self, "_saved_size_labels", {})
        total = 0
        for build_id, size in sizes.items():
            label = labels.get(build_id)
            if label is None or not label.winfo_exists():
                continue
            if size is None:
                label.configure(text="    not built — rebuilt on next play")
            else:
                total += size
                label.configure(text=f"    {self._fmt_size(size)}")
        with contextlib.suppress(Exception):
            self.saved_total.configure(
                text=f"{len(sizes)} saved build(s) · {self._fmt_size(total)} on disk"
            )

    def _delete_saved_build(self, build_id: str) -> None:
        if self._playing:
            self._info(
                "Busy",
                "A replay is playing right now. Close it before removing a build —"
                " the one in use may be this build.",
            )
            return
        if not self._confirm(
            "Remove this saved build?",
            f"Remove the saved {build_id} build?\n\n"
            "Replays from this build will start slower next time (it gets rebuilt), "
            "but it won't need to be downloaded again.",
            ok="Remove", danger=True,
        ):
            return
        with contextlib.suppress(Exception):
            buildcache.delete_build(self.cfg, build_id)
        self._refresh_saved_builds()

    def _on_close(self) -> None:
        # Cancel an in-flight download so DepotDownloader is killed rather than
        # orphaned (it would otherwise keep running after the window closes).
        # Give the watchdog a moment to act before we exit.
        if self._playing and self._cancel_event is not None:
            self._cancel_event.set()
            time.sleep(0.4)
        # Don't clean while a play/reconstruction is active — its build dir is in
        # use (and a game may be running); startup cleanup catches it next time.
        if not self._playing:
            with contextlib.suppress(Exception):
                buildcache.cleanup(self.cfg)
        self.destroy()

    def _play_finished(
        self,
        error: str | None,
        warning: ctk.CTkLabel,
        built_build_id: str | None = None,
        cancelled: bool = False,
        auth_failed: bool = False,
        path: Path | None = None,
    ) -> None:
        self._playing = False
        self._cancel_event = None
        self._show_cancel(False)
        for btn in self._play_buttons:
            btn.configure(state="normal")
        warning.configure(text="")  # remove any download progress message
        if cancelled:
            self.status.configure(text="Download cancelled.")
            return
        if auth_failed:
            # The saved Steam login expired/was rejected. Clear it and open the
            # connect dialog straight away (instead of a "press Play again" notice),
            # then retry the same replay automatically once reconnected.
            with contextlib.suppress(Exception):
                config.set_steam_username(self.cfg.project_root, "")
                self.cfg = config.load()
            self.status.configure(text="Steam login expired — reconnect to continue.")
            if path is not None:
                self._prompt_steam_login(
                    on_success=lambda: self._start_play(path, just_connected=True)
                )
            return
        if error:
            self.status.configure(text="Playback failed.")
            self._error("Playback failed", error)
            return
        self.status.configure(text="Ready.")
        if built_build_id:
            self._maybe_offer_keep(built_build_id)

    def _maybe_offer_keep(self, build_id: str) -> None:
        """Offer to keep a reconstructed build for instant future opens — at most
        once per build per session, and never if it is already saved."""
        if build_id in self._keep_asked:
            return
        with contextlib.suppress(Exception):
            if buildcache.is_saved(self.cfg, build_id):
                return
        self._keep_asked.add(build_id)  # asked once this session, whatever the answer
        size = 0
        with contextlib.suppress(Exception):
            size = buildcache.delta_size(buildcache.build_dir(self.cfg, build_id))
        disk = f"about {self._fmt_size(size)} of" if size else "more"
        keep = self._confirm(
            "Keep this build for faster replays?",
            f"Save the {build_id} build?\n\n"
            f"Saving it uses {disk} disk space, but replays from this build will open "
            "instantly next time — no rebuilding.",
            ok="Save", cancel="Not now",
        )
        if not keep:
            return
        with contextlib.suppress(Exception):
            buildcache.mark_saved(self.cfg, build_id)
            self.status.configure(text=f"Saved {build_id} for instant replays.")
            self._refresh_saved_builds()

    # ---- auto-update (Velopack) ------------------------------------------

    def _check_for_updates(self) -> None:
        """Background check for a newer release; offer to install if found.

        Silent no-op when not running as a Velopack install (dev/source run,
        the legacy build, or offline) — UpdateManager raises in those cases.
        """
        try:
            import velopack

            mgr = velopack.UpdateManager(velopack.GithubSource(INFO_GITHUB_URL, None, False))
            info = mgr.check_for_updates()
        except Exception:  # noqa: BLE001 - an update check must never break startup
            return
        if not info:
            return
        self.after(0, lambda: self._offer_update(mgr, info))

    def _offer_update(self, mgr, info) -> None:
        version = ""
        with contextlib.suppress(Exception):
            version = str(info.target_full_release.version)
        label = f" ({version})" if version else ""
        if not self._confirm(
            "Update available",
            f"A new version{label} of AoE4 Replay Launcher is available.\n\n"
            "Your downloaded builds, saved builds and settings are kept — only the "
            "app itself is updated. Update now?",
            ok="Update", cancel="Later",
        ):
            return
        threading.Thread(target=lambda: self._run_update(mgr, info), daemon=True).start()

    def _run_update(self, mgr, info) -> None:
        def progress(pct: int) -> None:
            self.after(
                0,
                lambda p=pct: self.status.configure(text=f"Downloading update… {int(p)}%"),
            )

        try:
            self.after(0, lambda: self.status.configure(text="Downloading update…"))
            mgr.download_updates(info, progress)
            self.after(0, lambda: self.status.configure(text="Installing update…"))
            mgr.apply_updates_and_restart(info)  # exits and relaunches the new version
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda e=exc: self._error(
                "Update failed",
                f"The update could not be applied:\n{e}\n\nYou can still download the "
                "latest version manually from the Releases page.",
            ))


_single_instance_handle = None  # kept alive for the process lifetime


def _acquire_single_instance(name: str = "AoE4ReplayLauncher_SingleInstance") -> bool:
    """True if we hold the single-instance lock; False if another panel has it.

    Uses a named Windows mutex: the OS releases it automatically when the process
    exits (even on a crash), so a previous run can never leave a stale lock that
    blocks the next launch. Fails open (returns True) if the mutex can't be made,
    so a quirk never wrongly locks the user out.
    """
    global _single_instance_handle
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 - never block startup on a lock check failure
        return True
    if not handle:
        return True
    if already:
        return False
    _single_instance_handle = handle  # hold it open so the lock stays for our lifetime
    return True


def run(cfg: Config) -> None:
    if sys.version_info < (3, 12):
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "AoE4 Replay Launcher needs Python 3.12 or newer.\n\n"
            f"You're running Python {sys.version_info.major}.{sys.version_info.minor}. "
            "Install Python 3.12+ from python.org, recreate the .venv, then run "
            "'pip install .' again (see README.md).",
            "AoE4 Replay Launcher",
            0x10,
        )
        return
    if not _acquire_single_instance():
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "AoE4 Replay Launcher is already running.\n\n"
            "Check your taskbar for the open window.",
            "AoE4 Replay Launcher",
            0x40,  # information icon
        )
        return
    try:
        Panel(cfg).mainloop()
    except Exception:  # noqa: BLE001 - last resort so a startup failure isn't silent
        import ctypes
        import traceback

        message = (
            "AoE4 Replay Launcher couldn't start.\n\n"
            f"{traceback.format_exc()}\n"
            "If this is a fresh install, run 'pip install .' in the project folder "
            "(see README.md)."
        )
        # native MessageBox works even if Tcl/Tk itself failed to initialise
        ctypes.windll.user32.MessageBoxW(0, message, "AoE4 Replay Launcher", 0x10)
        raise
