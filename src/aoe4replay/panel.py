"""customtkinter desktop panel: find, download and play head-to-head replays.

Double-clicking AoE4-Replay-Launcher.vbs opens this. Enter two profile
ids (or find them by name), list every head-to-head match, download a replay
(tried from both players' perspectives), and play any downloaded replay through
the normal watch flow. The panel stays open across game launches.
"""

from __future__ import annotations

import calendar as _calendar
import contextlib
import ctypes
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
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

# DPI awareness has to be selected before tkinter/customtkinter are imported.
# CustomTkinter otherwise registers its own mode first and Windows virtualizes a
# 3072 px work area as 3840 Tk pixels at 125% display scaling.  That makes right-
# aligned controls land outside the physical window and leaves visible content
# looking disproportionately small.
if sys.platform == "win32":
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

import tkinter as tk  # noqa: E402
from tkinter import filedialog  # noqa: E402

import customtkinter as ctk  # noqa: E402 - must follow _ensure_tcl_tk()
from PIL import (  # noqa: E402
    Image,
    ImageChops,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageGrab,
    ImageTk,
)

# Keep the reference coordinate system physical-pixel exact on mixed-DPI
# desktops.  Windows remains per-monitor DPI aware, while CustomTkinter's
# second widget-scaling pass is disabled so 1672x941 never becomes a 125%
# interpretation of the same layout on another monitor.
if sys.platform == "win32":
    ctk.deactivate_automatic_dpi_awareness()

from . import (  # noqa: E402
    __version__,
    aoe4world,
    buildcache,
    config,
    launch,
    nethttp,
    replay,
    service,
    steamqr,
)
from . import (  # noqa: E402
    ui_theme as theme,
)

# ---- palette / glyphs -----------------------------------------------------

# Keep the short names used throughout this presentation module while sourcing
# every visual decision from the dedicated AoE4 theme layer.
BG = theme.BG
PANEL = theme.PANEL
CARD = theme.CARD
ACCENT = theme.ACCENT
ACCENT_HOVER = theme.ACCENT_HOVER
DANGER = theme.DANGER
DANGER_HOVER = theme.DANGER_HOVER
NEUTRAL = theme.NEUTRAL
NEUTRAL_HOVER = theme.NEUTRAL_HOVER
GREEN = theme.GREEN
PLAY_GREEN = theme.PLAY_GREEN
PLAY_GREEN_HOVER = theme.PLAY_GREEN_HOVER
LOSS = theme.LOSS
TEXT = theme.TEXT
MUTED = theme.MUTED
DIVIDER = theme.DIVIDER
GOLD = theme.GOLD
GOLD_BRIGHT = theme.GOLD_BRIGHT
GOLD_DARK = theme.GOLD_DARK
BRONZE = theme.BRONZE

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

ASSET_DIR = Path(__file__).with_name("assets")

# Player-game headers and virtual rows must use one coordinate system. RESULT
# is a dedicated fixed-width column whose centre is the exact window/canvas
# centre; this puts every Victory badge on the header's central diamond axis.
# The four information columns retain their established proportions, PLAYERS
# absorbs the remaining right-hand space, and ACTION keeps a safe minimum.
RESULT_COLUMN_WEIGHTS = (16, 10, 12, 9)
RESULT_COLUMN_WIDTH = 120.0
RESULT_ACTION_FRACTION = 0.12
RESULT_ACTION_MIN_WIDTH = 156.0
RESULT_CONTENT_LEFT = 6.0
RESULT_CONTENT_RIGHT = 26.0
WINDOW_RESIZE_DRAG_THRESHOLD = 7


def _result_column_bounds(
    width: int,
    *,
    include_result: bool = True,
) -> tuple[tuple[float, float], ...]:
    """Return the canonical Profile Games column bounds for a canvas width.

    ``include_result`` controls whether RESULT is painted, not the geometry.
    Head to Head leaves that slot visually empty so every shared heading and
    value remains on exactly the same x coordinate as Profile Games.
    """

    left = RESULT_CONTENT_LEFT
    right = max(left + 6.0, float(width) - RESULT_CONTENT_RIGHT)
    available = right - left
    action_width = min(
        available,
        max(RESULT_ACTION_MIN_WIDTH, available * RESULT_ACTION_FRACTION),
    )
    action_left = right - action_width
    result_half = RESULT_COLUMN_WIDTH / 2
    result_center = min(
        max(float(width) / 2, left + result_half),
        action_left - result_half,
    )
    result_left = result_center - result_half
    result_right = result_center + result_half
    table_width = max(0.0, result_left - left)
    weight_total = float(sum(RESULT_COLUMN_WEIGHTS))
    bounds: list[tuple[float, float]] = []
    cursor = left
    for weight in RESULT_COLUMN_WEIGHTS:
        next_cursor = cursor + table_width * weight / weight_total
        bounds.append((cursor, next_cursor))
        cursor = next_cursor
    # Eliminate accumulated floating-point drift at the fixed RESULT boundary;
    # headers and rows should share the exact same adjoining coordinate.
    if bounds:
        bounds[-1] = (bounds[-1][0], result_left)
    bounds.append((result_left, result_right))
    bounds.append((result_right, action_left))
    bounds.append((action_left, right))
    return tuple(bounds)


def _metallic_gold_from_mask(mask: Image.Image, *, outline: bool = True) -> Image.Image:
    """Fill an antialiased text mask with a restrained, embossed gold finish.

    Lettering uses one uninterrupted transition from a pale upper gold to a
    redder lower bronze. Keeping this treatment in one helper gives every
    heading the same metallic finish.
    """

    mask = mask.convert("L")
    width, height = mask.size
    result = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    if not mask.getbbox():
        return result

    top_gold = (255, 226, 132)
    bottom_bronze = (178, 78, 24)
    gradient = Image.new("RGBA", mask.size)
    draw = ImageDraw.Draw(gradient)
    for y in range(height):
        position = y / max(1, height - 1)
        color = tuple(
            round(
                top_gold[channel]
                + (bottom_bronze[channel] - top_gold[channel]) * position
            )
            for channel in range(3)
        )
        draw.line((0, y, width, y), fill=(*color, 255))
    gradient.putalpha(mask)

    if outline:
        radius = max(1, round(height * 0.018))
        expanded = mask.filter(ImageFilter.MaxFilter(radius * 2 + 1))
        border = ImageChops.subtract(expanded, mask)
        shadow = Image.new("L", mask.size, 0)
        shadow.paste(expanded, (max(1, radius), max(1, radius)))
        shadow = ImageChops.subtract(shadow, expanded)
        shadow_layer = Image.new("RGBA", mask.size, (9, 4, 1, 0))
        shadow_layer.putalpha(shadow.point(lambda value: round(value * 0.72)))
        result.alpha_composite(shadow_layer)
        border_layer = Image.new("RGBA", mask.size, (48, 24, 7, 0))
        border_layer.putalpha(border)
        result.alpha_composite(border_layer)

    result.alpha_composite(gradient)

    # A hairline highlight on upper glyph edges sells the engraved-metal effect.
    shifted_down = Image.new("L", mask.size, 0)
    shifted_down.paste(mask, (0, max(1, round(height * 0.012))))
    upper_edge = ImageChops.subtract(mask, shifted_down)
    highlight = Image.new("RGBA", mask.size, (255, 246, 190, 0))
    highlight.putalpha(upper_edge.point(lambda value: round(value * 0.58)))
    result.alpha_composite(highlight)
    return result


def _gold_text_raster(
    text: str,
    font_size: int,
    *,
    align: str = "left",
) -> Image.Image:
    """Render crisp transparent Palatino lettering with the shared gold finish."""

    scale = 4
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    try:
        font = ImageFont.truetype(str(font_dir / "palab.ttf"), font_size * scale)
    except OSError:
        try:
            font = ImageFont.truetype(str(font_dir / "georgiab.ttf"), font_size * scale)
        except OSError:
            font = ImageFont.load_default()
    probe = Image.new("L", (1, 1))
    draw = ImageDraw.Draw(probe)
    spacing = max(scale, round(font_size * scale * 0.10))
    bounds = draw.multiline_textbbox(
        (0, 0), text, font=font, spacing=spacing, align=align
    )
    pad = 3 * scale
    width = max(scale, math.ceil(bounds[2] - bounds[0] + pad * 2))
    height = max(scale, math.ceil(bounds[3] - bounds[1] + pad * 2))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).multiline_text(
        (pad - bounds[0], pad - bounds[1]),
        text,
        font=font,
        fill=255,
        spacing=spacing,
        align=align,
    )
    gold = _metallic_gold_from_mask(mask)
    return gold.resize(
        (max(1, round(width / scale)), max(1, round(height / scale))),
        Image.Resampling.LANCZOS,
    )

INFO_GITHUB_URL = "https://github.com/EKYavsil/AoE4-Replay-Launcher"

# Game-mode filter: dropdown label -> aoe4world `leaderboard` value (None = no filter,
# applied server-side alongside the date `since`). FFA is quick-match only in AoE4
# (qm_ffa); there is no ranked FFA leaderboard.
_MODE_LEADERBOARDS = {
    "All modes": None,
    "Ranked 1v1": "rm_1v1",
    "Ranked 2v2": "rm_2v2",
    "Ranked 3v3": "rm_3v3",
    "Ranked 4v4": "rm_4v4",
    "Quick Match 1v1": "qm_1v1",
    "Quick Match 2v2": "qm_2v2",
    "Quick Match 3v3": "qm_3v3",
    "Quick Match 4v4": "qm_4v4",
    "Free-for-All": "qm_ffa",
}
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
        if hasattr(parent, "_apply_icon_to_window"):
            parent._apply_icon_to_window(self)
        self.on_pick = on_pick
        self._view = (initial or date.today()).replace(day=1)
        self._bold = theme.font(13, weight="bold")
        self._norm = theme.font(12)

        head = ctk.CTkFrame(self, **theme.panel_style(strong=True))
        head.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkButton(
            head, text="‹", width=34, height=30, command=self._prev,
            **theme.button_style("gold"),
        ).pack(side="left")
        self._title = ctk.CTkLabel(head, text="", text_color=TEXT, font=self._bold, width=150)
        self._title.pack(side="left", expand=True)
        ctk.CTkButton(
            head, text="›", width=34, height=30, command=self._next,
            **theme.button_style("gold"),
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
                    self._grid, text=str(day), width=34, height=30, corner_radius=2,
                    fg_color=GOLD_DARK if picked == today else CARD,
                    hover_color=theme.GOLD_FAINT, text_color=TEXT, font=self._norm,
                    border_width=1, border_color=GOLD if picked == today else theme.GOLD_FAINT,
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


class IconEntry(ctk.CTkFrame):
    """Bordered search field with an image icon and a CTkEntry-compatible API."""

    def __init__(
        self,
        master,
        *,
        placeholder: str,
        width: int,
        font: ctk.CTkFont,
        icon: ctk.CTkImage | None,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=48,
            fg_color="#03101c",
            border_color=GOLD_DARK,
            border_width=1,
            corner_radius=3,
        )
        self.pack_propagate(False)
        self.icon_label = ctk.CTkLabel(
            self,
            text="" if icon else "⌕",
            image=icon,
            width=46,
            text_color=theme.TEXT_SOFT,
            fg_color="transparent",
        )
        self.icon_label.pack(side="left", padx=(7, 0), fill="y")
        self.entry = ctk.CTkEntry(
            self,
            placeholder_text=placeholder,
            height=44,
            font=font,
            fg_color="#03101c",
            border_width=0,
            text_color=TEXT,
            placeholder_text_color=MUTED,
            corner_radius=0,
        )
        self.entry.pack(side="left", fill="both", expand=True, padx=(0, 7), pady=2)

    def get(self) -> str:
        return self.entry.get()

    def delete(self, first, last=None) -> None:
        self.entry.delete(first, last)

    def insert(self, index, string) -> None:
        self.entry.insert(index, string)

    def bind(self, sequence=None, command=None, add=None):
        if hasattr(self, "entry"):
            if add is None:
                return self.entry.bind(sequence, command)
            return self.entry.bind(sequence, command, add)
        if add is None:
            return super().bind(sequence, command)
        return super().bind(sequence, command, add)

    def focus_set(self) -> None:
        self.entry.focus_set()


class ModeMenuButton(ctk.CTkButton):
    """Theme-matched game-mode selector that keeps its current value visible."""

    def __init__(self, master, *, values: list[str], font: ctk.CTkFont, command) -> None:
        self._on_select = command
        self._value = tk.StringVar(master=master, value=values[0])
        self._menu = tk.Menu(
            master,
            tearoff=False,
            bg=PANEL,
            fg=TEXT,
            activebackground=NEUTRAL_HOVER,
            activeforeground=GOLD_BRIGHT,
            bd=1,
            relief=tk.SOLID,
            font=(theme.FONT_BODY, 11),
        )
        for value in values:
            self._menu.add_radiobutton(
                label=value,
                value=value,
                variable=self._value,
                command=lambda selected=value: self._select(selected),
            )
        super().__init__(
            master,
            text=self._button_text(values[0]),
            width=138,
            height=48,
            font=font,
            anchor="w",
            command=self._open_menu,
            **theme.button_style("neutral"),
        )

    @staticmethod
    def _button_text(value: str) -> str:
        return f"  {value}   ▾"

    def _open_menu(self) -> None:
        self._menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())

    def _select(self, value: str) -> None:
        self._value.set(value)
        self.configure(text=self._button_text(value))
        self._on_select(value)

    def set(self, value: str) -> None:
        self._value.set(value)
        self.configure(text=self._button_text(value))


class SwitchingLabel(ctk.CTkLabel):
    """A CTk label that reports text changes to its layout controller."""

    def __init__(self, master, *, on_text_change, **kwargs) -> None:
        self._on_text_change = None
        super().__init__(master, **kwargs)
        self._on_text_change = on_text_change

    def configure(self, require_redraw: bool = False, **kwargs) -> None:
        super().configure(require_redraw=require_redraw, **kwargs)
        if self._on_text_change is not None and "text" in kwargs:
            self._on_text_change(str(kwargs["text"]))

    def set_text_silently(self, text: str) -> None:
        callback = self._on_text_change
        self._on_text_change = None
        try:
            super().configure(text=text)
        finally:
            self._on_text_change = callback


class CanvasButtonProxy:
    """Small controller-compatible handle for a button drawn on a virtual list.

    The download controller historically receives a ``CTkButton`` and keeps that
    object while the worker thread runs.  Canvas rows do not own widgets, so this
    proxy preserves the tiny stateful part of that contract without recreating a
    hidden button for every result.
    """

    def __init__(self, surface: VirtualListCanvas, key: object) -> None:
        self.surface = surface
        self.key = key
        self._download_complete = False
        self._download_face_state = "normal"

    def configure(self, **_kwargs) -> None:
        self.surface.refresh()

    def winfo_exists(self) -> bool:
        return bool(
            self.surface.winfo_exists()
            and self.surface.contains_key(self.key)
        )


class VirtualListCanvas(tk.Frame):
    """A smooth, windowed list rendered by one native Tk Canvas.

    Only rows intersecting the viewport are materialised as Canvas items.  A
    fifty-result page therefore costs roughly the same as a ten-result page,
    instead of constructing 35--40 CTk widgets per row.  The class deliberately
    exposes the two private attributes used by the old scrollable-frame helpers
    so page/splitter code can migrate without a second layout rewrite.
    """

    def __init__(
        self,
        master,
        owner: Panel,
        renderer,
        *,
        row_height: int,
        background: str = PANEL,
    ) -> None:
        super().__init__(master, bg=background, bd=0, highlightthickness=0)
        self.owner = owner
        self.renderer = renderer
        self.row_height = max(1, int(row_height))
        self.items: list[object] = []
        self._keys: set[object] = set()
        self._offset = 0.0
        self._target_offset = 0.0
        self._scroll_job: str | None = None
        self._redraw_job: str | None = None
        self._scrollbar_requested = False
        self._dragging_scrollbar = False
        self._drag_thumb_offset = 0.0
        self._hit_regions: list[dict[str, object]] = []
        self._hover_key: object | None = None
        self._hover_leave = None
        self._hover_redraw = False
        self._last_size = (0, 0)
        self.canvas = tk.Canvas(
            self,
            bg=background,
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
            cursor="arrow",
        )
        self.canvas.pack(fill="both", expand=True)
        # Compatibility with the former CTkScrollableFrame call sites.
        self._parent_frame = self
        self._parent_canvas = self
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_button_press)
        self.canvas.bind("<B1-Motion>", self._on_button_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_button_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

    @property
    def hover_key(self) -> object | None:
        return self._hover_key

    def contains_key(self, key: object) -> bool:
        return key in self._keys

    def set_items(self, items, *, renderer=None, row_height: int | None = None) -> None:
        self._clear_hover()
        self.items = list(items)
        self._keys = {
            item.get("_canvas_key", index) if isinstance(item, dict) else index
            for index, item in enumerate(self.items)
        }
        if renderer is not None:
            self.renderer = renderer
        if row_height is not None:
            self.row_height = max(1, int(row_height))
        self._offset = 0.0
        self._target_offset = 0.0
        self.refresh(immediate=True)
        self.owner._schedule_stable_snapshot(260)

    def clear(self) -> None:
        self.set_items([])

    def show_scrollbar(self, visible: bool) -> None:
        self._scrollbar_requested = bool(visible)
        self.refresh()

    def refresh(self, *, immediate: bool = False) -> None:
        if not self.winfo_exists():
            return
        if self._redraw_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._redraw_job)
            self._redraw_job = None
        if immediate:
            self._redraw()
        else:
            self._redraw_job = self.after_idle(self._redraw)

    def _on_configure(self, event: tk.Event) -> None:
        size = (int(event.width), int(event.height))
        if size == self._last_size:
            return
        self._last_size = size
        self._clamp_offsets()
        self.refresh()

    def _max_offset(self) -> float:
        viewport = max(1, self.canvas.winfo_height())
        return float(max(0, len(self.items) * self.row_height - viewport))

    def _clamp_offsets(self) -> None:
        maximum = self._max_offset()
        self._offset = min(maximum, max(0.0, self._offset))
        self._target_offset = min(maximum, max(0.0, self._target_offset))

    def yview_moveto(self, fraction: float) -> None:
        maximum = self._max_offset()
        self._offset = self._target_offset = maximum * min(1.0, max(0.0, float(fraction)))
        self.refresh(immediate=True)

    def yview_scroll(self, number: int, what: str) -> None:
        amount = self.row_height if what == "units" else max(1, self.canvas.winfo_height() - 30)
        self._target_offset += int(number) * amount
        self._clamp_offsets()
        self._start_scroll_animation()

    def _on_mousewheel(self, event: tk.Event) -> str:
        self._clear_hover()
        self.owner._hide_roster_tip()
        steps = event.delta / 120 if event.delta else 0
        if not steps:
            return "break"
        # Accumulate wheel impulses, then ease toward the target at display rate.
        self._target_offset -= steps * max(48, round(self.row_height * 0.82))
        self._clamp_offsets()
        self._start_scroll_animation()
        return "break"

    def _start_scroll_animation(self) -> None:
        if self._scroll_job is None:
            self._scroll_job = self.after(16, self._animate_scroll)

    def _animate_scroll(self) -> None:
        self._scroll_job = None
        delta = self._target_offset - self._offset
        if abs(delta) < 0.75:
            self._offset = self._target_offset
            self.refresh(immediate=True)
            self.owner._schedule_stable_snapshot(220)
            return
        self._offset += delta * 0.38
        self.refresh(immediate=True)
        self._scroll_job = self.after(16, self._animate_scroll)

    def add_hitbox(
        self,
        bounds: tuple[float, float, float, float],
        command=None,
        *,
        cursor: str = "hand2",
        key: object | None = None,
        on_enter=None,
        on_leave=None,
        redraw_on_hover: bool = False,
    ) -> None:
        self._hit_regions.append(
            {
                "bounds": bounds,
                "command": command,
                "cursor": cursor,
                "key": key if key is not None else ("hitbox", *bounds),
                "on_enter": on_enter,
                "on_leave": on_leave,
                "redraw": bool(redraw_on_hover),
            }
        )

    def _hit_at(self, x: float, y: float):
        for region in reversed(self._hit_regions):
            x1, y1, x2, y2 = region["bounds"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return region
        return None

    def _set_hover_region(self, region) -> None:
        key = region["key"] if region is not None else None
        if key == self._hover_key:
            return
        old_leave = self._hover_leave
        redraw = self._hover_redraw or bool(region and region["redraw"])
        self._hover_key = key
        self._hover_leave = region["on_leave"] if region is not None else None
        self._hover_redraw = bool(region and region["redraw"])
        if callable(old_leave):
            old_leave()
        if region is not None and callable(region["on_enter"]):
            region["on_enter"]()
        if redraw:
            self.refresh(immediate=True)

    def _clear_hover(self) -> None:
        self._set_hover_region(None)

    def _on_leave(self, _event: tk.Event) -> None:
        self.canvas.configure(cursor="arrow")
        self._clear_hover()

    def _scrollbar_geometry(self) -> tuple[float, float, float, float] | None:
        height = self.canvas.winfo_height()
        total = len(self.items) * self.row_height
        if not self._scrollbar_requested or total <= height or height <= 1:
            return None
        track_top, track_bottom = 6.0, float(height - 6)
        track_height = max(1.0, track_bottom - track_top)
        thumb_height = max(38.0, track_height * height / total)
        travel = max(1.0, track_height - thumb_height)
        fraction = self._offset / max(1.0, total - height)
        thumb_top = track_top + travel * fraction
        return float(self.canvas.winfo_width() - 13), thumb_top, 8.0, thumb_height

    def _on_motion(self, event: tk.Event) -> None:
        hit = self._hit_at(event.x, event.y)
        self._set_hover_region(hit)
        over_scrollbar = event.x >= self.canvas.winfo_width() - 18 and self._scrollbar_geometry()
        cursor = hit["cursor"] if hit else "hand2" if over_scrollbar else "arrow"
        self.canvas.configure(cursor=cursor)

    def _on_button_press(self, event: tk.Event) -> str | None:
        geometry = self._scrollbar_geometry()
        if geometry and event.x >= self.canvas.winfo_width() - 18:
            _x, thumb_top, _width, thumb_height = geometry
            self._dragging_scrollbar = True
            if thumb_top <= event.y <= thumb_top + thumb_height:
                self._drag_thumb_offset = event.y - thumb_top
            else:
                self._drag_thumb_offset = thumb_height / 2
                self._move_thumb_to(event.y)
            return "break"
        hit = self._hit_at(event.x, event.y)
        if hit is not None and callable(hit["command"]):
            hit["command"]()
            return "break"
        return None

    def _on_button_drag(self, event: tk.Event) -> str | None:
        if not self._dragging_scrollbar:
            return None
        self._move_thumb_to(event.y)
        return "break"

    def _on_button_release(self, _event: tk.Event) -> str | None:
        if not self._dragging_scrollbar:
            return None
        self._dragging_scrollbar = False
        return "break"

    def _move_thumb_to(self, pointer_y: float) -> None:
        geometry = self._scrollbar_geometry()
        if geometry is None:
            return
        _x, _thumb_top, _width, thumb_height = geometry
        track_top = 6.0
        travel = max(1.0, self.canvas.winfo_height() - 12.0 - thumb_height)
        fraction = (pointer_y - self._drag_thumb_offset - track_top) / travel
        self._offset = self._target_offset = self._max_offset() * min(1.0, max(0.0, fraction))
        self.refresh(immediate=True)

    def _draw_background(self, width: int, height: int) -> None:
        photo = self.owner._canvas_photo("theme/surface-panel-tile.png")
        if photo is None:
            return
        tile_w, tile_h = max(1, photo.width()), max(1, photo.height())
        for y in range(0, height, tile_h):
            for x in range(0, width, tile_w):
                self.canvas.create_image(
                    x, y, anchor="nw", image=photo, tags=("list_dynamic", "list_background")
                )

    def _draw_scrollbar(self) -> None:
        geometry = self._scrollbar_geometry()
        if geometry is None:
            return
        x, thumb_top, width, thumb_height = geometry
        self.canvas.create_rectangle(
            x,
            5,
            x + width,
            self.canvas.winfo_height() - 5,
            fill=theme.BG_RAISED,
            outline="",
            tags="list_dynamic",
        )
        self.canvas.create_rectangle(
            x,
            thumb_top,
            x + width,
            thumb_top + thumb_height,
            fill=GOLD_DARK,
            outline=BRONZE,
            width=1,
            tags="list_dynamic",
        )

    @staticmethod
    def visible_window(
        offset: float, row_height: int, viewport_height: int, item_count: int
    ) -> tuple[int, int, float]:
        """Return ``first, stop, y`` for the rows intersecting one viewport."""

        if item_count <= 0 or row_height <= 0 or viewport_height <= 0:
            return 0, 0, 0.0
        offset = max(0.0, float(offset))
        first = min(item_count, int(offset // row_height))
        row_y = first * row_height - offset
        visible = math.ceil((viewport_height + row_height - row_y) / row_height)
        return first, min(item_count, first + max(1, visible)), row_y

    def _redraw(self) -> None:
        self._redraw_job = None
        if not self.winfo_exists():
            return
        canvas = self.canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        self._clamp_offsets()
        canvas.delete("list_dynamic")
        self._hit_regions.clear()
        self._draw_background(width, height)
        first, stop, row_y = self.visible_window(
            self._offset, self.row_height, height, len(self.items)
        )
        # One extra row above and below avoids gaps while eased scrolling moves.
        index = first
        while index < stop:
            self.renderer(self, self.items[index], row_y, self.row_height, width, index)
            row_y += self.row_height
            index += 1
        self._draw_scrollbar()


class Panel(ctk.CTk):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.title("AoE4 Replay Launcher")
        self._custom_chrome = sys.platform == "win32"
        self._window_maximized = False
        self._window_restore_geometry = "1672x941"
        self._window_restore_size = (1672, 941)
        self._window_min_size = (1180, 680)
        self._title_drag_origin: tuple[int, int, int, int] | None = None
        self._native_resize_active = False
        self._window_resize_origin: tuple[str, int, int, int, int, int, int] | None = None
        self._window_resize_pending: tuple[int, int, int, int] | None = None
        self._window_resize_deadzone_anchor: tuple[int, int] | None = None
        self._window_resize_last_pointer: tuple[int, int] | None = None
        self._window_resize_job: str | None = None
        self._window_transition_active = False
        self._window_transition_job: str | None = None
        self._window_rect_generation = 0
        self._resize_snapshot_active = False
        self._resize_snapshot_source: Image.Image | None = None
        self._resize_snapshot_photo: ImageTk.PhotoImage | None = None
        self._resize_snapshot_fill_item: int | None = None
        self._resize_snapshot_item: int | None = None
        self._resize_snapshot_edge = ""
        self._resize_snapshot_finish_job: str | None = None
        self._resize_hold_reflow_job: str | None = None
        self._resize_responsive_pending = False
        self._stable_snapshot_source: Image.Image | None = None
        self._stable_snapshot_photo: ImageTk.PhotoImage | None = None
        self._stable_snapshot_size = (0, 0)
        self._stable_snapshot_job: str | None = None
        self._stable_snapshot_busy = False
        self._native_resize_cursor_widget: tk.Misc | None = None
        self._native_resize_cursor_previous = ""
        self._native_resize_cursor_applied = ""
        self._native_hwnd = 0
        self._chrome_restore_pending = False
        if self._custom_chrome:
            self.overrideredirect(True)
        self._apply_window_icon()
        left, top, right, bottom = self._work_area()
        initial_width = min(1672, right - left)
        initial_height = min(941, bottom - top)
        initial_x = left + max(0, (right - left - initial_width) // 2)
        initial_y = top + max(0, (bottom - top - initial_height) // 2)
        initial_geometry = (
            f"{initial_width}x{initial_height}+{initial_x}+{initial_y}"
        )
        self._window_restore_geometry = initial_geometry
        self.geometry(initial_geometry)
        self.minsize(*self._window_min_size)
        ctk.set_appearance_mode("dark")

        self._searching = False
        self._playing = False
        self._game_running = False  # AoE4 open per the Steam registry -> lock actions
        self._game_watcher = None  # event-driven watcher of the Steam "running" value
        self._downloading = False  # a replay download is running -> block a second
        self._cancel_event: threading.Event | None = None  # set while a download runs
        self._play_buttons: list[ctk.CTkButton] = []
        self._dl_files: list[Path] = []  # all downloaded replays, newest-first (paged)
        self._dl_page = 1  # currently shown page of the downloaded-replays list
        self._dl_buttons: list[object] = []  # live CTk/canvas Download controllers
        self._canvas_download_states: dict[int, str] = {}
        self._info_fetching: set[int] = set()
        self._stamp_cache: dict[tuple, datetime] = {}  # (path, mtime, size) -> replay time
        self._info_cache: dict[int, dict] = self._load_match_info()  # persisted across runs
        # scrolling any list dismisses an open roster tooltip — a prime way it could
        # otherwise be stranded (a canvas scroll doesn't fire <Leave> on the moved name)
        self.bind_all("<MouseWheel>", self._route_match_mousewheel, add="+")
        self._images: dict[tuple[str, tuple[int, int]], ctk.CTkImage] = {}
        self._hover_images: dict[tuple[str, tuple[int, int]], ctk.CTkImage] = {}
        self._texture_photos: dict[str, tk.PhotoImage] = {}
        self._canvas_photos: dict[tuple[str, tuple[int, int] | None, bool], tk.PhotoImage] = {}
        self._canvas_hover_photos: dict[tuple[str, tuple[int, int]], tk.PhotoImage] = {}
        self._canvas_disabled_photos: dict[tuple[str, tuple[int, int]], tk.PhotoImage] = {}
        self._canvas_fonts: dict[tuple[str, int, str], ctk.CTkFont] = {}
        self._country_sources: dict[str, Image.Image] = {}
        self._country_images: dict[str, ctk.CTkImage] = {}
        self._country_failures: set[str] = set()
        self._suggest_token = 0  # bumped per name search; guards the async flag refresh
        self._info_window: ctk.CTkToplevel | None = None
        self._active_tab = "Profile games"
        # profile-games tab paging state
        self._pg_profile: int | None = None
        self._pg_page = 0  # currently displayed page
        self._pg_start_page = 1  # first page of the view (date-jump aware); Prev stops here
        self._pg_total: int | None = None
        # page-navigation cache: page -> (summaries, raw_count). Only the current
        # page's cards ever exist on screen (widgets are the scarce resource; the
        # summary data is cheap), which keeps Windows handle use bounded.
        self._pg_page_cache: dict[int, tuple[list[dict], int]] = {}
        self._pg_searching = False
        self._pg_since: str | None = None  # API server-side lower bound (YYYY-MM-DD)
        self._pg_until: date | None = None  # client-side upper bound
        self._pg_mode: str | None = None  # server-side leaderboard filter (rm_1v1, qm_ffa, …)
        self._h2h_ids: tuple[int, int] | None = None
        self._h2h_page = 0  # currently displayed h2h page
        self._h2h_start_page = 1
        self._h2h_total: int | None = None
        self._h2h_page_cache: dict[int, tuple[list[dict], int]] = {}
        self._h2h_since: str | None = None
        self._h2h_until: date | None = None
        self._h2h_mode: str | None = None  # server-side leaderboard filter
        self._split_initialized = False
        self._split_min_main_height = 380
        self._split_min_download_height = 150
        self._split_ratio = 678 / 941
        self._split_last_height = 0  # previous tick's splitter height (settle check)
        self._split_attempts = 0  # safety cap so a never-settling size still places
        self._grip_drag_offset = 0
        self._grip_target_y = 0
        self._responsive_repaint_job: str | None = None
        self._notification_texts = {"status": "", "notify": ""}
        self._active_notification_channel: str | None = None
        self._notice_canvas_items: dict[int, tuple[ctk.CTkFrame, tk.Canvas, int]] = {}
        self._cancel_visible = False

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
        if self._custom_chrome:
            self.bind("<Map>", self._restore_chrome_after_minimize, add="+")
            self.bind(
                "<Visibility>", self._restore_chrome_after_minimize, add="+"
            )
            # CustomTkinter fills the top-level with nested native child HWNDs.
            # bind_all guarantees the invisible outer resize target is observed
            # whichever child happens to be under the pointer.
            self.bind_all("<Motion>", self._update_native_resize_cursor, add="+")
            self.bind_all(
                "<ButtonPress-1>", self._begin_native_window_resize, add="+"
            )
            self.bind_all("<B1-Motion>", self._drag_native_window_resize, add="+")
            self.bind_all(
                "<ButtonRelease-1>", self._finish_native_window_resize, add="+"
            )

        self._build_ui()
        self.bind_all(
            "<KeyRelease>",
            lambda _event: self._schedule_stable_snapshot(260),
            add="+",
        )
        self.bind_all(
            "<ButtonRelease-1>",
            lambda _event: self._schedule_stable_snapshot(260),
            add="+",
        )
        self._compact_layout: bool | None = None
        self._responsive_signature: tuple[bool, bool, int, int] | None = None
        self.bind("<Configure>", self._on_responsive_layout, add="+")
        if self._custom_chrome:
            self.after(40, self._expose_in_taskbar)
        self.refresh_downloads()
        self._schedule_stable_snapshot(600)
        # learn builds released while the panel was closed (background, best-effort)
        threading.Thread(
            target=lambda: service.sync_build_map(self.cfg), daemon=True
        ).start()
        # check for a newer app release in the background (Velopack auto-update)
        threading.Thread(target=self._check_for_updates, daemon=True).start()
        self._start_game_watch()  # lock Play/Delete while AoE4 is open (registry watch)

    # ---- Steam sign-in ----------------------------------------------------

    def _prompt_steam_login(self, on_success=None) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Connect your Steam account")
        self._apply_icon_to_window(win)
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.transient(self)
        # Center over the main window (like the other modals) instead of the
        # window manager's default top-left placement.
        win.update_idletasks()
        login_width, login_height = 500, 630
        cx = self.winfo_rootx() + (self.winfo_width() - login_width) // 2
        cy = self.winfo_rooty() + (self.winfo_height() - login_height) // 2
        win.geometry(
            f"{login_width}x{login_height}+{max(cx, 0)}+{max(cy, 0)}"
        )
        win.after(200, win.grab_set)
        win.protocol("WM_DELETE_WINDOW", self._close_login)  # the X just closes this dialog
        self._login_win = win
        self._login_on_success = on_success
        self._login_cancel = threading.Event()
        self._awaiting_guard = False
        self._qr_image = None  # keep a reference so the CTkImage isn't garbage-collected

        shell = ctk.CTkFrame(win, **theme.panel_style(strong=True))
        shell.pack(fill="both", expand=True, padx=8, pady=8)
        login_title_image = self._gold_text_image("CONNECT YOUR STEAM ACCOUNT", 18)
        login_title_size = login_title_image.cget("size")
        ctk.CTkLabel(
            shell,
            text="",
            image=login_title_image,
            width=login_title_size[0],
            height=login_title_size[1],
            fg_color="transparent",
        ).pack(pady=(22, 2))
        theme.OrnateRule(shell, bg=PANEL, color=GOLD_DARK, height=12).pack(
            fill="x", padx=24
        )
        ctk.CTkLabel(
            shell,
            text="Replays only play on the exact build they were recorded on, so the\n"
                 "tool downloads that build from Steam using your own account.",
            text_color=MUTED, font=self.font_meta, justify="center",
        ).pack(pady=(0, 10))

        # Swappable area: the QR view or the username/password view live here.
        self._login_body = ctk.CTkFrame(shell, fg_color="transparent")
        self._login_body.pack(fill="both", expand=True, padx=20)

        self._login_status = ctk.CTkLabel(
            shell, text="", text_color=MUTED, font=self.font_meta,
            justify="center", wraplength=400,
        )
        self._login_status.pack(pady=(6, 6))

        ctk.CTkButton(
            shell, text="Why is this needed?", width=170, height=34, font=self.font_meta,
            command=self._show_steam_privacy,
            **theme.button_style("neutral"),
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
            width=260, height=38, font=self.font_bold,
            command=self._login_show_password,
            **theme.button_style("neutral"),
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
            font=self.font_bold, command=self._login_show_qr,
            **theme.button_style("primary"),
        ).pack(pady=6)
        ctk.CTkButton(
            self._login_body, text="Use username & password instead", width=240, height=38,
            font=self.font_bold, command=self._login_show_password,
            **theme.button_style("neutral"),
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
            font=self.font_normal, **theme.entry_style(),
        )
        self._login_user.pack(pady=6)
        self._login_pass = ctk.CTkEntry(
            self._login_body, placeholder_text="Password", show="*", width=300, height=42,
            font=self.font_normal, **theme.entry_style(),
        )
        self._login_pass.pack(pady=6)
        # Steam Guard code field — created hidden, revealed only when Steam asks.
        self._login_guard = ctk.CTkEntry(
            self._login_body, placeholder_text="Code", width=300, height=42,
            font=self.font_normal, **theme.entry_style(),
        )

        self._login_btnrow = ctk.CTkFrame(self._login_body, fg_color="transparent")
        self._login_btnrow.pack(pady=(12, 0))
        ctk.CTkButton(
            self._login_btnrow, text="Back to QR", width=145, height=42, font=self.font_bold,
            command=self._login_show_qr,
            **theme.button_style("neutral"),
        ).pack(side="left", padx=6)
        self._login_btn = ctk.CTkButton(
            self._login_btnrow, text="Connect", width=145, height=42, font=self.font_bold,
            command=self._connect_steam,
            **theme.button_style("primary"),
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

    # ---- custom window chrome --------------------------------------------

    def _native_window_handle(self) -> int:
        """Return Tk's real top-level HWND instead of its child drawing HWND."""

        if self._native_hwnd:
            with contextlib.suppress(Exception):
                if ctypes.windll.user32.IsWindow(ctypes.c_void_p(self._native_hwnd)):
                    return self._native_hwnd
        self.update_idletasks()
        child = int(self.winfo_id())
        if not self._custom_chrome:
            self._native_hwnd = child
            return self._native_hwnd
        parent = int(ctypes.windll.user32.GetParent(child))
        self._native_hwnd = parent or child
        return self._native_hwnd

    def _expose_in_taskbar(self) -> None:
        """Expose the custom window normally and enable Windows-native sizing."""

        if not self._custom_chrome or not self.winfo_exists():
            return
        with contextlib.suppress(Exception):
            hwnd = self._native_window_handle()
            user32 = ctypes.windll.user32
            # Keep the captionless AoE frame free of the native sizing border.
            # WS_THICKFRAME is enabled only for the duration of an actual edge
            # drag and removed as soon as Windows exits its sizing loop.
            style = int(user32.GetWindowLongW(hwnd, -16))
            user32.SetWindowLongW(hwnd, -16, style & ~0x00040000)  # WS_THICKFRAME
            # Windows 11 otherwise paints its own light activation border around
            # WS_THICKFRAME. Suppress that second frame and keep square corners;
            # the AoE artwork remains the only visible window outline.
            with contextlib.suppress(Exception):
                dwmapi = ctypes.windll.dwmapi
                no_rounding = ctypes.c_int(1)  # DWMWCP_DONOTROUND
                dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    33,  # DWMWA_WINDOW_CORNER_PREFERENCE
                    ctypes.byref(no_rounding),
                    ctypes.sizeof(no_rounding),
                )
                no_border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE
                dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    34,  # DWMWA_BORDER_COLOR
                    ctypes.byref(no_border),
                    ctypes.sizeof(no_border),
                )
            ex_style = int(user32.GetWindowLongW(hwnd, -20))
            # A frameless Tk window can acquire WS_EX_TOPMOST while its native
            # styles are being rebuilt.  Clear it explicitly so fullscreen and
            # foreground applications are allowed to cover the launcher.
            ex_style = (ex_style | 0x00040000) & ~(0x00000080 | 0x00000008)
            user32.SetWindowLongW(hwnd, -20, ex_style)
            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            )
            set_window_pos.restype = ctypes.c_int
            set_window_pos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(-2),  # HWND_NOTOPMOST
                0,
                0,
                0,
                0,
                0x0033,  # NOSIZE | NOMOVE | NOACTIVATE | FRAMECHANGED
            )

    def _bind_titlebar_drag(self, *widgets: tk.Misc) -> None:
        for widget in widgets:
            widget.bind("<ButtonPress-1>", self._start_title_drag, add="+")
            widget.bind("<B1-Motion>", self._drag_titlebar, add="+")
            widget.bind("<Double-Button-1>", lambda _event: self._toggle_maximize_window(), add="+")

    def _header_art_press(self, event: tk.Event) -> str | None:
        """Route clicks on chrome painted directly into the panorama asset."""

        if event.y <= 65:
            if 1362 <= event.x < 1404:
                self._show_info()
                return "break"
            if 1437 <= event.x < 1483:
                self._minimize_window()
                return "break"
            if 1483 <= event.x < 1538:
                self._toggle_maximize_window()
                return "break"
            if event.x >= 1538:
                self._on_close()
                return "break"
        self._start_title_drag(event)
        return None

    def _header_art_double_click(self, event: tk.Event) -> str | None:
        if event.y <= 65 and (
            1362 <= event.x < 1404 or event.x >= 1437
        ):
            return "break"
        self._toggle_maximize_window()
        return "break"

    def _start_title_drag(self, event: tk.Event) -> None:
        if self._native_resize_active:
            return
        if self._window_maximized:
            if not self._begin_window_transition():
                return
            ratio = (event.x_root - self.winfo_rootx()) / max(1, self.winfo_width())
            self._restore_window(stabilize=False)
            self.update_idletasks()
            x = int(event.x_root - self.winfo_width() * ratio)
            y = max(0, event.y_root - 24)
            self.geometry(f"+{x}+{y}")
        self._title_drag_origin = (
            event.x_root,
            event.y_root,
            self.winfo_x(),
            self.winfo_y(),
        )

    def _drag_titlebar(self, event: tk.Event) -> None:
        if self._title_drag_origin is None:
            return
        sx, sy, wx, wy = self._title_drag_origin
        self.geometry(f"+{wx + event.x_root - sx}+{wy + event.y_root - sy}")

    def _minimize_window(self) -> None:
        if not self._custom_chrome:
            self.iconify()
            return
        self._cancel_window_resize()
        self._begin_resize_snapshot()
        try:
            hwnd = self._native_window_handle()
            user32 = ctypes.windll.user32
            user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
            user32.ShowWindow.restype = ctypes.c_int
            self._chrome_restore_pending = True
            user32.ShowWindow(ctypes.c_void_p(hwnd), 6)  # SW_MINIMIZE
        except Exception:
            self._chrome_restore_pending = False
            self.iconify()

    def _restore_chrome_after_minimize(self, _event: tk.Event | None = None) -> None:
        if not self._custom_chrome or not self._chrome_restore_pending:
            return
        with contextlib.suppress(Exception):
            if ctypes.windll.user32.IsIconic(
                ctypes.c_void_p(self._native_window_handle())
            ):
                return
        if self.state() == "iconic":
            return
        self._chrome_restore_pending = False
        # The HWND and every Tk child remained mapped while minimized.  A small,
        # asynchronous invalidation is enough; rebuilding override-redirect
        # chrome here remapped the full widget tree and caused the visible stall.
        if self._resize_snapshot_active:
            self.after(24, self._finish_resize_snapshot)
        self._schedule_window_repaint(delay=16)

    def _work_area(self) -> tuple[int, int, int, int]:
        if self._custom_chrome:
            class Rect(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            class MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_ulong),
                    ("rcMonitor", Rect),
                    ("rcWork", Rect),
                    ("dwFlags", ctypes.c_ulong),
                ]

            # Maximise against the monitor that actually contains this window.
            # SystemParametersInfoW returns only the primary work area, which made
            # a secondary-monitor window jump, overflow, or stop short at random.
            with contextlib.suppress(Exception):
                user32 = ctypes.windll.user32
                user32.MonitorFromWindow.argtypes = (ctypes.c_void_p, ctypes.c_uint)
                user32.MonitorFromWindow.restype = ctypes.c_void_p
                user32.GetMonitorInfoW.argtypes = (
                    ctypes.c_void_p,
                    ctypes.POINTER(MonitorInfo),
                )
                user32.GetMonitorInfoW.restype = ctypes.c_int
                monitor = user32.MonitorFromWindow(
                    ctypes.c_void_p(self._native_window_handle()), 2
                )  # MONITOR_DEFAULTTONEAREST
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(MonitorInfo)
                if monitor and user32.GetMonitorInfoW(
                    ctypes.c_void_p(monitor), ctypes.byref(info)
                ):
                    work = info.rcWork
                    return work.left, work.top, work.right, work.bottom

            rect = Rect()
            with contextlib.suppress(Exception):
                if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                    return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _set_window_rect(self, width: int, height: int, x: int, y: int) -> None:
        """Apply size and position atomically to Tk and the native HWND."""

        spec = self._window_geometry_spec(width, height, x, y)
        self.geometry(spec)
        if self._custom_chrome:
            with contextlib.suppress(Exception):
                user32 = ctypes.windll.user32
                user32.SetWindowPos(
                    ctypes.c_void_p(self._native_window_handle()),
                    None,
                    int(x),
                    int(y),
                    int(width),
                    int(height),
                    0x0014,  # SWP_NOZORDER | SWP_NOACTIVATE
                )

    @staticmethod
    def _centered_window_rect(
        width: int,
        height: int,
        work_area: tuple[int, int, int, int],
        min_size: tuple[int, int],
    ) -> tuple[int, int, int, int]:
        """Clamp a restored window to one monitor and centre it there."""

        left, top, right, bottom = work_area
        available_width = max(1, right - left)
        available_height = max(1, bottom - top)
        min_width, min_height = min_size
        width = min(available_width, max(min_width, int(width)))
        height = min(available_height, max(min_height, int(height)))
        x = left + (available_width - width) // 2
        y = top + (available_height - height) // 2
        return width, height, x, y

    def _begin_window_transition(self) -> bool:
        """Serialise custom-chrome maximize/restore requests.

        Windows emits both press and double-click messages during a rapid title
        interaction.  Without this guard a maximize can immediately be undone by
        the second message while Tk is still applying the first geometry.
        """

        if self._window_transition_active:
            return False
        self._window_transition_active = True
        if self._window_transition_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._window_transition_job)
        # Screenshot capture can fail on a protected/transitioning desktop.  This
        # fail-safe prevents that exceptional path from leaving the lock engaged.
        self._window_transition_job = self.after(500, self._end_window_transition)
        return True

    def _end_window_transition(self) -> None:
        job = self._window_transition_job
        self._window_transition_job = None
        self._window_transition_active = False
        if job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(job)

    def _set_window_rect_stably(
        self, width: int, height: int, x: int, y: int
    ) -> None:
        """Apply one native window rectangle and verify it after Tk's idle pass."""

        target = (int(width), int(height), int(x), int(y))
        self._window_rect_generation += 1
        generation = self._window_rect_generation
        self._set_window_rect(*target)

        def reassert() -> None:
            if generation != self._window_rect_generation or not self.winfo_exists():
                return
            actual = (
                self.winfo_width(),
                self.winfo_height(),
                self.winfo_x(),
                self.winfo_y(),
            )
            if actual != target:
                self._set_window_rect(*target)

        self.after_idle(reassert)
        self.after(72, reassert)

    def _toggle_maximize_window(self) -> None:
        if not self._begin_window_transition():
            return
        if self._window_maximized:
            self._restore_window()
            return
        self._begin_resize_snapshot()
        self.update_idletasks()
        self._window_restore_size = (self.winfo_width(), self.winfo_height())
        self._window_restore_geometry = self.geometry()
        left, top, right, bottom = self._work_area()
        self._window_maximized = True
        self._set_window_rect_stably(right - left, bottom - top, left, top)
        self.after(32, self._finish_resize_snapshot)
        self._schedule_window_repaint()
        with contextlib.suppress(Exception):
            self._draw_header_controls()

    def _restore_window(self, *, stabilize: bool = True) -> None:
        self._begin_resize_snapshot()
        self._window_maximized = False
        width, height, x, y = self._centered_window_rect(
            *self._window_restore_size,
            self._work_area(),
            self._window_min_size,
        )
        if stabilize:
            self._set_window_rect_stably(width, height, x, y)
        else:
            # In a drag-from-maximized gesture the caller immediately moves the
            # restored window under the pointer, so no delayed centred retry.
            self._window_rect_generation += 1
            self._set_window_rect(width, height, x, y)
        self._window_restore_geometry = self._window_geometry_spec(width, height, x, y)
        self.after(32, self._finish_resize_snapshot)
        self._schedule_window_repaint()
        with contextlib.suppress(Exception):
            self._draw_header_controls()

    def _edge_at(self, x_root: int, y_root: int) -> str:
        if not self._custom_chrome or self._window_maximized:
            return ""
        x = x_root - self.winfo_rootx()
        y = y_root - self.winfo_rooty()
        width = self.winfo_width()
        height = self.winfo_height()
        margin = 14
        return "se" if x >= width - margin and y >= height - margin else ""

    def _update_native_resize_cursor(self, event: tk.Event) -> None:
        if self._native_resize_active:
            return
        edge = self._edge_at(event.x_root, event.y_root)
        if not edge:
            self._clear_native_resize_cursor()
            return
        cursor_names = {
            "n": "size_ns",
            "s": "size_ns",
            "e": "size_we",
            "w": "size_we",
            "nw": "size_nw_se",
            "se": "size_nw_se",
            "ne": "size_ne_sw",
            "sw": "size_ne_sw",
        }
        cursor_ids = {
            "n": 32645,
            "s": 32645,  # IDC_SIZENS
            "e": 32644,
            "w": 32644,  # IDC_SIZEWE
            "nw": 32642,
            "se": 32642,  # IDC_SIZENWSE
            "ne": 32643,
            "sw": 32643,  # IDC_SIZENESW
        }
        cursor_name = cursor_names[edge]
        widget = event.widget
        if widget is not self._native_resize_cursor_widget:
            self._clear_native_resize_cursor()
            with contextlib.suppress(Exception):
                self._native_resize_cursor_previous = str(widget.cget("cursor"))
                widget.configure(cursor=cursor_name)
                self._native_resize_cursor_widget = widget
                self._native_resize_cursor_applied = cursor_name
        elif cursor_name != self._native_resize_cursor_applied:
            with contextlib.suppress(Exception):
                widget.configure(cursor=cursor_name)
                self._native_resize_cursor_applied = cursor_name
        # Tk applies a configured cursor at the next WM_SETCURSOR dispatch.
        # Set it once immediately too, then let the widget option keep it stable.
        with contextlib.suppress(Exception):
            user32 = ctypes.windll.user32
            user32.LoadCursorW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
            user32.LoadCursorW.restype = ctypes.c_void_p
            user32.SetCursor.argtypes = (ctypes.c_void_p,)
            user32.SetCursor.restype = ctypes.c_void_p
            cursor = user32.LoadCursorW(
                ctypes.c_void_p(), ctypes.c_void_p(cursor_ids[edge])
            )
            if cursor:
                user32.SetCursor(cursor)

    def _clear_native_resize_cursor(self) -> None:
        """Restore the edge widget without overwriting another cursor owner."""

        widget = self._native_resize_cursor_widget
        previous = self._native_resize_cursor_previous
        applied = self._native_resize_cursor_applied
        self._native_resize_cursor_widget = None
        self._native_resize_cursor_previous = ""
        self._native_resize_cursor_applied = ""
        if widget is None:
            return
        with contextlib.suppress(Exception):
            if widget.winfo_exists() and str(widget.cget("cursor")) == applied:
                widget.configure(cursor=previous)

    @staticmethod
    def _resized_window_geometry(
        edge: str,
        start_x: int,
        start_y: int,
        start_width: int,
        start_height: int,
        delta_x: int,
        delta_y: int,
        min_width: int,
        min_height: int,
    ) -> tuple[int, int, int, int]:
        """Return ``width, height, x, y`` for a borderless edge drag."""

        width = start_width
        height = start_height
        x = start_x
        y = start_y
        if "w" in edge:
            width = max(min_width, start_width - delta_x)
            x = start_x + start_width - width
        elif "e" in edge:
            width = max(min_width, start_width + delta_x)
        if "n" in edge:
            height = max(min_height, start_height - delta_y)
            y = start_y + start_height - height
        elif "s" in edge:
            height = max(min_height, start_height + delta_y)
        return width, height, x, y

    @staticmethod
    def _window_geometry_spec(width: int, height: int, x: int, y: int) -> str:
        return f"{width}x{height}{x:+d}{y:+d}"

    def _begin_native_window_resize(self, event: tk.Event) -> str | None:
        """Start a frame-limited borderless resize without blocking Tk's loop."""

        if self._native_resize_active:
            return "break"
        edge = self._edge_at(event.x_root, event.y_root)
        if not edge:
            return None
        self._native_resize_active = True
        self._title_drag_origin = None
        self._window_resize_pending = None
        self._cancel_resize_hold_reflow()
        self._window_resize_deadzone_anchor = (
            int(event.x_root),
            int(event.y_root),
        )
        self._window_resize_last_pointer = self._window_resize_deadzone_anchor
        self._window_resize_origin = (
            edge,
            int(event.x_root),
            int(event.y_root),
            self.winfo_x(),
            self.winfo_y(),
            self.winfo_width(),
            self.winfo_height(),
        )
        return "break"

    def _drag_native_window_resize(self, event: tk.Event) -> str | None:
        if not self._native_resize_active or self._window_resize_origin is None:
            return None
        edge, pointer_x, pointer_y, x, y, width, height = self._window_resize_origin
        current_pointer = (int(event.x_root), int(event.y_root))
        deadzone_anchor = self._window_resize_deadzone_anchor
        if deadzone_anchor is not None:
            crossed = self._pointer_crossed_drag_threshold(
                deadzone_anchor,
                current_pointer,
            )
            if not crossed:
                return "break"
            if self._resize_snapshot_item is not None and not self._resize_snapshot_active:
                # The previous stable frame is still being uncovered. Apply the
                # larger movement underneath it, then require a fresh threshold
                # before another freeze so pointer jitter cannot flash the UI.
                self._window_resize_deadzone_anchor = current_pointer
            else:
                self._window_resize_deadzone_anchor = None
                if not self._resize_snapshot_active:
                    self._begin_resize_snapshot(edge=edge)
        elif not self._resize_snapshot_active and self._resize_snapshot_item is None:
            self._begin_resize_snapshot(edge=edge)
        self._window_resize_last_pointer = current_pointer
        min_width, min_height = self._window_min_size
        self._window_resize_pending = self._resized_window_geometry(
            edge,
            x,
            y,
            width,
            height,
            int(event.x_root) - pointer_x,
            int(event.y_root) - pointer_y,
            min_width,
            min_height,
        )
        if self._window_resize_job is None:
            # One geometry mutation per display frame keeps Tk's layout queue
            # bounded even when Windows delivers hundreds of mouse events.
            self._window_resize_job = self.after(
                16, self._apply_pending_window_resize
            )
        self._cancel_resize_hold_reflow()
        # Do not keep the frozen frame until button release. Once the pointer
        # pauses briefly, reflow the real interface at the current size even
        # while the mouse button remains held. A resumed drag freezes a fresh
        # stable frame on the next motion event.
        self._resize_hold_reflow_job = self.after(
            140, self._settle_native_window_resize_while_held
        )
        return "break"

    @staticmethod
    def _pointer_crossed_drag_threshold(
        anchor: tuple[int, int],
        current: tuple[int, int],
    ) -> bool:
        """Return whether pointer travel is large enough to be intentional."""

        return max(
            abs(current[0] - anchor[0]),
            abs(current[1] - anchor[1]),
        ) >= WINDOW_RESIZE_DRAG_THRESHOLD

    def _cancel_resize_hold_reflow(self) -> None:
        if self._resize_hold_reflow_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._resize_hold_reflow_job)
        self._resize_hold_reflow_job = None

    def _settle_native_window_resize_while_held(self) -> None:
        """Reveal a fully reflowed interface after a short held-pointer pause."""

        self._resize_hold_reflow_job = None
        if not self._native_resize_active or not self.winfo_exists():
            return
        if self._window_resize_last_pointer is not None:
            self._window_resize_deadzone_anchor = self._window_resize_last_pointer
        if self._window_resize_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._window_resize_job)
            self._window_resize_job = None
        self._apply_pending_window_resize()
        if self._resize_snapshot_active:
            self._finish_resize_snapshot()

    def _apply_pending_window_resize(self) -> None:
        self._window_resize_job = None
        geometry = self._window_resize_pending
        self._window_resize_pending = None
        if geometry is None or not self.winfo_exists():
            return
        width, height, x, y = geometry
        spec = self._window_geometry_spec(width, height, x, y)
        self._set_window_rect(width, height, x, y)
        if not self._window_maximized:
            self._window_restore_geometry = spec
            self._window_restore_size = (width, height)

    def _finish_native_window_resize(self, _event: tk.Event | None = None) -> str | None:
        if not self._native_resize_active:
            return None
        self._cancel_resize_hold_reflow()
        if self._window_resize_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._window_resize_job)
            self._window_resize_job = None
        self._apply_pending_window_resize()
        self._native_resize_active = False
        self._window_resize_origin = None
        self._window_resize_deadzone_anchor = None
        self._window_resize_last_pointer = None
        self._clear_native_resize_cursor()
        if self._resize_snapshot_active:
            self._resize_snapshot_finish_job = self.after(
                24, self._finish_resize_snapshot
            )
        self._schedule_window_repaint(delay=24)
        return "break"

    def _cancel_window_resize(self) -> None:
        self._cancel_resize_hold_reflow()
        if self._window_resize_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._window_resize_job)
        self._window_resize_job = None
        self._window_resize_pending = None
        self._window_resize_origin = None
        self._window_resize_deadzone_anchor = None
        self._window_resize_last_pointer = None
        self._native_resize_active = False
        self._clear_native_resize_cursor()

    @staticmethod
    def _resize_snapshot_origin(
        edge: str,
        target_width: int,
        target_height: int,
        source_width: int,
        source_height: int,
    ) -> tuple[int, int]:
        """Anchor a frozen frame to the edges that did not move during resize."""

        x = target_width - source_width if "w" in edge else 0
        y = target_height - source_height if "n" in edge else 0
        return x, y

    def _begin_resize_snapshot(self, *, edge: str = "") -> None:
        """Freeze one composited frame while Windows changes the outer geometry.

        Reflowing the remaining CTk toolbar/header tree on every pointer message is
        still substantially slower than drawing the virtual lists.  Keeping the
        last complete frame on the root Canvas makes live resize immediate; the
        real controls are laid out underneath it when the pointer pauses or the
        gesture ends.
        """

        if self._resize_snapshot_active or not hasattr(self, "reference_canvas"):
            return
        if self._resize_snapshot_finish_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._resize_snapshot_finish_job)
            self._resize_snapshot_finish_job = None
        try:
            x, y = self.winfo_rootx(), self.winfo_rooty()
            width, height = self.winfo_width(), self.winfo_height()
            if width <= 1 or height <= 1:
                return
            cached = (
                self._stable_snapshot_source is not None
                and self._stable_snapshot_size == (width, height)
            )
            source = (
                self._stable_snapshot_source
                if cached
                else ImageGrab.grab(
                    bbox=(x, y, x + width, y + height), all_screens=True
                ).convert("RGB")
            )
        except (OSError, ValueError, tk.TclError):
            return
        try:
            photo = (
                self._stable_snapshot_photo
                if cached and self._stable_snapshot_photo is not None
                else ImageTk.PhotoImage(source, master=self.reference_canvas)
            )
        except (ValueError, tk.TclError):
            return
        self._resize_snapshot_source = source
        self._resize_snapshot_photo = photo
        self._resize_snapshot_edge = edge
        # A persistent Canvas image survives normal Tk expose/repaint events. It
        # is deliberately not scaled: newly exposed space shows this solid AoE
        # night navy until the real controls reflow once at pointer release.
        self._resize_snapshot_fill_item = self.reference_canvas.create_rectangle(
            0,
            0,
            width,
            height,
            fill=BG,
            outline="",
            tags="resize_snapshot",
        )
        self._resize_snapshot_item = self.reference_canvas.create_image(
            0,
            0,
            anchor="nw",
            image=photo,
            tags="resize_snapshot",
        )
        self.reference_canvas.itemconfigure(self._main_canvas_window, state="hidden")
        self.reference_canvas.itemconfigure(
            self._download_canvas_window, state="hidden"
        )
        self._resize_snapshot_active = True
        if self._resize_snapshot_item is not None:
            self.reference_canvas.tag_raise("resize_snapshot")
        self._resize_snapshot_to(width, height)

    def _resize_snapshot_to(self, width: int, height: int) -> None:
        source = self._resize_snapshot_source
        item = self._resize_snapshot_item
        if not self._resize_snapshot_active or source is None:
            return
        width, height = max(1, int(width)), max(1, int(height))
        if item is None:
            return
        try:
            source_width, source_height = source.size
            x, y = self._resize_snapshot_origin(
                self._resize_snapshot_edge,
                width,
                height,
                source_width,
                source_height,
            )
            fill_item = self._resize_snapshot_fill_item
            if fill_item is not None:
                self.reference_canvas.coords(fill_item, 0, 0, width, height)
                self.reference_canvas.tag_raise(fill_item)
            self.reference_canvas.coords(item, x, y)
            self.reference_canvas.tag_raise(item)
        except tk.TclError:
            return

    def _finish_resize_snapshot(self) -> None:
        """Reflow live controls once, then uncover them after their idle paint."""

        self._resize_snapshot_finish_job = None
        if not self._resize_snapshot_active:
            return
        self._resize_snapshot_to(
            self.reference_canvas.winfo_width(),
            self.reference_canvas.winfo_height(),
        )
        self._resize_snapshot_active = False
        self._reference_layout_signature = None
        if self._resize_responsive_pending:
            self._resize_responsive_pending = False
            event = tk.Event()
            event.widget = self
            event.width = self.winfo_width()
            event.height = self.winfo_height()
            self._on_responsive_layout(event)
        self._layout_reference_canvas(
            self.reference_canvas.winfo_width(),
            self.reference_canvas.winfo_height(),
        )
        # The snapshot remains above the live windows while their pending CTk
        # dimensions settle, eliminating the partially repainted intermediate.
        self.after(80, self._reveal_resize_snapshot)

    def _reveal_resize_snapshot(self) -> None:
        with contextlib.suppress(tk.TclError):
            self.reference_canvas.itemconfigure(
                self._main_canvas_window, state="normal"
            )
            self.reference_canvas.itemconfigure(
                self._download_canvas_window, state="normal"
            )
        self._drop_resize_snapshot()
        self._end_window_transition()

    def _drop_resize_snapshot(self) -> None:
        if hasattr(self, "reference_canvas"):
            with contextlib.suppress(tk.TclError):
                self.reference_canvas.delete("resize_snapshot")
        self._resize_snapshot_fill_item = None
        self._resize_snapshot_item = None
        self._resize_snapshot_photo = None
        self._resize_snapshot_source = None
        self._resize_snapshot_edge = ""
        self._schedule_stable_snapshot(180)

    def _schedule_stable_snapshot(self, delay: int = 250) -> None:
        """Refresh the next resize frame after the live UI has become idle."""

        if not self.winfo_exists() or self._resize_snapshot_active:
            return
        if self._stable_snapshot_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._stable_snapshot_job)
        self._stable_snapshot_job = self.after(
            max(1, int(delay)), self._capture_stable_snapshot
        )

    def _capture_stable_snapshot(self) -> None:
        self._stable_snapshot_job = None
        if self._stable_snapshot_busy or self._resize_snapshot_active:
            return
        with contextlib.suppress(Exception):
            if os.name == "nt":
                user32 = ctypes.windll.user32
                if user32.IsIconic(ctypes.c_void_p(self._native_window_handle())):
                    return
        x, y = self.winfo_rootx(), self.winfo_rooty()
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        geometry = (x, y, width, height)
        hwnd = self._native_window_handle() if os.name == "nt" else 0
        self._stable_snapshot_busy = True

        def worker() -> None:
            source = None
            with contextlib.suppress(OSError, ValueError):
                source = (
                    ImageGrab.grab(window=hwnd)
                    if hwnd
                    else ImageGrab.grab(
                        bbox=(x, y, x + width, y + height), all_screens=True
                    )
                ).convert("RGB")
            with contextlib.suppress(Exception):
                self.after(0, lambda: finish(source, geometry))

        def finish(source: Image.Image | None, captured_geometry) -> None:
            self._stable_snapshot_busy = False
            current = (
                self.winfo_rootx(),
                self.winfo_rooty(),
                self.winfo_width(),
                self.winfo_height(),
            )
            if (
                source is not None
                and captured_geometry == current
                and source.size == current[2:]
                and not self._resize_snapshot_active
            ):
                self._stable_snapshot_source = source
                self._stable_snapshot_size = current[2:]
                self._stable_snapshot_photo = None
                with contextlib.suppress(ValueError, tk.TclError):
                    self._stable_snapshot_photo = ImageTk.PhotoImage(
                        source, master=self.reference_canvas
                    )

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_window_repaint(self, delay: int = 16) -> None:
        """Coalesce a lightweight asynchronous window invalidation."""

        if self._responsive_repaint_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._responsive_repaint_job)
        self._responsive_repaint_job = self.after(
            max(1, delay), self._force_window_repaint
        )

    def _on_responsive_layout(self, event: tk.Event) -> None:
        """Keep both filter toolbars usable below the 1672 px reference width."""

        if event.widget is not self:
            return
        if self._resize_snapshot_active:
            self._resize_responsive_pending = True
            return
        compact = event.width < 1600
        compact_nav = event.width < 1800
        if compact:
            profile_control_width, h2h_entry_width = 160, 220
        elif event.width >= 3000:
            profile_control_width, h2h_entry_width = 280, 340
        elif event.width >= 2200:
            profile_control_width, h2h_entry_width = 280, 300
        else:
            profile_control_width, h2h_entry_width = 240, 265
        signature = (compact, compact_nav, profile_control_width, h2h_entry_width)
        previous_signature = self._responsive_signature
        if signature == previous_signature:
            return
        initial_layout = previous_signature is None
        density_changed = initial_layout or self._compact_layout != compact
        nav_changed = initial_layout or previous_signature[:2] != signature[:2]
        self._responsive_signature = signature
        self._compact_layout = compact
        sizes = {
            "profile_control": profile_control_width,
            # Date Filter has the widest default content. A single compact width
            # keeps all four actions equal and releases the unused toolbar space.
            "profile_action": 138,
            "h2h_entry": h2h_entry_width,
        }
        profile_width = sizes["profile_control"]
        action_width = sizes["profile_action"]
        if initial_layout or previous_signature[2] != profile_control_width:
            self.p_entry.configure(width=profile_width)
        if initial_layout or previous_signature[3] != h2h_entry_width:
            self.entry1.configure(width=sizes["h2h_entry"])
            self.entry2.configure(width=sizes["h2h_entry"])
        if initial_layout:
            self.p_filter_btn.configure(width=action_width)
            self.h_filter_btn.configure(width=action_width)
            self.p_filter_mode.configure(width=action_width)
            self.h_filter_mode.configure(width=action_width)
            search_size = (action_width - 10, 40)
            clear_size = (action_width - 10, 40)
            self.p_search_btn.configure(width=action_width)
            self.search_btn.configure(width=action_width)
            self.p_filter_clear.configure(width=action_width)
            self.h_filter_clear.configure(width=action_width)
            self._set_full_face_button_asset(
                self.p_search_btn, "theme/button-search-face.png", search_size
            )
            self._set_full_face_button_asset(
                self.search_btn, "theme/button-search-face.png", search_size
            )
            self._set_full_face_button_asset(
                self.p_filter_clear, "theme/button-clear-face.png", clear_size
            )
            self._set_full_face_button_asset(
                self.h_filter_clear, "theme/button-clear-face.png", clear_size
            )
        # The match counter and every inline notification share one toolbar dock.
        # Keep the counter portion stable while the message portion absorbs the
        # remaining width.
        if density_changed:
            self.notification_count_slot.configure(width=200 if compact else 240)
            self.p_status.configure(
                width=188 if compact else 228,
                wraplength=178 if compact else 218,
            )
            self._place_notification_bar(compact=compact)
        if compact:
            if nav_changed:
                self.header_nav.configure(width=600)
                self.header_nav.place_configure(relx=0.5, x=0)
                self._draw_header_navigation()
                self._align_header_nav_background(event.width)
            if density_changed:
                self.reference_brand_label.place_forget()
                self._set_scrollbar_visible(
                    self.games, len(self._surface_children(self.games)) > 5
                )
                self._set_scrollbar_visible(
                    self.matches, len(self._surface_children(self.matches)) > 5
                )
                self._set_scrollbar_visible(
                    self.downloads, len(self._surface_children(self.downloads)) > 2
                )
                self._set_scrollbar_visible(
                    self.saved_list, getattr(self, "_saved_build_count", 0) > 4
                )
                compact_brand = self._gold_text_image("AoE IV", 18)
                compact_brand_size = compact_brand.cget("size")
                self.brand_title_label.configure(
                    image=compact_brand,
                    width=compact_brand_size[0],
                    height=compact_brand_size[1],
                )
                self.brand_title_label.place_configure(x=112, y=27)
                compact_subtitle = self._gold_text_image("REPLAY LAUNCHER", 10)
                compact_subtitle_size = compact_subtitle.cget("size")
                self.brand_subtitle_label.configure(
                    text="",
                    image=compact_subtitle,
                    width=compact_subtitle_size[0],
                    height=compact_subtitle_size[1],
                )
                self.brand_subtitle_label.place_configure(x=112, y=62)
        else:
            if nav_changed:
                if compact_nav:
                    self.header_nav.configure(width=600)
                    self.header_nav.place_configure(relx=0.5, x=0)
                else:
                    self.header_nav.configure(width=760)
                    self.header_nav.place_configure(relx=0.5, x=-44)
                self._draw_header_navigation()
                self._align_header_nav_background(event.width)
            if density_changed:
                self.reference_brand_label.place(x=0, y=-5)
                self.reference_brand_label.lift()
                # Keep short lists visually clean, but expose a real scrollbar
                # as soon as additional rows require scrolling.
                self._set_scrollbar_visible(
                    self.games, len(self._surface_children(self.games)) > 5
                )
                self._set_scrollbar_visible(
                    self.matches, len(self._surface_children(self.matches)) > 5
                )
                self._set_scrollbar_visible(
                    self.downloads, len(self._surface_children(self.downloads)) > 2
                )
                self._set_scrollbar_visible(
                    self.saved_list, getattr(self, "_saved_build_count", 0) > 7
                )
                full_brand = self._gold_text_image("AoE IV", 26)
                full_brand_size = full_brand.cget("size")
                self.brand_title_label.configure(
                    image=full_brand,
                    width=full_brand_size[0],
                    height=full_brand_size[1],
                )
                self.brand_title_label.place_configure(x=142, y=24)
                full_subtitle = self._gold_text_image("REPLAY LAUNCHER", 13)
                full_subtitle_size = full_subtitle.cget("size")
                self.brand_subtitle_label.configure(
                    text="",
                    image=full_subtitle,
                    width=full_subtitle_size[0],
                    height=full_subtitle_size[1],
                )
                self.brand_subtitle_label.place_configure(x=142, y=63)

        if density_changed:
            live_headers = []
            for label, normal_width, compact_width in self._responsive_headers:
                if label.winfo_exists():
                    label.configure(
                        width=compact_width if compact else normal_width
                    )
                    live_headers.append((label, normal_width, compact_width))
            self._responsive_headers = live_headers

            live_cells = []
            for label, normal_width, compact_width in self._responsive_cells:
                if label.winfo_exists():
                    label.configure(
                        width=compact_width if compact else normal_width
                    )
                    live_cells.append((label, normal_width, compact_width))
            self._responsive_cells = live_cells

    def _force_window_repaint(self) -> None:
        """Invalidate a resized window without synchronously flushing its children."""

        self._responsive_repaint_job = None
        if not self.winfo_exists():
            return
        if os.name != "nt":
            return
        with contextlib.suppress(Exception):
            hwnd = self._native_window_handle()
            # RDW_INVALIDATE | RDW_NOERASE | RDW_ALLCHILDREN.  Crucially this
            # omits RDW_UPDATENOW: Tk can service paints in its normal idle
            # order instead of blocking on every nested CTk child at once.
            redraw_window = ctypes.windll.user32.RedrawWindow
            redraw_window.argtypes = (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint,
            )
            redraw_window.restype = ctypes.c_int
            redraw_window(ctypes.c_void_p(hwnd), None, None, 0x00A1)

    # ---- layout -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.configure(fg_color=BG)
        self._responsive_headers: list[tuple[ctk.CTkLabel, int, int]] = []
        self._responsive_cells: list[tuple[ctk.CTkLabel, int, int]] = []
        self._reference_skin_items: dict[str, int] = {}
        self._reference_layout_signature: tuple[int, int, int] | None = None
        self.font_normal = theme.font(17)
        self.font_bold = theme.font(17)
        self.font_head = theme.font(21, weight="bold", family=theme.FONT_DISPLAY)
        self.font_section = theme.font(21, weight="bold", family=theme.FONT_DISPLAY)
        self.font_meta = theme.font(16)

        # Reference-coordinate hybrid shell: Canvas owns the exact 1672x941
        # geometry and ornament pixels; live CTk panels remain real child
        # windows so entries, buttons, scrolling and accessibility still work.
        self.reference_canvas = tk.Canvas(
            self,
            bg=BG,
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
            cursor="arrow",
        )
        self.reference_canvas.pack(fill="both", expand=True)
        root_texture = self._texture_photo("theme/surface-background-tile.png")
        self._root_texture_items = self._tile_canvas_texture(
            self.reference_canvas, root_texture, layer="bottom"
        )
        self.splitter = self.reference_canvas  # compatibility for layout helpers
        self.main_pane = ctk.CTkFrame(
            self.reference_canvas, fg_color=BG, corner_radius=0
        )
        self.download_pane = ctk.CTkFrame(
            self.reference_canvas, fg_color=BG, corner_radius=0
        )
        self._texture_frame(self.main_pane, "theme/surface-background.png")
        self._texture_frame(self.download_pane, "theme/surface-background.png")
        self._main_canvas_window = self.reference_canvas.create_window(
            5, 5, anchor="nw", window=self.main_pane
        )
        self._download_canvas_window = self.reference_canvas.create_window(
            5, 683, anchor="nw", window=self.download_pane
        )
        self.reference_canvas.bind("<Configure>", self._on_splitter_configure)
        # Listen at the toplevel too: moving from the bare divider onto either
        # embedded panel does not reliably emit <Leave> for a Canvas window item.
        self.bind("<Motion>", self._on_splitter_motion, add="+")
        self.reference_canvas.bind(
            "<Leave>", lambda _event: self.reference_canvas.configure(cursor="arrow")
        )
        self.reference_canvas.bind("<ButtonPress-1>", self._on_splitter_press)
        self.reference_canvas.bind("<B1-Motion>", self._drag_split_grip)
        self.reference_canvas.bind("<ButtonRelease-1>", self._on_splitter_release)

        # Section shells already draw their own borders and corner ornaments.
        # Keeping another framed parent here produced the duplicate outer frame
        # visible around the main content near the split line.
        self.tabs_shell = ctk.CTkFrame(
            self.main_pane,
            fg_color=PANEL,
            corner_radius=0,
            border_width=0,
        )
        self.tabs_shell.pack(fill="both", expand=True)
        self._texture_frame(self.tabs_shell)

        self.header = ctk.CTkFrame(
            self.tabs_shell, fg_color=BG, corner_radius=0, height=112
        )
        header = self.header
        header.pack(fill="x")
        header.pack_propagate(False)

        header_art_image = self._asset_image("theme/header-backdrop.png", (1600, 112))
        self.header_art = ctk.CTkLabel(
            header,
            text="",
            image=header_art_image,
            width=1600,
            height=112,
            fg_color="transparent",
        )
        self.header_art.place(relx=1, x=-1, y=-5, anchor="ne")
        self.header_art.bind("<ButtonPress-1>", self._header_art_press)
        self.header_art.bind("<B1-Motion>", self._drag_titlebar)
        self.header_art.bind(
            "<Double-Button-1>", self._header_art_double_click
        )

        # CTk's "transparent" child widgets repaint their parent's flat
        # ``fg_color``; they do not reveal bitmap content underneath.  That made
        # the three navigation buttons look like dark rectangular patches over
        # the textured header.  Keep the backdrop, lettering and active rule on
        # one native Canvas instead, where PNG alpha is composited correctly.
        self._header_nav_parent = header
        self.header_nav = tk.Canvas(
            header,
            width=600,
            height=72,
            bg=BG,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=True,
        )
        # The initial 1672 px window uses the compact navigation geometry so
        # its first label cannot slide under the 500 px brand lockup.
        self.header_nav.place(relx=0.5, x=0, y=39, anchor="n")
        self._header_nav_backdrop = self._texture_photo(
            "theme/header-backdrop.png"
        )
        self._header_nav_backdrop_item = self.header_nav.create_image(
            0,
            -44,
            anchor="nw",
            image=self._header_nav_backdrop,
            tags="nav_backdrop",
        )
        self._tab_active_images: dict[str, tk.PhotoImage | None] = {}
        self._tab_inactive_images: dict[str, tk.PhotoImage | None] = {}
        tab_titles = {
            "Profile games": "Profile Games",
            "Head-to-head": "Head to Head",
            "Saved builds": "Saved Builds",
        }
        for name, display_title in tab_titles.items():
            # Active and inactive states are derived from this exact same bold
            # 25 px mask. Only the metal colour changes, never size or weight.
            self._tab_active_images[name] = self._nav_title_photo(
                display_title, active=True
            )
            self._tab_inactive_images[name] = self._nav_title_photo(
                display_title, active=False
            )
        self.header_nav.bind("<Button-1>", self._header_nav_click)
        self.header_nav.bind("<Key-Left>", self._header_nav_previous)
        self.header_nav.bind("<Key-Right>", self._header_nav_next)
        self.header_nav.bind("<Return>", self._header_nav_activate)
        self.header_nav.bind("<space>", self._header_nav_activate)
        header.bind("<Configure>", self._header_nav_parent_configure, add="+")
        self._draw_header_navigation()

        # Use the launcher icon in compact layouts too. The normal layout uses
        # the same file inside the composited brand lockup.
        logo = self._asset_image("logo.png", (82, 82))
        if logo:
            self.logo_label = ctk.CTkLabel(
                header,
                text="",
                image=logo,
                width=86,
                height=86,
                fg_color="transparent",
            )
            self.logo_label.place(x=20, y=13)
        brand_title_image = self._gold_text_image("AoE IV", 26)
        brand_title_size = brand_title_image.cget("size")
        self.brand_title_label = ctk.CTkLabel(
            header,
            text="",
            image=brand_title_image,
            width=brand_title_size[0],
            height=brand_title_size[1],
            anchor="w",
            fg_color="transparent",
        )
        self.brand_title_label.place(x=142, y=24)
        brand_subtitle_image = self._gold_text_image("REPLAY LAUNCHER", 13)
        brand_subtitle_size = brand_subtitle_image.cget("size")
        self.brand_subtitle_label = ctk.CTkLabel(
            header,
            text="",
            image=brand_subtitle_image,
            width=brand_subtitle_size[0],
            height=brand_subtitle_size[1],
            anchor="w",
            fg_color="transparent",
        )
        self.brand_subtitle_label.place(x=142, y=63)

        # Keep the crest and wordmark in one composited lockup while navigation
        # and window controls remain independent interactive widgets.
        reference_brand = self._header_brand_image()
        self.reference_brand_label = ctk.CTkLabel(
            header,
            text="",
            image=reference_brand,
            width=500,
            height=112,
            fg_color="transparent",
        )
        self.reference_brand_label.place(x=0, y=-5)

        # Window chrome lives inside ``header_art`` so the panorama remains
        # uninterrupted and is scaled by one DPI-aware widget.
        ctk.CTkFrame(header, width=735, height=1, fg_color=GOLD_DARK).place(
            x=500, y=3
        )

        drag_widgets = [
            header,
            self.brand_title_label,
            self.brand_subtitle_label,
            self.reference_brand_label,
        ]
        if logo:
            drag_widgets.append(self.logo_label)
        self._bind_titlebar_drag(*drag_widgets)

        theme.OrnateRule(
            self.tabs_shell, bg=theme.BG_RAISED, color=GOLD_DARK, height=14
        ).pack(fill="x")
        # Shared operation status/progress dock.  It stays in the toolbar's old
        # MATCHES position, aligned with the search/filter controls.  The counter,
        # errors and download/playback progress all live inside this one frame.
        # Two text rows provide enough room for the longest built-in notification.
        self.notify_bar = ctk.CTkFrame(
            self.tabs_shell,
            fg_color="transparent",
            width=570,
            height=48,
            corner_radius=0,
        )
        notify_bar = self.notify_bar
        notify_bar.pack_propagate(False)
        self.notification_count_slot = ctk.CTkFrame(
            notify_bar,
            fg_color="transparent",
            width=240,
            height=46,
            corner_radius=0,
        )
        self.notification_count_slot.pack_propagate(False)
        notify_right = ctk.CTkFrame(
            notify_bar,
            fg_color="transparent",
            width=30,
            height=46,
            corner_radius=0,
        )
        self.notification_cancel_slot = notify_right
        notify_right.pack(side="right", fill="y")
        notify_right.pack_propagate(False)
        message_stack = ctk.CTkFrame(
            notify_bar, fg_color="transparent", corner_radius=0
        )
        self.notification_message_stack = message_stack
        self.status = SwitchingLabel(
            message_stack,
            on_text_change=lambda text: self._inline_notification_changed("status", text),
            text="",
            height=42,
            anchor="e",
            justify="right",
            text_color=MUTED,
            font=theme.font(11),
        )
        self.notify = SwitchingLabel(
            message_stack,
            on_text_change=lambda text: self._inline_notification_changed("notify", text),
            text="",
            height=42,
            anchor="e",
            justify="right",
            text_color=LOSS,
            font=theme.font(11),
        )
        # Tiny "✕" box (info-button style) right next to the download progress;
        # hidden until a cancellable download is running (see _show_cancel).
        self._cancel_button = ctk.CTkButton(
            self.tabs_shell, text="×", width=22, height=32,
            font=theme.font(11, weight="bold"), command=self._cancel_download,
            **theme.button_style("danger"),
        )
        self.tab_content = ctk.CTkFrame(self.tabs_shell, fg_color=PANEL, corner_radius=0)
        self.tab_content.pack(fill="both", expand=True)
        self.profile_tab = ctk.CTkFrame(self.tab_content, fg_color=PANEL, corner_radius=0)
        self.h2h_tab = ctk.CTkFrame(self.tab_content, fg_color=PANEL, corner_radius=0)
        self.saved_tab = ctk.CTkFrame(self.tab_content, fg_color=PANEL, corner_radius=0)
        for textured_tab in (
            self.tab_content,
            self.profile_tab,
            self.h2h_tab,
            self.saved_tab,
        ):
            self._texture_frame(textured_tab)
        self._build_profile_tab(self.profile_tab)
        self._build_h2h_tab(self.h2h_tab)
        self._build_saved_tab(self.saved_tab)
        self._switch_tab("Profile games")

        dl_shell = ctk.CTkFrame(self.download_pane, **theme.panel_style(strong=True))
        dl_shell.pack(fill="both", expand=True, padx=24)
        self._texture_frame(dl_shell)
        self.dl_header = ctk.CTkFrame(
            dl_shell, fg_color=PANEL, height=44, corner_radius=0
        )
        dl_header = self.dl_header
        dl_header.pack(fill="x", padx=(19, 29))
        dl_header.pack_propagate(False)
        self._texture_frame(dl_header, "theme/surface-dark.png")
        title_box = ctk.CTkFrame(dl_header, fg_color="transparent")
        title_box.pack(side="left", fill="y")
        self.dl_title_icon = ctk.CTkLabel(
            title_box,
            text="",
            image=self._asset_image("theme/icon-download-section.png", (36, 36)),
            width=40,
            height=40,
            fg_color="transparent",
        )
        self.dl_title_icon.pack(side="left", padx=(11, 0))
        downloaded_title_image = self._gold_text_image("Downloaded Replays", 23)
        downloaded_title_size = downloaded_title_image.cget("size")
        self.dl_title_label = ctk.CTkLabel(
            title_box,
            text="",
            image=downloaded_title_image,
            width=downloaded_title_size[0],
            height=downloaded_title_size[1],
            fg_color="transparent",
        )
        self.dl_title_label.pack(side="left", padx=(6, 32))
        self.dl_count_label = ctk.CTkLabel(
            title_box, text="", text_color=MUTED, font=self.font_meta
        )
        dl_actions = ctk.CTkFrame(dl_header, fg_color="transparent")
        dl_actions.pack(side="right", pady=(0, 2))
        self.dl_add_btn = ctk.CTkButton(
            dl_actions,
            text="",
            image=self._asset_image("theme/button-add-replay-face.png", (176, 38)),
            compound="left",
            width=180,
            height=42,
            font=self.font_bold,
            command=self._import_replay,
            **theme.button_style("primary"),
        )
        self._set_full_face_button_asset(
            self.dl_add_btn, "theme/button-add-replay-face.png", (176, 38)
        )
        self.dl_add_btn.pack(side="left", padx=(0, 12))
        # shown only when there's more than one replay (see _render_downloads_page) —
        # a download-watch-delete-one-at-a-time user never needs a "delete all".
        self.dl_delete_all_btn = ctk.CTkButton(
            dl_actions,
            text="",
            image=self._asset_image("theme/button-delete-all-face.png", (196, 38)),
            compound="left",
            width=200,
            height=42,
            font=self.font_bold,
            command=self._delete_all_replays,
            **theme.button_style("danger"),
        )
        self._set_full_face_button_asset(
            self.dl_delete_all_btn, "theme/button-delete-all-face.png", (196, 38)
        )
        theme.OrnateRule(
            dl_header, bg=PANEL, color=GOLD_DARK, center=False, height=16
        ).pack(
            side="left", fill="x", expand=True, padx=(0, 12), pady=14
        )

        self.downloads = VirtualListCanvas(
            dl_shell,
            self,
            self._draw_download_row,
            row_height=87,
        )
        self.downloads.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self._set_scrollbar_visible(self.downloads, False)
        # Page navigation for the downloaded list, mirroring the search tabs: at most
        # one page (50) of rows is built at a time so the widget/handle count stays
        # bounded. Sits fixed below the scroll area (only shown past 50 replays).
        self.dl_pager = ctk.CTkFrame(
            dl_shell, width=338, height=42, fg_color="transparent"
        )
        self.dl_pager.pack_propagate(False)
        self.dl_pager_prev = ctk.CTkButton(
            self.dl_pager,
            text="",
            image=self._asset_image("theme/button-prev-face.png", (104, 30)),
            width=108,
            height=42,
            font=self.font_bold,
            command=self.on_dl_prev_page,
            **theme.button_style("gold"),
        )
        self.dl_pager_prev.pack(side="left", padx=(0, 6))
        self.dl_pager_label = ctk.CTkLabel(
            self.dl_pager, text="", text_color=theme.TEXT_SOFT, font=self.font_bold, width=90
        )
        self.dl_pager_label.pack(side="left", padx=4)
        self.dl_pager_next = ctk.CTkButton(
            self.dl_pager,
            text="",
            image=self._asset_image("theme/button-next-face.png", (104, 30)),
            width=108,
            height=42,
            font=self.font_bold,
            command=self.on_dl_next_page,
            **theme.button_style("gold"),
        )
        self.dl_pager_next.pack(side="left", padx=(6, 0))
        theme.add_corner_ornaments(dl_shell, bg=theme.PANEL_ALT)
        dl_header.lift()
        self.after(40, self._set_initial_split)

    def _set_initial_split(self) -> None:
        if not self.reference_canvas.winfo_exists():
            return
        self.update_idletasks()
        height = self.reference_canvas.winfo_height()
        self._split_attempts += 1
        if height <= 100 and self._split_attempts < 15:
            self.after(40, self._set_initial_split)
            return
        self._grip_target_y = round(height * (678 / 941))
        self._split_ratio = 678 / 941
        self._split_initialized = True
        self._layout_reference_canvas()

    def _on_splitter_configure(self, event: tk.Event) -> None:
        if event.widget is not self.reference_canvas:
            return
        if self._resize_snapshot_active:
            self._resize_snapshot_to(event.width, event.height)
            return
        if not self._split_initialized:
            self._grip_target_y = round(event.height * (678 / 941))
        elif not getattr(self, "_split_dragging", False):
            self._grip_target_y = round(
                event.height * getattr(self, "_split_ratio", 678 / 941)
            )
        self._layout_reference_canvas(event.width, event.height)

    def _on_splitter_press(self, event: tk.Event) -> None:
        if not self._over_split_grip(event.x, event.y):
            return
        self._split_dragging = True
        self._grip_drag_offset = event.y - self._grip_target_y
        self.reference_canvas.configure(cursor="sb_v_double_arrow")

    def _over_split_grip(self, x: int, y: int) -> bool:
        """Return whether canvas-local coordinates touch the compact grip."""

        center = self.reference_canvas.winfo_width() // 2
        return abs(x - center) <= 16 and abs(y - self._grip_target_y) <= 11

    def _on_splitter_motion(self, event: tk.Event) -> None:
        """Show the resize cursor only while the pointer is over the grip."""

        x = event.x_root - self.reference_canvas.winfo_rootx()
        y = event.y_root - self.reference_canvas.winfo_rooty()
        over_grip = self._over_split_grip(x, y)
        cursor = (
            "sb_v_double_arrow"
            if over_grip or getattr(self, "_split_dragging", False)
            else "arrow"
        )
        self.reference_canvas.configure(cursor=cursor)

    def _on_splitter_release(self, _event: tk.Event) -> None:
        if not getattr(self, "_split_dragging", False):
            return
        self._split_dragging = False
        height = max(1, self.reference_canvas.winfo_height())
        self._split_ratio = self._grip_target_y / height
        self.reference_canvas.configure(cursor="arrow")
        self._layout_reference_canvas()

    def _position_split_grip(self) -> None:
        self._layout_reference_canvas()

    def _start_grip_drag(self, event: tk.Event) -> None:
        self._on_splitter_press(event)

    def _drag_split_grip(self, event: tk.Event) -> None:
        if not getattr(self, "_split_dragging", False):
            return
        height = self.reference_canvas.winfo_height()
        y = event.y - self._grip_drag_offset
        self._grip_target_y = max(
            self._split_min_main_height,
            min(y, height - self._split_min_download_height),
        )
        self._layout_reference_canvas()

    def _finish_grip_drag(self, _event: tk.Event) -> None:
        self._on_splitter_release(_event)

    def _layout_reference_canvas(
        self, width: int | None = None, height: int | None = None
    ) -> None:
        """Place live panels on the reference coordinate canvas."""

        canvas = self.reference_canvas
        if not canvas.winfo_exists():
            return
        width = width or canvas.winfo_width()
        height = height or canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        # The divider is user state, so store and reapply it as a proportion.
        # Maximize/restore can resize the Canvas while the live panes are hidden
        # under a snapshot; in that path no second <Configure> is guaranteed.
        # Recomputing here prevents an old pixel Y from silently changing 80/20
        # into a different split at the new window height.
        if (
            self._split_initialized
            and not getattr(self, "_split_dragging", False)
            and height != self._split_last_height
        ):
            self._grip_target_y = round(height * self._split_ratio)
        split_y = max(
            self._split_min_main_height,
            min(self._grip_target_y, height - self._split_min_download_height),
        )
        self._grip_target_y = split_y
        self._split_last_height = height
        signature = (width, height, split_y)
        if signature == self._reference_layout_signature:
            return
        self._reference_layout_signature = signature
        main_top = 5
        # Reserve a real uncovered band for all three grip strokes.  Previously
        # the download panel began at +3 and painted over the bottom stroke.
        main_bottom = split_y - 10
        download_top = split_y + 10
        download_bottom = height - 19
        panel_width = max(1, width - 10)
        canvas.coords(self._main_canvas_window, 5, main_top)
        canvas.itemconfigure(
            self._main_canvas_window,
            width=panel_width,
            height=max(260, main_bottom - main_top),
        )
        canvas.coords(self._download_canvas_window, 5, download_top)
        canvas.itemconfigure(
            self._download_canvas_window,
            width=panel_width,
            height=max(150, download_bottom - download_top),
        )
        self._draw_reference_canvas(width, height, split_y)

    def _draw_reference_canvas(self, width: int, height: int, split_y: int) -> None:
        """Resize the persistent ornamental shell behind the child windows."""

        canvas = self.reference_canvas
        outer = GOLD_DARK
        faint = theme.GOLD_FAINT
        items = self._reference_skin_items
        if not items:
            items.update(
                {
                    "outer": canvas.create_rectangle(
                        0,
                        0,
                        0,
                        0,
                        outline=faint,
                        width=1,
                        tags="reference_skin",
                    ),
                    "split_left": canvas.create_line(
                        0, 0, 0, 0, fill=outer, tags="reference_skin"
                    ),
                    "split_right": canvas.create_line(
                        0, 0, 0, 0, fill=outer, tags="reference_skin"
                    ),
                    "grip_top": canvas.create_line(
                        0, 0, 0, 0,
                        fill=GOLD_BRIGHT,
                        width=2,
                        capstyle=tk.ROUND,
                        tags="reference_skin",
                    ),
                    "grip_middle": canvas.create_line(
                        0, 0, 0, 0,
                        fill=GOLD_BRIGHT,
                        width=2,
                        capstyle=tk.ROUND,
                        tags="reference_skin",
                    ),
                    "grip_bottom": canvas.create_line(
                        0, 0, 0, 0,
                        fill=GOLD_BRIGHT,
                        width=2,
                        capstyle=tk.ROUND,
                        tags="reference_skin",
                    ),
                    "footer_version": canvas.create_text(
                        0,
                        0,
                        text=f"v{__version__}",
                        fill=MUTED,
                        font=(theme.FONT_BODY, 10),
                        anchor="w",
                        tags="reference_skin",
                    ),
                    "footer_author": canvas.create_text(
                        0,
                        0,
                        text="by Zavarash",
                        fill=MUTED,
                        font=(theme.FONT_BODY, 10),
                        anchor="e",
                        tags="reference_skin",
                    ),
                    "footer_author_mark": canvas.create_polygon(
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        fill="",
                        outline=GOLD_DARK,
                        width=1,
                        tags="reference_skin",
                    ),
                    "footer_author_dot": canvas.create_oval(
                        0,
                        0,
                        0,
                        0,
                        fill=GOLD_BRIGHT,
                        outline="",
                        tags="reference_skin",
                    ),
                }
            )
            for index in range(4):
                items[f"corner_outer_{index}"] = canvas.create_line(
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    fill=outer,
                    width=1,
                    tags="reference_skin",
                )
                items[f"corner_inner_{index}"] = canvas.create_line(
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    fill=faint,
                    width=1,
                    tags="reference_skin",
                )

        canvas.coords(items["outer"], 1, 1, width - 2, height - 2)
        center = width // 2
        canvas.coords(items["split_left"], 29, split_y, center - 25, split_y)
        canvas.coords(
            items["split_right"], center + 25, split_y, width - 29, split_y
        )
        canvas.coords(
            items["grip_top"], center - 11, split_y - 6, center + 11, split_y - 6
        )
        canvas.coords(
            items["grip_middle"], center - 11, split_y, center + 11, split_y
        )
        canvas.coords(
            items["grip_bottom"], center - 11, split_y + 6, center + 11, split_y + 6
        )
        canvas.coords(items["footer_version"], 18, height - 10)
        canvas.coords(items["footer_author"], width - 18, height - 10)
        author_box = canvas.bbox(items["footer_author"])
        mark_x = (author_box[0] - 11) if author_box else width - 102
        mark_y = height - 10
        canvas.coords(
            items["footer_author_mark"],
            mark_x,
            mark_y - 5,
            mark_x + 5,
            mark_y,
            mark_x,
            mark_y + 5,
            mark_x - 5,
            mark_y,
        )
        canvas.coords(
            items["footer_author_dot"],
            mark_x - 1.5,
            mark_y - 1.5,
            mark_x + 1.5,
            mark_y + 1.5,
        )
        corners = (
            (2, 1, 2, 1),
            (2, 1, height - 2, -1),
            (width - 2, -1, 2, 1),
            (width - 2, -1, height - 2, -1),
        )
        for index, (x, sx, y, sy) in enumerate(corners):
            canvas.coords(
                items[f"corner_outer_{index}"],
                x,
                y + sy * 28,
                x,
                y,
                x + sx * 28,
                y,
            )
            canvas.coords(
                items[f"corner_inner_{index}"],
                x + sx * 5,
                y + sy * 22,
                x + sx * 5,
                y + sy * 5,
                x + sx * 22,
                y + sy * 5,
            )
        if self._root_texture_items:
            canvas.tag_lower("surface_texture")
            canvas.tag_raise("reference_skin", "surface_texture")
        else:
            canvas.tag_lower("reference_skin")

    @staticmethod
    def _sync_scrollable_width(
        frame: ctk.CTkScrollableFrame,
        viewport_width: int | None = None,
        *,
        force: bool = False,
    ) -> None:
        """Keep a scrollable frame and its fluid rows locked to its live viewport."""

        canvas = getattr(frame, "_parent_canvas", None)
        window_id = getattr(frame, "_create_window_id", None)
        if canvas is not None and window_id is not None:
            if viewport_width is None:
                viewport_width = canvas.winfo_width()
            if viewport_width > 1:
                viewport_width = int(viewport_width)
                content_height = max(
                    1,
                    canvas.winfo_height(),
                    frame.winfo_reqheight(),
                )
                current_size = (viewport_width, content_height)
                previous_size = getattr(frame, "_synced_viewport_size", None)
                if current_size == previous_size and not force:
                    return
                width_changed = (
                    previous_size is None or previous_size[0] != viewport_width
                )
                canvas.itemconfigure(
                    window_id,
                    width=viewport_width,
                    height=content_height,
                )
                canvas.configure(
                    scrollregion=(0, 0, viewport_width, content_height)
                )
                if width_changed:
                    row_width = max(1, viewport_width - 12)
                    for child in frame.winfo_children():
                        if (
                            getattr(child, "_fluid_viewport_row", False)
                            and getattr(child, "_fluid_viewport_width", None)
                            != row_width
                        ):
                            child.configure(width=row_width)
                            child._fluid_viewport_width = row_width
                frame._synced_viewport_size = current_size

    def _on_scrollable_viewport_configure(
        self, frame: ctk.CTkScrollableFrame, event: tk.Event
    ) -> None:
        """Keep live rows matched to a changed viewport width."""

        self._sync_scrollable_width(frame, event.width)

    @staticmethod
    def _set_scrollbar_visible(frame: ctk.CTkScrollableFrame, visible: bool) -> None:
        """Toggle the bar while permanently reserving its layout gutter.

        ``grid_remove`` normally gives the scrollbar's column back to the
        viewport.  When results then make the bar visible, every fluid row is
        narrowed and appears to jump left.  A fixed grid-column minimum keeps
        the viewport width identical in both states.
        """

        if isinstance(frame, VirtualListCanvas):
            frame.show_scrollbar(visible)
            return

        scrollbar = getattr(frame, "_scrollbar", None)
        if scrollbar is not None:
            parent = getattr(frame, "_parent_frame", None)
            if parent is not None:
                with contextlib.suppress(Exception):
                    requested = int(scrollbar.winfo_reqwidth())
                    configured = int(float(scrollbar.cget("width")))
                    reserved = max(12, requested, configured)
                    parent.grid_columnconfigure(1, minsize=reserved)
            if visible:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()

        # Page rendering can reset the embedded window to its requested width.
        # Reassert the current viewport synchronously; the Canvas <Configure>
        # binding also handles width changes that occur after rows already exist.
        Panel._sync_scrollable_width(frame, force=True)
        frame.after_idle(
            lambda target=frame: Panel._sync_scrollable_width(target, force=True)
        )

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
            else:
                frame.pack_forget()
        self._draw_header_navigation()
        self._place_notification_bar()
        self._schedule_stable_snapshot(260)

    def _place_notification_bar(self, *, compact: bool | None = None) -> None:
        """Render notifications directly on the active textured toolbar."""

        if not hasattr(self, "notify_bar"):
            return
        # A transparent CTk child still repaints its parent's flat colour, which
        # produced the visible dark rectangle.  The labels remain state holders,
        # but the visible string is drawn on the toolbar's own textured Canvas.
        self.notify_bar.place_forget()
        self._sync_toolbar_notice()
        if self._cancel_visible:
            self._place_cancel_button()

    def _show_profile_status(self) -> None:
        """Expose profile status only when no newer notification has replaced it."""

        if self._active_notification_channel is not None:
            return
        if hasattr(self, "notification_message_stack"):
            self.notification_message_stack.pack_forget()
        if not self.notification_count_slot.winfo_manager():
            self.notification_count_slot.pack(
                side="right",
                fill="y",
                padx=(5, 5),
                before=self.notification_cancel_slot,
            )
        if not self.p_status.winfo_manager():
            self.p_status.pack(fill="both", expand=True, padx=2, pady=3)
        self._render_toolbar_notice()

    def _profile_status_changed(self, text: str) -> None:
        """A new count/loading state replaces any older inline notification."""

        if text:
            self._clear_inline_notifications()
            if getattr(self, "_active_tab", "Profile games") == "Profile games":
                self._show_profile_status()
        else:
            self.notification_count_slot.pack_forget()

    def _inline_notification_changed(self, channel: str, text: str) -> None:
        """Show only the newest notification in place of the MATCHES value."""

        other = "notify" if channel == "status" else "status"
        self._notification_texts[channel] = text
        if text:
            self._notification_texts[other] = ""
            other_widget = getattr(self, other, None)
            if isinstance(other_widget, SwitchingLabel):
                other_widget.set_text_silently("")
            self._active_notification_channel = channel
        elif self._active_notification_channel == channel:
            self._active_notification_channel = None
        self._sync_toolbar_notice()

    def _clear_inline_notifications(self) -> None:
        self._notification_texts = {"status": "", "notify": ""}
        self._active_notification_channel = None
        for name in ("status", "notify"):
            widget = getattr(self, name, None)
            if isinstance(widget, SwitchingLabel):
                widget.set_text_silently("")
        self._sync_toolbar_notice()

    def _sync_toolbar_notice(self) -> None:
        """Mutually swap MATCHES and the newest right-aligned notification."""

        if not hasattr(self, "notification_message_stack"):
            return
        stack = self.notification_message_stack
        for widget in (getattr(self, "status", None), getattr(self, "notify", None)):
            if widget is not None:
                widget.pack_forget()
        channel = self._active_notification_channel
        text = self._notification_texts.get(channel, "") if channel else ""
        if channel and text:
            self.notification_count_slot.pack_forget()
            if not stack.winfo_manager():
                stack.pack(side="left", fill="both", expand=True, padx=(4, 4), pady=3)
            getattr(self, channel).pack(fill="both", expand=True)
            self._render_toolbar_notice()
            return
        stack.pack_forget()
        if (
            getattr(self, "_active_tab", "Profile games") == "Profile games"
            and hasattr(self, "p_status")
            and self.p_status.winfo_manager()
        ):
            self._show_profile_status()
        else:
            self.notification_count_slot.pack_forget()
        self._render_toolbar_notice()

    def _notice_surface(self) -> ctk.CTkFrame | None:
        return {
            "Profile games": getattr(self, "profile_search_bar", None),
            "Head-to-head": getattr(self, "h2h_search_bar", None),
            "Saved builds": getattr(self, "saved_section_bar", None),
        }.get(getattr(self, "_active_tab", "Profile games"))

    def _ensure_notice_canvas_item(
        self, surface: ctk.CTkFrame
    ) -> tuple[tk.Canvas, int] | None:
        key = id(surface)
        cached = self._notice_canvas_items.get(key)
        if cached is not None:
            return cached[1], cached[2]
        canvas = getattr(surface, "_canvas", None)
        if canvas is None:
            return None
        item = canvas.create_text(
            0,
            0,
            text="",
            anchor="e",
            justify="right",
            fill=MUTED,
            font=(theme.FONT_BODY, 12),
            tags="toolbar_notice_text",
        )
        self._notice_canvas_items[key] = (surface, canvas, item)
        canvas.bind(
            "<Configure>",
            lambda _event, target=surface: self._layout_notice_canvas_item(target),
            add="+",
        )
        return canvas, item

    def _layout_notice_canvas_item(self, surface: ctk.CTkFrame) -> None:
        cached = self._notice_canvas_items.get(id(surface))
        if cached is None:
            return
        _, canvas, item = cached
        right_pad = 52 if self._cancel_visible and surface is self._notice_surface() else 16
        canvas.coords(
            item,
            max(1, canvas.winfo_width() - right_pad),
            max(1, canvas.winfo_height() // 2),
        )
        canvas.tag_raise(item)

    @staticmethod
    def _label_text_color(label: ctk.CTkLabel) -> str:
        color = label.cget("text_color")
        return label._apply_appearance_mode(color)

    def _render_toolbar_notice(self) -> None:
        """Draw exactly one right-aligned string with no child background."""

        for _, canvas, item in self._notice_canvas_items.values():
            canvas.itemconfigure(item, text="")
        surface = self._notice_surface()
        if surface is None:
            return
        ensured = self._ensure_notice_canvas_item(surface)
        if ensured is None:
            return
        canvas, item = ensured
        channel = self._active_notification_channel
        if channel and self._notification_texts.get(channel):
            label = getattr(self, channel)
            text = self._notification_texts[channel]
        elif (
            getattr(self, "_active_tab", "Profile games") == "Profile games"
            and hasattr(self, "p_status")
            and self.p_status.winfo_manager()
        ):
            label = self.p_status
            text = str(self.p_status.cget("text"))
        else:
            label = self.status
            text = ""
        canvas.itemconfigure(item, text=text, fill=self._label_text_color(label))
        self._layout_notice_canvas_item(surface)

    def _place_cancel_button(self) -> None:
        y = 162 if getattr(self, "_active_tab", "Profile games") != "Saved builds" else 146
        self._cancel_button.place(relx=1.0, x=-54, y=y, anchor="ne")
        self._cancel_button.lift()

    def _header_nav_geometry(
        self,
    ) -> tuple[tuple[str, int, int], ...]:
        """Return the reference-aligned hit/rule bounds for each header tab."""

        if int(self.header_nav.cget("width")) <= 600:
            return (
                ("Profile games", 8, 192),
                ("Head-to-head", 208, 392),
                ("Saved builds", 408, 592),
            )
        return (
            ("Profile games", 27, 265),
            ("Head-to-head", 300, 548),
            ("Saved builds", 564, 752),
        )

    def _draw_header_navigation(self) -> None:
        """Composite transparent tab art and its rule over the live backdrop."""

        if not hasattr(self, "header_nav") or not self.header_nav.winfo_exists():
            return
        self.header_nav.delete("nav_foreground")
        for name, left, right in self._header_nav_geometry():
            image = (
                self._tab_active_images[name]
                if name == self._active_tab
                else self._tab_inactive_images[name]
            )
            if image is not None:
                self.header_nav.create_image(
                    (left + right) // 2,
                    22,
                    anchor="n",
                    image=image,
                    tags="nav_foreground",
                )
            if name != self._active_tab:
                continue
            center = (left + right) // 2
            gap = 9
            self.header_nav.create_line(
                left,
                67,
                center - gap,
                67,
                fill=GOLD,
                width=1,
                tags="nav_foreground",
            )
            self.header_nav.create_line(
                center + gap,
                67,
                right,
                67,
                fill=GOLD,
                width=1,
                tags="nav_foreground",
            )
            self.header_nav.create_line(
                left + 5,
                71,
                center - gap - 4,
                71,
                fill=GOLD_DARK,
                width=1,
                tags="nav_foreground",
            )
            self.header_nav.create_line(
                center + gap + 4,
                71,
                right - 5,
                71,
                fill=GOLD_DARK,
                width=1,
                tags="nav_foreground",
            )
            self.header_nav.create_polygon(
                center,
                60,
                center + 7,
                67,
                center,
                74,
                center - 7,
                67,
                fill=BG,
                outline=GOLD,
                width=1,
                tags="nav_foreground",
            )
            self.header_nav.create_polygon(
                center,
                64,
                center + 3,
                67,
                center,
                70,
                center - 3,
                67,
                fill=GOLD_BRIGHT,
                outline="",
                tags="nav_foreground",
            )

    def _draw_header_controls(self) -> None:
        """Swap the single panorama between maximize and restore chrome art."""

        if not hasattr(self, "header_art"):
            return
        filename = (
            "theme/header-backdrop-maximized.png"
            if self._window_maximized
            else "theme/header-backdrop.png"
        )
        image = self._asset_image(filename, (1600, 112))
        if image is not None:
            self.header_art.configure(image=image)

    def _align_header_nav_background(self, header_width: int | None = None) -> None:
        """Align the Canvas crop with the identically placed header bitmap."""

        if not hasattr(self, "_header_nav_backdrop_item"):
            return
        width = header_width or self._header_nav_parent.winfo_width()
        nav_width = int(self.header_nav.cget("width"))
        nav_offset = 0 if nav_width <= 600 else -44
        nav_left = round(width * 0.5 + nav_offset - nav_width / 2)
        backdrop_left = width - 1 - 1600
        self.header_nav.coords(
            self._header_nav_backdrop_item,
            backdrop_left - nav_left,
            -44,
        )

    def _header_nav_parent_configure(self, event: tk.Event) -> None:
        self._align_header_nav_background(event.width)

    def _header_nav_click(self, event: tk.Event) -> None:
        self.header_nav.focus_set()
        for name, left, right in self._header_nav_geometry():
            if left <= event.x <= right:
                self._switch_tab(name)
                return

    def _header_nav_step(self, delta: int) -> None:
        tabs = ("Profile games", "Head-to-head", "Saved builds")
        index = (tabs.index(self._active_tab) + delta) % len(tabs)
        self._switch_tab(tabs[index])

    def _header_nav_previous(self, _event: tk.Event) -> str:
        self._header_nav_step(-1)
        return "break"

    def _header_nav_next(self, _event: tk.Event) -> str:
        self._header_nav_step(1)
        return "break"

    def _header_nav_activate(self, _event: tk.Event) -> str:
        self._switch_tab(self._active_tab)
        return "break"

    def _show_info(self) -> None:
        if self._info_window is not None and self._info_window.winfo_exists():
            self._info_window.focus()
            self._info_window.lift()
            return

        window = ctk.CTkToplevel(self)
        self._info_window = window
        window.title("About")
        self._apply_icon_to_window(window)
        window.geometry("560x720")
        window.resizable(False, False)
        window.configure(fg_color=BG)
        window.transient(self)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", self._close_info)

        shell = ctk.CTkFrame(window, **theme.panel_style(strong=True))
        shell.pack(fill="both", expand=True, padx=8, pady=8)
        about_title_image = self._gold_text_image(
            "AoE IV\nREPLAY LAUNCHER", 20, align="center"
        )
        about_title_size = about_title_image.cget("size")
        ctk.CTkLabel(
            shell,
            text="",
            image=about_title_image,
            width=about_title_size[0],
            height=about_title_size[1],
            fg_color="transparent",
        ).pack(pady=(24, 8))
        theme.OrnateRule(shell, bg=PANEL, color=GOLD_DARK, height=14).pack(
            fill="x", padx=28
        )
        ctk.CTkLabel(
            shell,
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
            text_color=theme.TEXT_SOFT,
            font=self.font_normal,
            wraplength=500,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=30, pady=(0, 20))

        links = ctk.CTkFrame(shell, fg_color="transparent")
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
                fg_color=CARD,
                hover_color=NEUTRAL_HOVER,
                border_width=1,
                border_color=GOLD_DARK,
                corner_radius=3,
                command=lambda target=url: webbrowser.open(target),
            ).pack(side="left", padx=8)

        # Decorative logo below the links — larger than the icons, not clickable.
        dclogo = self._asset_image("dclogo.png", (100, 100))
        if dclogo:
            ctk.CTkLabel(shell, text="", image=dclogo, fg_color="transparent").pack(
                pady=(22, 18)
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
        prev_grab = None
        with contextlib.suppress(Exception):
            prev_grab = self.grab_current()
        owner = prev_grab if prev_grab is not None and prev_grab.winfo_exists() else self
        win = ctk.CTkToplevel(owner)
        win.title(title)
        self._apply_icon_to_window(win)
        win.resizable(False, False)
        win.configure(fg_color=BG)
        # A nested message (notably Steam privacy) belongs to the currently
        # grabbed modal, not the main window. This keeps it above its caller.
        win.transient(owner)
        result = {"value": None}

        shell = ctk.CTkFrame(win, **theme.panel_style(strong=True))
        shell.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(
            shell, text=title.upper(), text_color=title_color,
            font=theme.font(18, weight="bold", family=theme.FONT_DISPLAY),
            wraplength=440, justify="left",
        ).pack(padx=28, pady=(24, 8), anchor="w")
        theme.OrnateRule(shell, bg=PANEL, color=GOLD_DARK, height=12).pack(
            fill="x", padx=24
        )
        ctk.CTkLabel(
            shell, text=message, text_color=theme.TEXT_SOFT, font=self.font_normal,
            wraplength=440, justify="left",
        ).pack(padx=28, pady=(8, 20), anchor="w")

        def _choose(value):
            result["value"] = value
            with contextlib.suppress(Exception):
                win.grab_release()
            win.destroy()
            if prev_grab is not None:
                with contextlib.suppress(Exception):
                    if prev_grab.winfo_exists():
                        prev_grab.grab_set()

        row = ctk.CTkFrame(shell, fg_color="transparent")
        row.pack(padx=28, pady=(0, 22), anchor="e")
        for index, (label, value) in enumerate(buttons):
            primary = index == len(buttons) - 1
            kind = "danger" if primary and danger else "primary" if primary else "neutral"
            ctk.CTkButton(
                row, text=label, width=120, height=40, font=self.font_bold,
                command=lambda v=value: _choose(v),
                **theme.button_style(kind),
            ).pack(side="left", padx=(8, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: _choose(None))

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.lift(owner)
        win.focus_force()
        win.after_idle(lambda: win.winfo_exists() and win.lift(owner))
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
        self._apply_icon_to_window(self)

    def _apply_icon_to_window(self, window: tk.Misc) -> None:
        """Apply the application icon to a root or modal after CTk's own reset."""

        icon = ASSET_DIR / "app.ico"
        logo = ASSET_DIR / "logo.png"
        if not icon.is_file() and not logo.is_file():
            return

        photo = getattr(self, "_window_icon_photo", None)
        if photo is None and logo.is_file():
            with contextlib.suppress(OSError, ValueError, tk.TclError):
                with Image.open(logo) as source:
                    source = source.convert("RGBA")
                    source.thumbnail((64, 64), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(source, master=self)
                self._window_icon_photo = photo

        def _set() -> None:
            with contextlib.suppress(Exception):
                if window.winfo_exists():
                    if photo is not None:
                        window.iconphoto(False, photo)
                    if icon.is_file():
                        window.iconbitmap(str(icon))

        _set()
        window.after(80, _set)
        window.after(300, _set)

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

    def _hover_asset_image(
        self, relative_path: str, size: tuple[int, int]
    ) -> ctk.CTkImage | None:
        """Return a cached brightened CTk image for a full-face button hover."""

        key = (relative_path, size)
        cached = self._hover_images.get(key)
        if cached is not None:
            return cached
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            with Image.open(path) as opened:
                source = opened.convert("RGBA")
            alpha = source.getchannel("A")
            source = ImageEnhance.Brightness(source).enhance(1.28)
            source = ImageEnhance.Contrast(source).enhance(1.06)
            source.putalpha(alpha)
            image = ctk.CTkImage(source, size=size)
        except (OSError, ValueError):
            return None
        self._hover_images[key] = image
        return image

    @staticmethod
    def _show_full_face_hover(button: ctk.CTkButton, hovered: bool) -> None:
        """Swap an image-backed button's complete face instead of only its border."""

        if not button.winfo_exists():
            return
        disabled = str(button.cget("state")) == "disabled"
        image = getattr(
            button,
            "_full_face_normal" if disabled or not hovered else "_full_face_hover",
            None,
        )
        if image is not None:
            button.configure(image=image)

    def _set_full_face_button_asset(
        self,
        button: ctk.CTkButton,
        relative_path: str,
        size: tuple[int, int],
    ) -> None:
        """Install normal/hover face art and bind the swap exactly once."""

        normal = self._asset_image(relative_path, size)
        hover = self._hover_asset_image(relative_path, size)
        button._full_face_normal = normal
        button._full_face_hover = hover or normal
        if normal is not None:
            button.configure(image=normal)
        if getattr(button, "_full_face_hover_bound", False):
            return
        button._full_face_hover_bound = True
        button.bind(
            "<Enter>",
            lambda _event, target=button: self._show_full_face_hover(target, True),
            add="+",
        )
        button.bind(
            "<Leave>",
            lambda _event, target=button: self._show_full_face_hover(target, False),
            add="+",
        )

    def _canvas_photo(
        self,
        relative_path: str,
        size: tuple[int, int] | None = None,
        *,
        crop: bool = False,
    ) -> tk.PhotoImage | None:
        """Return a cached native-Tk image for the virtual Canvas surfaces."""

        key = (relative_path, size, crop)
        cached = self._canvas_photos.get(key)
        if cached is not None:
            return cached
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            with Image.open(path) as opened:
                source = opened.convert("RGBA")
            if size is not None:
                if crop:
                    width, height = size
                    if source.width < width or source.height < height:
                        repeats_x = max(1, math.ceil(width / source.width))
                        repeats_y = max(1, math.ceil(height / source.height))
                        tiled = Image.new(
                            "RGBA",
                            (source.width * repeats_x, source.height * repeats_y),
                        )
                        for tile_y in range(repeats_y):
                            for tile_x in range(repeats_x):
                                tiled.alpha_composite(
                                    source,
                                    dest=(tile_x * source.width, tile_y * source.height),
                                )
                        source = tiled
                    source = source.crop((0, 0, width, height))
                elif source.size != size:
                    source = source.resize(size, Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(source, master=self)
        except (OSError, ValueError, tk.TclError):
            return None
        self._canvas_photos[key] = photo
        return photo

    def _canvas_hover_photo(
        self, relative_path: str, size: tuple[int, int]
    ) -> tk.PhotoImage | None:
        """Return a brighter cached face for virtual Canvas button hover."""

        key = (relative_path, size)
        cached = self._canvas_hover_photos.get(key)
        if cached is not None:
            return cached
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            with Image.open(path) as opened:
                source = opened.convert("RGBA")
            if source.size != size:
                source = source.resize(size, Image.Resampling.LANCZOS)
            alpha = source.getchannel("A")
            source = ImageEnhance.Brightness(source).enhance(1.28)
            source = ImageEnhance.Contrast(source).enhance(1.06)
            source.putalpha(alpha)
            photo = ImageTk.PhotoImage(source, master=self)
        except (OSError, ValueError, tk.TclError):
            return None
        self._canvas_hover_photos[key] = photo
        return photo

    def _canvas_disabled_photo(
        self, relative_path: str, size: tuple[int, int]
    ) -> tk.PhotoImage | None:
        """Return a cached muted face for an inactive virtual-list button."""

        key = (relative_path, size)
        cached = self._canvas_disabled_photos.get(key)
        if cached is not None:
            return cached
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            with Image.open(path) as opened:
                source = opened.convert("RGBA")
            if source.size != size:
                source = source.resize(size, Image.Resampling.LANCZOS)
            alpha = source.getchannel("A")
            source = ImageEnhance.Color(source).enhance(0.18)
            source = ImageEnhance.Brightness(source).enhance(0.58)
            source.putalpha(alpha)
            photo = ImageTk.PhotoImage(source, master=self)
        except (OSError, ValueError, tk.TclError):
            return None
        self._canvas_disabled_photos[key] = photo
        return photo

    def _gold_text_image(
        self,
        text: str,
        font_size: int,
        *,
        align: str = "left",
    ) -> ctk.CTkImage:
        """Return cached transparent heading artwork with a metallic gold fill."""

        source = _gold_text_raster(text, font_size, align=align)
        key = (f"__gold_text__/{font_size}/{align}/{text}", source.size)
        cached = self._images.get(key)
        if cached is not None:
            return cached
        image = ctk.CTkImage(source, size=source.size)
        self._images[key] = image
        return image

    def _nav_title_photo(self, text: str, *, active: bool) -> tk.PhotoImage | None:
        """Render every tab from one 25 px bold mask; vary colour only."""

        state = "active" if active else "inactive"
        key = f"__nav_title__/{state}/{text}"
        if key in self._texture_photos:
            return self._texture_photos[key]
        try:
            source = _gold_text_raster(text, 25)
            if not active:
                alpha = source.getchannel("A")
                neutral = ImageEnhance.Color(source).enhance(0.08)
                neutral = ImageEnhance.Brightness(neutral).enhance(0.88)
                warm = Image.new("RGBA", source.size, theme.TEXT_SOFT)
                source = Image.blend(neutral, warm, 0.38)
                source.putalpha(alpha)
            photo = ImageTk.PhotoImage(source, master=self)
        except (OSError, ValueError, tk.TclError):
            return None
        self._texture_photos[key] = photo
        return photo

    def _gold_asset_image(
        self, relative_path: str, size: tuple[int, int]
    ) -> ctk.CTkImage | None:
        """Return an existing monochrome icon recoloured to the theme gold."""

        key = (f"__gold__/{relative_path}", size)
        cached = self._images.get(key)
        if cached is not None:
            return cached
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            with Image.open(path) as source:
                alpha = source.convert("RGBA").getchannel("A")
            tinted = Image.new("RGBA", alpha.size, GOLD_BRIGHT)
            tinted.putalpha(alpha)
            image = ctk.CTkImage(tinted, size=size)
        except (OSError, ValueError):
            return None
        self._images[key] = image
        return image

    def _header_brand_image(self) -> ctk.CTkImage | None:
        """Return the composited launcher crest and wordmark."""

        key = ("__header_brand__", (500, 112))
        if key in self._images:
            return self._images[key]
        reference_path = ASSET_DIR / "theme" / "reference-header-left.png"
        logo_path = ASSET_DIR / "logo.png"
        if not reference_path.is_file() or not logo_path.is_file():
            return None
        try:
            with Image.open(reference_path) as source:
                composite = source.convert("RGBA")
            # Stretch a clean vertical strip of the original navy background
            # across the old crest while preserving the ornamental outer edge.
            clean_patch = composite.crop((126, 5, 138, 109)).resize(
                (112, 104), Image.Resampling.BICUBIC
            )
            composite.paste(clean_patch, (18, 5))
            # Remove the old full game name as well.  The shorter product label
            # avoids presenting the launcher as an official Age of Empires app.
            clean_wordmark = composite.crop((126, 5, 138, 109)).resize(
                (370, 104), Image.Resampling.BICUBIC
            )
            composite.paste(clean_wordmark, (130, 5))
            with Image.open(logo_path) as source:
                logo = source.convert("RGBA").resize(
                    (94, 94), Image.Resampling.LANCZOS
                )
            composite.alpha_composite(logo, dest=(25, 9))
            title_image = _gold_text_raster("AoE IV", 28)
            composite.alpha_composite(title_image, dest=(136, 18))
            subtitle_image = _gold_text_raster("REPLAY LAUNCHER", 18)
            composite.alpha_composite(subtitle_image, dest=(136, 59))
            image = ctk.CTkImage(composite, size=(500, 112))
        except (OSError, ValueError):
            return None
        self._images[key] = image
        return image

    def _match_vs_image(self) -> ctk.CTkImage:
        """Render the separator once so CTk resize redraws cannot ghost its text."""

        key = ("__match_vs__", (44, 28))
        cached = self._images.get(key)
        if cached is not None:
            return cached
        source = Image.new("RGBA", (88, 56), (0, 0, 0, 0))
        font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        try:
            font = ImageFont.truetype(str(font_dir / "palab.ttf"), 36)
        except OSError:
            font = ImageFont.load_default()
        draw = ImageDraw.Draw(source)
        bounds = draw.textbbox((0, 0), "vs", font=font)
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        draw.text(
            ((88 - text_width) / 2 - bounds[0], (56 - text_height) / 2 - bounds[1]),
            "vs",
            font=font,
            fill=theme.TEXT_SOFT,
        )
        image = ctk.CTkImage(source, size=(44, 28))
        self._images[key] = image
        return image

    def _texture_photo(self, relative_path: str) -> tk.PhotoImage | None:
        """Load a full-resolution texture once for native Tk Canvas surfaces."""

        if relative_path in self._texture_photos:
            return self._texture_photos[relative_path]
        path = ASSET_DIR / relative_path
        if not path.is_file():
            return None
        try:
            image = tk.PhotoImage(master=self, file=str(path))
        except tk.TclError:
            return None
        self._texture_photos[relative_path] = image
        return image

    def _readable_nav_inactive_photo(self, asset_name: str) -> tk.PhotoImage | None:
        """Keep inactive tab weight intact while reducing the contrast illusion."""

        key = f"__nav_inactive_readable__/{asset_name}"
        if key in self._texture_photos:
            return self._texture_photos[key]
        path = ASSET_DIR / "theme" / f"nav-{asset_name}-inactive.png"
        if not path.is_file():
            return None
        try:
            with Image.open(path) as source:
                original = source.convert("RGBA")
            alpha = original.getchannel("A")
            image = ImageEnhance.Brightness(original).enhance(1.35)
            image.putalpha(alpha)
            photo = ImageTk.PhotoImage(image, master=self)
        except (OSError, ValueError, tk.TclError):
            return None
        self._texture_photos[key] = photo
        return photo

    @staticmethod
    def _texture_tile_path(relative_path: str) -> str:
        """Return the seamless-tile companion path for a generated surface."""

        path = Path(relative_path)
        return str(path.with_name(f"{path.stem}-tile{path.suffix}")).replace(
            "\\", "/"
        )

    def _tile_canvas_texture(
        self,
        canvas: tk.Canvas,
        image: tk.PhotoImage | None,
        *,
        layer: str,
        border_item: int | None = None,
        border_width: int = 0,
    ) -> dict[tuple[int, int], int]:
        """Cover a Canvas with stable tiles, adding only newly exposed cells.

        A resize never destroys and recreates the full texture.  Existing tiles
        stay in place while only missing rows/columns are added (or surplus
        ones removed after shrinking), avoiding both blank blocks and flicker.
        """

        items: dict[tuple[int, int], int] = {}
        if image is None:
            return items
        tile_width = max(1, image.width())
        tile_height = max(1, image.height())
        last_size = (-1, -1)
        last_grid = (-1, -1)

        def arrange(event: tk.Event | None = None) -> None:
            nonlocal last_size, last_grid
            width = (
                event.width
                if event is not None and event.widget is canvas
                else canvas.winfo_width()
            )
            height = (
                event.height
                if event is not None and event.widget is canvas
                else canvas.winfo_height()
            )
            size = (width, height)
            if size == last_size:
                return
            columns = max(1, (max(1, width) + tile_width - 1) // tile_width)
            rows = max(1, (max(1, height) + tile_height - 1) // tile_height)
            grid = (columns, rows)
            if grid != last_grid:
                required = {
                    (column, row)
                    for row in range(rows)
                    for column in range(columns)
                }
                for position in tuple(items):
                    if position not in required:
                        canvas.delete(items.pop(position))
                for column, row in sorted(required - items.keys()):
                    items[(column, row)] = canvas.create_image(
                        column * tile_width,
                        row * tile_height,
                        anchor="nw",
                        image=image,
                        tags="surface_texture",
                    )
                last_grid = grid
            if layer == "bottom":
                canvas.tag_lower("surface_texture")
            elif layer == "above_inner":
                with contextlib.suppress(tk.TclError):
                    canvas.tag_raise("surface_texture", "inner_parts")
            if border_item is not None:
                inset = max(1, border_width)
                canvas.coords(
                    border_item,
                    inset,
                    inset,
                    max(inset, width - inset - 1),
                    max(inset, height - inset - 1),
                )
                canvas.tag_raise(border_item, "surface_texture")
            last_size = size

        canvas.bind("<Configure>", arrange, add="+")
        arrange()
        return items

    def _texture_frame(
        self, frame: ctk.CTkFrame, relative_path: str = "theme/surface-panel.png"
    ) -> None:
        """Layer a persistent texture over a CTkFrame's solid inner paint."""

        if getattr(frame, "_surface_texture_items", None) is not None:
            return
        image = self._texture_photo(self._texture_tile_path(relative_path))
        if image is None:
            image = self._texture_photo(relative_path)
        canvas = getattr(frame, "_canvas", None)
        if image is None or canvas is None:
            return
        border_width = int(frame.cget("border_width"))
        border_item = None
        if border_width:
            border_color = frame._apply_appearance_mode(frame.cget("border_color"))
            border_item = canvas.create_rectangle(
                0,
                0,
                0,
                0,
                outline=border_color,
                width=border_width,
                tags="surface_texture_border",
            )
            frame._surface_texture_border = border_item
        frame._surface_texture_items = self._tile_canvas_texture(
            canvas,
            image,
            layer="above_inner",
            border_item=border_item,
            border_width=border_width,
        )

    def _texture_scrollable(
        self,
        frame: ctk.CTkScrollableFrame,
        relative_path: str = "theme/surface-panel.png",
    ) -> None:
        """Texture the empty viewport behind a CTkScrollableFrame's live rows."""

        canvas = getattr(frame, "_parent_canvas", None)
        if canvas is None:
            return
        canvas.bind(
            "<Configure>",
            lambda event, target=frame: self._on_scrollable_viewport_configure(
                target, event
            ),
            add="+",
        )
        frame.bind(
            "<Configure>",
            lambda _event, target=frame: self._sync_scrollable_width(target),
            add="+",
        )
        image = self._texture_photo(self._texture_tile_path(relative_path))
        if image is None:
            image = self._texture_photo(relative_path)
        if image is None:
            return
        frame._viewport_texture_items = self._tile_canvas_texture(
            canvas, image, layer="bottom"
        )
        # CTkScrollableFrame places a plain Tk frame over its Canvas.  Its solid
        # background otherwise hides the texture wherever rows exist, most
        # noticeably in the downloaded-replays section.  A placed Canvas does
        # not affect requested size and tiles its full responsive content area.
        texture_canvas = tk.Canvas(
            frame,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
        )
        texture_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._tile_canvas_texture(texture_canvas, image, layer="bottom")
        # ``Canvas.lower`` means "lower an item tag", unlike the widget-level
        # Tk command.  Call the latter explicitly so this surface sits behind
        # the live scrollable rows.
        texture_canvas.tk.call("lower", texture_canvas._w)
        frame._surface_texture_label = texture_canvas

    @staticmethod
    def _surface_children(frame: tk.Misc) -> list[tk.Misc]:
        """Return live content children without a persistent texture widget."""

        if isinstance(frame, VirtualListCanvas):
            return list(frame.items)
        texture = getattr(frame, "_surface_texture_label", None)
        return [child for child in frame.winfo_children() if child is not texture]

    def _clear_surface_children(self, frame: tk.Misc) -> None:
        if isinstance(frame, VirtualListCanvas):
            frame.clear()
            return
        for child in self._surface_children(frame):
            child.destroy()

    def _civ_image(
        self, civ: str | None, size: tuple[int, int] = (38, 28)
    ) -> ctk.CTkImage | None:
        filename = CIV_IMAGES.get(civ or "")
        return self._asset_image(f"flags/{filename}", size) if filename else None

    def _league_image(self, rank: str) -> ctk.CTkImage | None:
        try:
            league, division = rank.lower().rsplit("_", 1)
        except ValueError:
            return None
        prefix = LEAGUE_IMAGES.get(league)
        if not prefix or division not in {"1", "2", "3"}:
            return None
        return self._asset_image(f"leagues/{prefix}{division}.png", (26, 39))

    def _canvas_league_photo(self, rank: str) -> tk.PhotoImage | None:
        """Canvas equivalent of ``_league_image`` for virtual suggestions."""

        try:
            league, division = rank.lower().rsplit("_", 1)
        except ValueError:
            return None
        prefix = LEAGUE_IMAGES.get(league)
        if not prefix or division not in {"1", "2", "3"}:
            return None
        return self._canvas_photo(f"leagues/{prefix}{division}.png", (26, 39))

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

    def _load_country_flags(self, players: list[dict]) -> bool:
        """Fetch each result's country flag from flagcdn over Happy Eyeballs +
        keep-alive: one pooled connection serves every flag, so a network with slow
        or broken IPv6 no longer pays a fresh (and possibly stalled) connect per
        flag — which was the real cost that made name searches feel slow. Returns
        True if at least one new flag was loaded (so the caller can refresh)."""
        codes = {
            str(player.get("country") or "").strip().lower()
            for player in players
            if len(str(player.get("country") or "").strip()) == 2
        }
        loaded = False
        for code in codes:
            if code in self._country_sources or code in self._country_failures:
                continue
            try:
                status, body = nethttp.get(
                    f"https://flagcdn.com/w40/{code}.png",
                    timeout=5,
                    headers={"User-Agent": "aoe4-replay-launcher"},
                )
                if status != 200:
                    raise OSError(f"flag request failed (HTTP {status})")
                with Image.open(io.BytesIO(body)) as image:
                    self._country_sources[code] = image.convert("RGBA").copy()
                loaded = True
            except (OSError, ValueError):
                self._country_failures.add(code)
        return loaded

    def _entry(self, parent: ctk.CTkFrame, placeholder: str, width: int) -> IconEntry:
        return IconEntry(
            parent,
            placeholder=placeholder,
            width=width,
            font=self.font_normal,
            icon=self._asset_image("theme/icon-search.png", (26, 26)),
        )

    def _accent_button(
        self,
        parent,
        text,
        width,
        command,
        *,
        icon_name: str | None = None,
        height: int = 48,
    ) -> ctk.CTkButton:
        reference_content = {
            "Search": ("search-face", (max(90, width - 4), max(30, height - 8))),
            "Download": ("download-face", (max(90, width - 4), max(28, height - 8))),
        }.get(text)
        button = ctk.CTkButton(
            parent,
            text="" if reference_content else text,
            image=(
                self._asset_image(
                    f"theme/button-{reference_content[0]}.png",
                    reference_content[1],
                )
                if reference_content
                else self._asset_image(f"theme/icon-{icon_name}.png", (26, 26))
                if icon_name
                else None
            ),
            compound="left",
            width=width,
            height=height,
            font=self.font_bold,
            command=command,
            **theme.button_style("primary"),
        )
        if reference_content:
            self._set_full_face_button_asset(
                button,
                f"theme/button-{reference_content[0]}.png",
                reference_content[1],
            )
        return button

    def _section_bar(self, parent: ctk.CTkFrame, icon_name: str, title: str) -> ctk.CTkFrame:
        bar = ctk.CTkFrame(parent, fg_color=PANEL, height=51, corner_radius=0)
        bar.pack(fill="x", padx=14, pady=(3, 0))
        bar.pack_propagate(False)
        self._texture_frame(bar, "theme/surface-dark.png")
        icon_asset = (
            f"theme/icon-{icon_name}-section.png"
            if icon_name == "file"
            else "sword.png"
            if icon_name == "swords"
            else f"theme/icon-{icon_name}.png"
        )
        icon_label = ctk.CTkLabel(
            bar,
            text="",
            image=self._asset_image(
                icon_asset,
                (34, 34) if icon_name == "swords" else (32, 32),
            ),
            width=41,
            height=38,
            fg_color="transparent",
        )
        icon_label.pack(
            side="left",
            padx=(15, 0) if icon_name == "swords" else (5, 0),
        )
        display_title = {
            "Player games": "Player Games",
            "Head-to-head matches": "Head to Head Matches",
            "Saved builds": "Saved Builds",
        }.get(title, title)
        title_image = self._gold_text_image(display_title, 23)
        title_size = title_image.cget("size")
        title_label = ctk.CTkLabel(
            bar,
            text="",
            image=title_image,
            fg_color="transparent",
            width=title_size[0],
            height=title_size[1],
        )
        title_label.pack(side="left", padx=(6, 12))
        theme.OrnateRule(bar, bg=PANEL, color=GOLD_DARK, center=False, height=16).pack(
            side="left", fill="x", expand=True, pady=15
        )
        return bar

    def _draw_column_header(
        self,
        row: tk.Canvas,
        *,
        include_result: bool = True,
    ) -> None:
        """Draw headings from the exact same bounds used by virtual rows."""

        width = row.winfo_width()
        height = row.winfo_height()
        if width <= 1 or height <= 1:
            return
        row.delete("header_dynamic")
        texture = self._canvas_photo("theme/surface-raised.png")
        if texture is not None:
            tile_width = max(1, texture.width())
            for x in range(0, width, tile_width):
                row.create_image(
                    x,
                    0,
                    anchor="nw",
                    image=texture,
                    tags="header_dynamic",
                )
        bounds = _result_column_bounds(width, include_result=include_result)
        headings = [
            (0, "DATE (UTC)", 15),
            (1, "MODE", 14),
            (2, "MAP", 14),
            (3, "DURATION", 14),
        ]
        if include_result:
            headings.append((4, "RESULT", 14))
        headings.extend(((5, "PLAYERS", 14), (6, "ACTION", 14)))
        for column_index, text, font_size in headings:
            left, right = bounds[column_index]
            centered = column_index >= 4
            row.create_text(
                (left + right) / 2
                if centered
                else left + (8 if column_index == 0 else 6),
                height / 2 + 1,
                anchor="center" if centered else "w",
                text=text,
                fill=theme.TEXT_SOFT,
                font=self._canvas_font(font_size, weight="bold"),
                tags="header_dynamic",
            )

    def _column_header(
        self,
        parent: ctk.CTkFrame,
        *,
        include_result: bool = True,
    ) -> tk.Canvas:
        row = tk.Canvas(
            parent,
            height=34,
            bg=theme.BG_RAISED,
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
        )
        # Match the virtual list's outer width exactly. Any different padding
        # would reintroduce proportional drift on ultrawide/full-screen layouts.
        row.pack(fill="x", padx=14, pady=(3, 0))
        row.bind(
            "<Configure>",
            lambda _event, canvas=row, result=include_result: self._draw_column_header(
                canvas,
                include_result=result,
            ),
        )
        return row

    def _build_profile_tab(self, parent: ctk.CTkFrame) -> None:
        self.profile_search_bar = ctk.CTkFrame(
            parent, height=90, **theme.panel_style(strong=True)
        )
        top = self.profile_search_bar
        top.pack(fill="x", padx=38, pady=(7, 7))
        top.pack_propagate(False)
        self._texture_frame(top)
        self.p_entry = self._entry(top, "Profile ID or player name", 357)
        self.p_entry.pack(side="left", padx=(23, 0), pady=19)
        self.p_entry.bind("<Return>", lambda _e: self.on_profile_search())
        self.p_search_btn = self._accent_button(
            top, "Search", 138, self.on_profile_search, icon_name="search"
        )
        self.p_search_btn.pack(side="left", padx=0, pady=19)
        self.p_filter_box, self.p_filter_btn, self.p_filter_clear, self.p_filter_mode = (
            self._make_filter_box(
                top, self._open_profile_filter, self._reset_profile, self._apply_profile_mode
            )
        )
        # Present the profile controls as one continuous 48 px-high toolbar group.
        # Square inner edges avoid dark wedges between adjacent rounded widgets.
        for control in (
            self.p_entry,
            self.p_filter_btn,
            self.p_filter_mode,
            self.p_search_btn,
            self.p_filter_clear,
        ):
            control.configure(height=48, corner_radius=0)
        self.p_status = SwitchingLabel(
            self.notification_count_slot,
            on_text_change=self._profile_status_changed,
            text="",
            width=228,
            height=40,
            font=self.font_bold,
            text_color=GOLD_BRIGHT,
            fg_color="transparent",
            corner_radius=0,
            anchor="e",
            justify="center",
            wraplength=218,
        )

        games_shell = ctk.CTkFrame(parent, **theme.panel_style(strong=True))
        games_shell.pack(fill="both", expand=True, padx=24, pady=(0, 9))
        self._texture_frame(games_shell)
        section = self._section_bar(games_shell, "swords", "Player games")
        self.p_refresh_btn = ctk.CTkButton(
            section,
            text="",
            image=self._gold_asset_image("theme/icon-refresh.png", (28, 28)),
            width=55,
            height=44,
            command=self._refresh_profile,
            **theme.button_style("gold"),
        )
        self.p_column_header = self._column_header(games_shell)
        self.games = VirtualListCanvas(
            games_shell,
            self,
            self._draw_game_row,
            row_height=64,
        )
        self.games.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self._set_scrollbar_visible(self.games, False)
        # Page navigation (replaces "Load more"): only one page of cards is on screen
        # at a time, so the widget/handle count stays bounded no matter how deep you go.
        self.pager = ctk.CTkFrame(
            parent, width=338, height=42, fg_color="transparent"
        )
        self.pager.pack_propagate(False)
        self.pager_prev = ctk.CTkButton(
            self.pager,
            text="",
            image=self._asset_image("theme/button-prev-face.png", (104, 30)),
            width=108,
            height=42,
            font=self.font_bold,
            command=self.on_prev_page,
            **theme.button_style("gold"),
        )
        self.pager_prev.pack(side="left", padx=(0, 6))
        self.pager_label = ctk.CTkLabel(
            self.pager, text="", text_color=theme.TEXT_SOFT, font=self.font_bold, width=90
        )
        self.pager_label.pack(side="left", padx=4)
        self.pager_next = ctk.CTkButton(
            self.pager,
            text="",
            image=self._asset_image("theme/button-next-face.png", (104, 30)),
            width=108,
            height=42,
            font=self.font_bold,
            command=self.on_next_page,
            **theme.button_style("gold"),
        )
        self.pager_next.pack(side="left", padx=(6, 0))
        theme.add_corner_ornaments(games_shell, bg=theme.PANEL_ALT)
        self.p_filter_rule = theme.PanelBottomRule(parent, bg=theme.PANEL_ALT)
        self.p_filter_rule.place(x=38, y=89, relwidth=1, width=-76, height=14)

    def _build_h2h_tab(self, parent: ctk.CTkFrame) -> None:
        self.h2h_search_bar = ctk.CTkFrame(
            parent, height=90, **theme.panel_style(strong=True)
        )
        top = self.h2h_search_bar
        top.pack(fill="x", padx=38, pady=(7, 7))
        top.pack_propagate(False)
        self._texture_frame(top)
        self.entry1 = self._entry(top, "Player 1 ID or name", 265)
        self.entry2 = self._entry(top, "Player 2 ID or name", 265)
        self.entry1.pack(side="left", padx=(23, 0), pady=19)
        self.entry2.pack(side="left", padx=0, pady=19)
        self.search_btn = self._accent_button(
            top, "Search", 138, self.on_search, icon_name="search"
        )
        self.search_btn.pack(side="left", padx=0, pady=19)
        self.h_filter_box, self.h_filter_btn, self.h_filter_clear, self.h_filter_mode = (
            self._make_filter_box(
                top, self._open_h2h_filter, self._reset_h2h, self._apply_h2h_mode
            )
        )
        # Match Profile Games: one continuous, equal-height toolbar group whose
        # inner edges remain square when the filters and Clear button appear.
        for control in (
            self.entry1,
            self.entry2,
            self.h_filter_btn,
            self.h_filter_mode,
            self.search_btn,
            self.h_filter_clear,
        ):
            control.configure(height=48, corner_radius=0)
        # far-right refresh: reload this head-to-head from page 1 with a fresh API
        # call. Packed only once matches are actually listed (see _h2h_render_page).
        matches_shell = ctk.CTkFrame(parent, **theme.panel_style(strong=True))
        matches_shell.pack(fill="both", expand=True, padx=24, pady=(0, 9))
        self._texture_frame(matches_shell)
        section = self._section_bar(matches_shell, "swords", "Head-to-head matches")
        self.h_refresh_btn = ctk.CTkButton(
            section,
            text="",
            image=self._gold_asset_image("theme/icon-refresh.png", (28, 28)),
            width=55,
            height=44,
            command=self._refresh_h2h,
            **theme.button_style("gold"),
        )
        self.h_column_header = self._column_header(
            matches_shell,
            include_result=False,
        )
        self.matches = VirtualListCanvas(
            matches_shell,
            self,
            self._draw_game_row,
            row_height=64,
        )
        self.matches.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self._set_scrollbar_visible(self.matches, False)
        # Page navigation for head-to-head, mirroring the profile tab: one page of
        # cards on screen at a time keeps the widget/handle count bounded.
        self.h_pager = ctk.CTkFrame(
            parent, width=338, height=42, fg_color="transparent"
        )
        self.h_pager.pack_propagate(False)
        self.h_pager_prev = ctk.CTkButton(
            self.h_pager,
            text="",
            image=self._asset_image("theme/button-prev-face.png", (104, 30)),
            width=108,
            height=42,
            font=self.font_bold,
            command=self.on_h2h_prev_page,
            **theme.button_style("gold"),
        )
        self.h_pager_prev.pack(side="left", padx=(0, 6))
        self.h_pager_label = ctk.CTkLabel(
            self.h_pager, text="", text_color=theme.TEXT_SOFT, font=self.font_bold, width=90
        )
        self.h_pager_label.pack(side="left", padx=4)
        self.h_pager_next = ctk.CTkButton(
            self.h_pager,
            text="",
            image=self._asset_image("theme/button-next-face.png", (104, 30)),
            width=108,
            height=42,
            font=self.font_bold,
            command=self.on_h2h_next_page,
            **theme.button_style("gold"),
        )
        self.h_pager_next.pack(side="left", padx=(6, 0))
        theme.add_corner_ornaments(matches_shell, bg=theme.PANEL_ALT)
        self.h_filter_rule = theme.PanelBottomRule(parent, bg=theme.PANEL_ALT)
        self.h_filter_rule.place(x=38, y=89, relwidth=1, width=-76, height=14)

    # ---- row cards --------------------------------------------------------

    def _cell(
        self,
        parent,
        text,
        column: int,
        *,
        color=TEXT,
        anchor="w",
        icon_name: str | None = None,
        padx: tuple[int, int] = (10, 2),
    ) -> ctk.CTkLabel:
        icon_size = {
            "calendar": (40, 26),
            "swords": (40, 28),
            "map": (32, 28),
            "clock": (34, 26),
        }.get(icon_name, (26, 26))
        if icon_name == "swords":
            icon_asset = "sword-cell.png"
        elif icon_name == "map":
            icon_asset = "map.png"
        elif icon_name == "clock":
            icon_asset = "time-cell.png"
        else:
            icon_asset = f"theme/icon-{icon_name}-cell.png"
        lbl = ctk.CTkLabel(
            parent,
            text=text,
            image=(
                self._asset_image(
                    icon_asset,
                    icon_size,
                )
                if icon_name else None
            ),
            compound="left",
            width=1,
            anchor=anchor,
            text_color=color,
            font=self.font_normal,
        )
        lbl.grid(
            row=0,
            column=column,
            sticky="ew",
            padx=padx,
            pady=(14, 4),
        )
        return lbl

    def _matchup(
        self,
        parent: ctk.CTkFrame,
        s: dict,
        *,
        align_table: bool = False,
        flush_left: bool = False,
        show_replay_icon: bool = False,
    ) -> ctk.CTkFrame:
        """Build a stable result/players grid shared by every replay list."""

        mf = ctk.CTkFrame(parent, fg_color="transparent")
        self._texture_frame(mf, "theme/surface-card.png")
        name_width = 96 if self._compact_layout else 124
        # Equal elastic gutters keep one compact, symmetric matchup centred in
        # the PLAYERS column.  The fixed middle follows the natural reading
        # order: result | nick | flag | vs | flag | nick | result.
        columns = (
            (
                (0, 0, 0),
                (1, 0, 0),
                (2, 0, 0),
                (3, 44, 0),
                (4, 52, 0),
                (5, 44, 0),
                (6, 0, 0),
                (7, 0, 0),
                (8, 0, 1),
            )
            if flush_left
            else (
                (0, 0, 1),
                (1, 92, 0),
                (2, name_width, 0),
                (3, 44, 0),
                (4, 52, 0),
                (5, 44, 0),
                (6, name_width, 0),
                (7, 92, 0),
                (8, 0, 1),
            )
        )
        for column, min_size, weight in columns:
            mf.grid_columnconfigure(column, minsize=min_size, weight=weight)
        mf.grid_rowconfigure(0, weight=1)

        winner = s.get("winner")
        left_won = winner == s.get("_id1")
        right_won = winner == s.get("_id2")
        result_known = left_won or right_won

        def add_player(
            name: str,
            civ: str | None,
            won: bool,
            roster: list[dict] | None,
            *,
            result_column: int,
            civ_column: int,
            name_column: int,
            name_anchor: str,
            leading_replay: bool = False,
            show_victory: bool = False,
        ) -> None:
            color = GREEN if won else LOSS if result_known else TEXT
            display_name = name
            civ_image = self._civ_image(civ, (38, 28))
            ctk.CTkLabel(
                mf,
                text="" if civ_image else (civ or "?"),
                image=civ_image,
                width=38,
                height=28,
                text_color=MUTED,
                font=self.font_normal,
                fg_color="transparent",
            ).grid(row=0, column=civ_column, padx=3)

            player_slot = ctk.CTkFrame(mf, fg_color="transparent")
            player_slot.grid(
                row=0,
                column=min(result_column, name_column),
                columnspan=2,
                sticky="nsew",
                padx=2,
            )
            player_group = ctk.CTkFrame(player_slot, fg_color="transparent")
            player_group.pack(
                side="right" if result_column < name_column else "left",
                fill="y",
            )

            name_label = ctk.CTkLabel(
                player_group,
                text=display_name,
                height=45,
                anchor=name_anchor,
                text_color=color,
                font=theme.player_font(name, size=17),
                fg_color="transparent",
            )
            victory_label = None
            if show_victory:
                victory_label = ctk.CTkLabel(
                    player_group,
                    text="",
                    image=self._asset_image("victory.png", (64, 48)),
                    width=64,
                    height=48,
                    fg_color="transparent",
                )
            replay_label = None
            if leading_replay:
                replay_label = ctk.CTkLabel(
                    player_group,
                    text="",
                    image=self._asset_image("replay.png", (25, 28)),
                    width=25,
                    height=28,
                    fg_color="transparent",
                )
            if result_column < name_column:
                if replay_label is not None:
                    replay_label.pack(side="left", padx=(0, 12))
                if victory_label is not None:
                    victory_label.pack(side="left", padx=(0, 9))
                name_label.pack(side="left")
            else:
                name_label.pack(side="left")
                if victory_label is not None:
                    victory_label.pack(side="left", padx=(9, 0))
            if roster and len(roster) > 1:
                self._bind_team_tooltip(name_label, roster)

        add_player(
            s["name1"],
            s.get("civ1"),
            left_won,
            s.get("team1"),
            result_column=1,
            civ_column=3,
            name_column=2,
            name_anchor="e",
            leading_replay=show_replay_icon,
            show_victory=left_won and not show_replay_icon,
        )
        ctk.CTkLabel(
            mf,
            text="",
            image=self._match_vs_image(),
            width=44,
            height=28,
            anchor="center",
            fg_color="transparent",
        ).grid(row=0, column=4, sticky="ew")
        add_player(
            s["name2"],
            s.get("civ2"),
            right_won,
            s.get("team2"),
            result_column=7,
            civ_column=5,
            name_column=6,
            name_anchor="w",
            show_victory=False,
        )
        return mf

    def _bind_team_tooltip(self, widget, roster: list[dict]) -> None:
        """Hovering the name pops the full team (name + civ). The roster is already
        in the summary (the games list carries every player) — no extra request."""
        widget.configure(cursor="hand2")
        widget.bind("<Enter>", lambda _e: self._show_roster_tip(widget, roster))
        widget.bind("<Leave>", lambda _e: self._hide_roster_tip())
        widget.bind("<Destroy>", lambda _e: self._hide_roster_tip())  # card rebuilt mid-hover

    def _roster_tip_window(self) -> ctk.CTkToplevel:
        """One reused borderless popup — only ever hidden/shown, never destroyed.
        A single window can't be left orphaned on screen the way a fresh
        create-per-hover window can (a missed <Leave> would strand it)."""
        tip = getattr(self, "_roster_tip", None)
        if tip is None or not tip.winfo_exists():
            tip = ctk.CTkToplevel(self)
            tip.withdraw()
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            self._roster_tip = tip
        return tip

    def _show_canvas_roster_tip(
        self,
        surface: VirtualListCanvas,
        bounds: tuple[float, float, float, float],
        roster: list[dict],
    ) -> None:
        self._show_roster_tip(surface.canvas, roster, canvas_bounds=bounds)

    def _show_roster_tip(
        self,
        widget,
        roster: list[dict],
        *,
        canvas_bounds: tuple[float, float, float, float] | None = None,
    ) -> None:
        tip = self._roster_tip_window()
        for child in tip.winfo_children():  # rebuild the content for this roster
            child.destroy()
        frame = ctk.CTkFrame(tip, **theme.panel_style(strong=True))
        frame.pack()
        for player in roster:
            line = ctk.CTkFrame(frame, fg_color="transparent")
            line.pack(fill="x", padx=12, pady=3)
            flag = self._civ_image(player.get("civ"))
            ctk.CTkLabel(  # flag image, or the civ abbreviation when no flag exists
                line, text="" if flag else (player.get("civ") or ""), image=flag,
                width=34, text_color=MUTED, font=self.font_normal,
            ).pack(side="left", padx=(0, 8))
            ctk.CTkLabel(
                line,
                text=player["name"],
                text_color=TEXT,
                font=theme.player_font(player["name"], size=12, weight="normal"),
                anchor="w",
            ).pack(side="left")
        # update_idletasks pumps only redraw/geometry (never mouse events), so it's
        # safe here and gives the real size — needed to keep a tall FFA roster (up to
        # 8 rows) fully on screen instead of clipped off the bottom edge.
        tip.update_idletasks()
        h, w = tip.winfo_reqheight(), tip.winfo_reqwidth()
        screen_h, screen_w = self.winfo_screenheight(), self.winfo_screenwidth()
        if canvas_bounds is None:
            x = widget.winfo_rootx()
            source_top = widget.winfo_rooty()
            y = source_top + widget.winfo_height() + 4
            self._roster_tip_canvas_source = None
        else:
            x1, y1, _x2, y2 = canvas_bounds
            x = widget.winfo_rootx() + round(x1)
            source_top = widget.winfo_rooty() + round(y1)
            y = widget.winfo_rooty() + round(y2) + 4
            self._roster_tip_canvas_source = (widget, canvas_bounds)
        if y + h > screen_h - 8:  # would overflow the bottom -> flip above the name
            y = max(8, source_top - h - 4)
        if x + w > screen_w - 8:  # would overflow the right -> pull it left
            x = max(8, screen_w - w - 8)
        tip.geometry(f"+{x}+{y}")
        tip.deiconify()
        tip.lift()
        self._roster_tip_src = widget
        self._roster_tip_deadline = time.monotonic() + 15.0  # last-resort auto-hide cap
        if getattr(self, "_roster_tip_poll", None) is None:
            self._roster_tip_poll = self.after(120, self._poll_roster_tip)

    def _pointer_over_src(self) -> bool:
        """True only if the pointer is over the source name — or one of its internal
        child widgets: a CTkLabel is a composite, so winfo_containing returns an inner
        tk widget, not the CTkLabel object (checking ``is widget`` would always fail
        and flicker the tip away). Tk hit-testing respects scroll-clipping, DPI and
        multi-monitor layouts, unlike manual rootx/width bounds math."""
        canvas_source = getattr(self, "_roster_tip_canvas_source", None)
        if canvas_source is not None:
            try:
                canvas, bounds = canvas_source
                if not canvas.winfo_exists():
                    return False
                pointer_x, pointer_y = self.winfo_pointerxy()
                local_x = pointer_x - canvas.winfo_rootx()
                local_y = pointer_y - canvas.winfo_rooty()
                x1, y1, x2, y2 = bounds
                return x1 <= local_x <= x2 and y1 <= local_y <= y2
            except Exception:  # noqa: BLE001 - a stale Canvas means the tip must hide
                return False
        widget = getattr(self, "_roster_tip_src", None)
        try:
            if widget is None or not widget.winfo_exists():
                return False
            under = self.winfo_containing(*self.winfo_pointerxy())
            if under is None:
                return False
            wpath, upath = str(widget), str(under)
            return upath == wpath or upath.startswith(wpath + ".")
        except Exception:  # noqa: BLE001 - any Tk hiccup -> treat as "not over" (hide)
            return False

    def _poll_roster_tip(self) -> None:
        """Safety net for a missed <Leave>: hide once the pointer is no longer over
        the source name. A hard deadline is the last resort if even this is fooled."""
        self._roster_tip_poll = None
        tip = getattr(self, "_roster_tip", None)
        if tip is None or not tip.winfo_exists() or not tip.winfo_viewable():
            return  # already hidden — stop polling until the next show
        if time.monotonic() >= getattr(self, "_roster_tip_deadline", 0.0):
            self._hide_roster_tip()  # absolute cap — never linger past the deadline
            return
        if self._pointer_over_src():
            self._roster_tip_poll = self.after(120, self._poll_roster_tip)
        else:
            self._hide_roster_tip()

    def _hide_roster_tip(self) -> None:
        poll = getattr(self, "_roster_tip_poll", None)
        if poll is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(poll)
            self._roster_tip_poll = None
        tip = getattr(self, "_roster_tip", None)
        self._roster_tip_canvas_source = None
        if tip is not None:
            with contextlib.suppress(Exception):
                tip.withdraw()  # reused next time; never destroyed -> can't be stranded

    @staticmethod
    def _canvas_ellipsis(value: object, limit: int) -> str:
        text = str(value or "?")
        return text if len(text) <= limit else f"{text[: max(1, limit - 1)]}…"

    def _canvas_row_background(
        self,
        surface: VirtualListCanvas,
        y: float,
        height: int,
        width: int,
        *,
        left: int = 6,
        right: int = 14,
    ) -> tuple[float, float, float, float]:
        canvas = surface.canvas
        top = y + 3
        bottom = y + height - 3
        row_right = max(left + 1, width - right - (12 if surface._scrollbar_requested else 0))
        strip = self._canvas_photo(
            "theme/surface-card-tile.png", (1024, max(1, int(bottom - top))), crop=True
        )
        if strip is not None:
            for x in range(left, int(row_right), 1024):
                canvas.create_image(
                    x,
                    top,
                    anchor="nw",
                    image=strip,
                    tags="list_dynamic",
                )
        canvas.create_rectangle(
            left,
            top,
            row_right,
            bottom,
            fill="",
            outline=theme.CARD_BORDER,
            width=1,
            tags="list_dynamic",
        )
        return float(left), top, float(row_right), bottom

    def _canvas_icon_text(
        self,
        canvas: tk.Canvas,
        x: float,
        y: float,
        icon_path: str,
        icon_size: tuple[int, int],
        text: str,
        *,
        gap: int = 8,
        color: str = TEXT,
        font=None,
    ) -> None:
        image = self._canvas_photo(icon_path, icon_size)
        if image is not None:
            canvas.create_image(
                x,
                y,
                anchor="w",
                image=image,
                tags="list_dynamic",
            )
        canvas.create_text(
            x + icon_size[0] + gap,
            y,
            anchor="w",
            text=text,
            fill=color,
            font=font or self.font_normal,
            tags="list_dynamic",
        )

    def _canvas_civ_photo(self, civ: str | None) -> tk.PhotoImage | None:
        filename = CIV_IMAGES.get(civ or "")
        return self._canvas_photo(f"flags/{filename}", (38, 28)) if filename else None

    def _canvas_country_photo(self, country: str) -> tk.PhotoImage | None:
        code = country.strip().lower()
        key = (f"__country__/{code}", (24, 18), False)
        cached = self._canvas_photos.get(key)
        if cached is not None:
            return cached
        source = self._country_sources.get(code)
        if source is None:
            return None
        try:
            photo = ImageTk.PhotoImage(
                source.resize((24, 18), Image.Resampling.LANCZOS), master=self
            )
        except (ValueError, tk.TclError):
            return None
        self._canvas_photos[key] = photo
        return photo

    def _canvas_text_width(self, text: str, font=None) -> int:
        selected = font or self.font_normal
        with contextlib.suppress(Exception):
            return int(selected.measure(text))
        return max(8, len(text) * 9)

    def _canvas_font(
        self,
        size: int,
        *,
        weight: str = "normal",
        family: str = theme.FONT_BODY,
    ) -> ctk.CTkFont:
        key = (family, int(size), weight)
        cached = self._canvas_fonts.get(key)
        if cached is None:
            cached = theme.font(size, weight=weight, family=family)
            self._canvas_fonts[key] = cached
        return cached

    def _canvas_player_font(self, text: str, size: int = 17) -> ctk.CTkFont:
        has_cjk = any(
            0x3400 <= ord(char) <= 0x4DBF
            or 0x4E00 <= ord(char) <= 0x9FFF
            or 0xF900 <= ord(char) <= 0xFAFF
            for char in text
        )
        family = "Microsoft YaHei UI" if has_cjk else theme.FONT_BODY
        return self._canvas_font(size, weight="bold", family=family)

    def _draw_canvas_matchup(
        self,
        surface: VirtualListCanvas,
        summary: dict,
        left: float,
        right: float,
        center_y: float,
        *,
        compact: bool = False,
        replay_icon: bool = False,
        hover_identity: object | None = None,
    ) -> None:
        canvas = surface.canvas
        winner = summary.get("winner")
        left_won = winner == summary.get("_id1")
        right_won = winner == summary.get("_id2")
        result_known = left_won or right_won
        full_name1 = str(summary.get("name1") or "?")
        full_name2 = str(summary.get("name2") or "?")
        # The PLAYERS column has a large dedicated share of the row. Keep the
        # actual nicknames visible instead of truncating them pre-emptively.
        name1 = full_name1
        name2 = full_name2
        roster1 = summary.get("team1")
        roster2 = summary.get("team2")
        hover_roster1 = roster1 if roster1 and len(roster1) > 1 else None
        hover_roster2 = roster2 if roster2 and len(roster2) > 1 else None
        hover_identity = (
            hover_identity
            if hover_identity is not None
            else summary.get("_canvas_key") or summary.get("game_id")
        )
        font1 = self._canvas_player_font(str(summary.get("name1") or ""), size=17)
        font2 = self._canvas_player_font(str(summary.get("name2") or ""), size=17)
        color1 = GREEN if left_won else LOSS if result_known else TEXT
        color2 = GREEN if right_won else LOSS if result_known else TEXT
        victory = self._canvas_photo("victory.png", (64, 48))
        replay_photo = self._canvas_photo("replay.png", (25, 28)) if replay_icon else None
        civ1 = self._canvas_civ_photo(summary.get("civ1"))
        civ2 = self._canvas_civ_photo(summary.get("civ2"))
        name1_width = self._canvas_text_width(name1, font1)
        name2_width = self._canvas_text_width(name2, font2)
        available = max(1, right - left)
        stable_table = compact and not replay_icon
        pieces: list[tuple[str, object, int]] = []
        if stable_table:
            # Result now has its own centred column. Keep only the symmetric
            # player cells here so names and flags never move between outcomes.
            fixed_width = 44 * 3
            remaining = max(1.0, available - fixed_width)
            name_slot = round(min(130.0, remaining / 2))
            pieces = [
                ("text1", name1, name_slot),
                ("image", civ1, 44),
                ("vs", "vs", 44),
                ("image", civ2, 44),
                ("text2", name2, name_slot),
            ]
        else:
            if replay_photo is not None:
                # Draw the 25 px film asset flush with the metadata's left
                # edge, then preserve the existing reading gap separately.
                # This moves only the film image; victory keeps its position.
                pieces.append(("image", replay_photo, 25))
                pieces.append(("spacer", None, 24))
            if not replay_icon and left_won and victory is not None:
                pieces.append(("image", victory, 61))
            pieces.extend(
                [
                    ("text1", name1, name1_width + 9),
                    ("image", civ1, 44),
                    ("vs", "vs", 44),
                    ("image", civ2, 44),
                    ("text2", name2, name2_width + 9),
                ]
            )
            # Never put Victory on the right. A right-side win means the
            # searched/left player lost, so downloaded rows show no badge.
        total = sum(piece[2] for piece in pieces)
        x = left if replay_icon else left + max(0.0, (available - total) / 2)
        if total > available:
            x = left
        for kind, value, piece_width in pieces:
            if kind == "image" and value is not None:
                canvas.create_image(
                    x + piece_width / 2,
                    center_y,
                    image=value,
                    tags="list_dynamic",
                )
            elif kind == "text1":
                text_x = x + piece_width - 6 if stable_table else x + 2
                canvas.create_text(
                    text_x,
                    center_y,
                    anchor="e" if stable_table else "w",
                    text=value,
                    fill=color1,
                    font=font1,
                    tags="list_dynamic",
                )
                if hover_roster1:
                    bounds = (
                        x,
                        center_y - 16,
                        x + piece_width if stable_table else x + name1_width + 6,
                        center_y + 16,
                    )
                    surface.add_hitbox(
                        bounds,
                        key=("roster", hover_identity, 1),
                        on_enter=lambda target=surface, area=bounds, team=hover_roster1: (
                            self._show_canvas_roster_tip(target, area, team)
                        ),
                        on_leave=self._hide_roster_tip,
                    )
            elif kind == "text2":
                canvas.create_text(
                    x + 2,
                    center_y,
                    anchor="w",
                    text=value,
                    fill=color2,
                    font=font2,
                    tags="list_dynamic",
                )
                if hover_roster2:
                    bounds = (
                        x,
                        center_y - 16,
                        x + piece_width if stable_table else x + name2_width + 6,
                        center_y + 16,
                    )
                    surface.add_hitbox(
                        bounds,
                        key=("roster", hover_identity, 2),
                        on_enter=lambda target=surface, area=bounds, team=hover_roster2: (
                            self._show_canvas_roster_tip(target, area, team)
                        ),
                        on_leave=self._hide_roster_tip,
                    )
            elif kind == "vs":
                canvas.create_text(
                    x + piece_width / 2,
                    center_y,
                    text="vs",
                    fill=theme.TEXT_SOFT,
                    font=self._canvas_font(
                        16, weight="bold", family=theme.FONT_DISPLAY
                    ),
                    tags="list_dynamic",
                )
            x += piece_width

    def _draw_game_row(
        self,
        surface: VirtualListCanvas,
        summary: dict,
        row_y: float,
        row_height: int,
        width: int,
        _index: int,
    ) -> None:
        canvas = surface.canvas
        left, top, right, bottom = self._canvas_row_background(
            surface, row_y, row_height, width
        )
        center_y = (top + bottom) / 2 + 1
        include_result = surface is not getattr(self, "matches", None)
        columns = _result_column_bounds(width, include_result=include_result)
        # Head to Head intentionally leaves RESULT empty, but all shared
        # columns retain Profile Games' canonical positions.
        player_index = 5
        action_index = 6
        starts = [column[0] for column in columns[:action_index]]
        ends = [column[1] for column in columns[:action_index]]
        action_left, action_right = columns[action_index]
        action_width = 144
        action_x = (action_left + action_right - action_width) / 2
        when = (
            summary["started_at"].strftime("%Y-%m-%d %H:%M")
            if summary.get("started_at")
            else "?"
        )
        self._canvas_icon_text(
            canvas,
            starts[0] + 8,
            center_y,
            "theme/icon-calendar-cell.png",
            (40, 32),
            when,
            gap=7,
        )
        self._canvas_icon_text(
            canvas,
            starts[1] + 6,
            center_y,
            "sword-cell.png",
            (32, 24),
            str(summary.get("kind") or "?"),
            gap=8,
        )
        self._canvas_icon_text(
            canvas,
            starts[2] + 6,
            center_y,
            "map.png",
            (28, 24),
            self._canvas_ellipsis(summary.get("map"), 18),
            gap=8,
        )
        self._canvas_icon_text(
            canvas,
            starts[3] + 6,
            center_y,
            "time-cell.png",
            (30, 24),
            _fmt_duration(summary.get("duration")),
            gap=8,
        )
        if include_result:
            result_asset = None
            if summary.get("winner") == summary.get("_id1"):
                result_asset = self._canvas_photo("victory.png", (64, 48))
            elif summary.get("winner") == summary.get("_id2"):
                # Defeat has a narrower optical footprint than Victory. A small
                # proportional scale-up equalises their perceived weight while
                # remaining safely inside the 64 px row.
                result_asset = self._canvas_photo("defeat.png", (66, 55))
            if result_asset is not None:
                result_left, result_right = columns[4]
                canvas.create_image(
                    (result_left + result_right) / 2,
                    center_y,
                    image=result_asset,
                    tags="list_dynamic",
                )
        self._draw_canvas_matchup(
            surface,
            summary,
            starts[player_index] + 2,
            ends[player_index] - 2,
            center_y,
            compact=True,
        )
        game_id = int(summary.get("game_id") or 0)
        destination = self.cfg.downloads_dir / f"AgeIV_Replay_{game_id}.rec"
        state = self._canvas_download_states.get(game_id)
        if state is None:
            state = "downloaded" if destination.is_file() else "normal"
            self._canvas_download_states[game_id] = state
        # A server-deleted replay has no button: leave the action column empty for
        # that match (the rest of the row still shows). This is the last thing drawn
        # in the row, so returning early is safe.
        if state == "deleted":
            return
        face_name = {
            "normal": "button-download-face.png",
            "loading": "button-downloading-face.png",
            "downloaded": "button-downloaded-face.png",
        }[state]
        button_key = ("download", game_id)
        face_path = f"theme/{face_name}"
        face = (
            self._canvas_hover_photo(face_path, (144, 36))
            if surface.hover_key == button_key and state == "normal"
            else self._canvas_photo(face_path, (144, 36))
        )
        if face is not None:
            canvas.create_image(
                action_x + action_width / 2,
                center_y,
                image=face,
                tags="list_dynamic",
            )
        # Downloaded and in-progress faces are status indicators, not actions.
        # Leaving them out of the hit map keeps both the hand cursor and hover
        # highlight off; only a genuinely clickable Download button reacts.
        if state == "normal":
            surface.add_hitbox(
                (action_x, center_y - 20, action_x + action_width, center_y + 20),
                lambda s=summary, target=surface: self._canvas_download_click(s, target),
                key=button_key,
                redraw_on_hover=True,
            )

    def _canvas_download_click(
        self, summary: dict, surface: VirtualListCanvas
    ) -> None:
        game_id = int(summary.get("game_id") or 0)
        if not game_id or self._canvas_download_states.get(game_id) == "downloaded":
            return
        proxy = CanvasButtonProxy(surface, ("game", game_id))
        proxy._download_face_state = self._canvas_download_states.get(game_id, "normal")
        proxy._download_complete = proxy._download_face_state == "downloaded"
        self._dl_buttons.append(proxy)
        self._download(summary, proxy, self.notify)

    def _draw_download_row(
        self,
        surface: VirtualListCanvas,
        item: dict,
        row_y: float,
        row_height: int,
        width: int,
        _index: int,
    ) -> None:
        canvas = surface.canvas
        path: Path = item["path"]
        summary = item.get("summary")
        left, top, right, bottom = self._canvas_row_background(
            surface, row_y, row_height, width, left=9, right=14
        )
        delete_x = right - 15 - 148
        play_x = delete_x - 24 - 148
        play_key = ("play", str(path))
        delete_key = ("delete", str(path))
        play_path = "theme/button-play-face.png"
        delete_path = "theme/button-delete-face.png"
        actions_disabled = self._replay_actions_locked()
        if actions_disabled:
            play_face = self._canvas_disabled_photo(play_path, (144, 42))
            delete_face = self._canvas_disabled_photo(delete_path, (144, 42))
        else:
            play_face = (
                self._canvas_hover_photo(play_path, (144, 42))
                if surface.hover_key == play_key
                else self._canvas_photo(play_path, (144, 42))
            )
            delete_face = (
                self._canvas_hover_photo(delete_path, (144, 42))
                if surface.hover_key == delete_key
                else self._canvas_photo(delete_path, (144, 42))
            )
        button_y = (top + bottom) / 2 + 1
        if play_face is not None:
            canvas.create_image(play_x + 74, button_y, image=play_face, tags="list_dynamic")
        if delete_face is not None:
            canvas.create_image(delete_x + 74, button_y, image=delete_face, tags="list_dynamic")
        if not actions_disabled:
            surface.add_hitbox(
                (play_x, button_y - 23, play_x + 148, button_y + 23),
                lambda p=path: self._play(p),
                key=play_key,
                redraw_on_hover=True,
            )
            surface.add_hitbox(
                (delete_x, button_y - 23, delete_x + 148, button_y + 23),
                lambda p=path: self._delete(p),
                key=delete_key,
                redraw_on_hover=True,
            )
        content_left = left + 24
        content_right = play_x - 18
        if summary:
            when = (
                summary["started_at"].strftime("%Y-%m-%d %H:%M")
                if summary.get("started_at")
                else "?"
            )
            meta = (
                f"{when} UTC  •  {summary.get('kind') or '?'}  •  "
                f"{summary.get('map') or '?'}  •  {_fmt_duration(summary.get('duration'))}"
            )
            canvas.create_text(
                content_left,
                top + 21,
                anchor="w",
                text=meta,
                fill=theme.TEXT_SOFT,
                font=self.font_meta,
                tags="list_dynamic",
            )
            self._draw_canvas_matchup(
                surface,
                summary,
                content_left,
                content_right,
                top + 60,
                replay_icon=True,
                hover_identity=item.get("_canvas_key"),
            )
        else:
            file_icon = self._canvas_photo("theme/icon-file.png", (18, 18))
            if file_icon is not None:
                canvas.create_image(
                    content_left,
                    top + 25,
                    anchor="w",
                    image=file_icon,
                    tags="list_dynamic",
                )
            canvas.create_text(
                content_left + 27,
                top + 25,
                anchor="w",
                text=path.name,
                fill=TEXT,
                font=self.font_bold,
                tags="list_dynamic",
            )
            canvas.create_text(
                content_left,
                top + 58,
                anchor="w",
                text="Loading details…",
                fill=theme.TEXT_SOFT,
                font=self.font_meta,
                tags="list_dynamic",
            )

    def _draw_suggestion_row(
        self,
        surface: VirtualListCanvas,
        player: dict,
        row_y: float,
        row_height: int,
        width: int,
        _index: int,
    ) -> None:
        canvas = surface.canvas
        left, top, right, bottom = self._canvas_row_background(
            surface, row_y, row_height, width
        )
        center_y = (top + bottom) / 2
        button_width = 86
        button_left = right - button_width - 12
        pid = int(player["profile_id"])
        button_key = ("select", pid)
        hovered = surface.hover_key == button_key
        canvas.create_rectangle(
            button_left,
            center_y - 22,
            button_left + button_width,
            center_y + 22,
            fill=ACCENT_HOVER if hovered else ACCENT,
            outline=BRONZE,
            width=1,
            tags="list_dynamic",
        )
        canvas.create_text(
            button_left + button_width / 2,
            center_y,
            text="Select",
            fill=TEXT,
            font=self.font_bold,
            tags="list_dynamic",
        )
        surface.add_hitbox(
            (button_left, center_y - 22, button_left + button_width, center_y + 22),
            lambda p=pid, target=surface: self._pick_suggestion(
                p,
                target,
                target._suggest_target_entry,
                target._suggest_after_pick,
            ),
            key=button_key,
            redraw_on_hover=True,
        )
        x = left + 14
        name = str(player.get("name") or "?")
        name_font = self._canvas_player_font(name, size=17)
        canvas.create_text(
            x,
            center_y,
            anchor="w",
            text=name,
            fill=TEXT,
            font=name_font,
            tags="list_dynamic",
        )
        x += self._canvas_text_width(name, name_font) + 10
        country = str(player.get("country") or "")
        country_photo = self._canvas_country_photo(country)
        if country_photo is not None:
            canvas.create_image(x, center_y, anchor="w", image=country_photo, tags="list_dynamic")
            x += 32
        elif country:
            canvas.create_text(
                x,
                center_y,
                anchor="w",
                text=country,
                fill=MUTED,
                font=self.font_meta,
                tags="list_dynamic",
            )
            x += self._canvas_text_width(country, self.font_meta) + 8
        rank = str(player.get("rank") if player.get("rank") != "unranked" else "-")
        rank_number = f" #{player['rank_num']}" if player.get("rank_num") else ""
        details = (
            f"id: {pid}  |  last: {player.get('last_game') or '--'}  |"
        )
        canvas.create_text(
            x,
            center_y,
            anchor="w",
            text=details,
            fill=MUTED,
            font=self.font_normal,
            tags="list_dynamic",
        )
        x += self._canvas_text_width(details, self.font_normal) + 8
        league_photo = self._canvas_league_photo(rank)
        if league_photo is not None:
            canvas.create_image(
                x,
                center_y,
                anchor="w",
                image=league_photo,
                tags="list_dynamic",
            )
            x += 32
            rank_text = rank_number.strip()
        else:
            rank_text = f"{rank}{rank_number}"
        if rank_text:
            canvas.create_text(
                x,
                center_y,
                anchor="w",
                text=rank_text,
                fill=MUTED,
                font=self.font_meta,
                tags="list_dynamic",
            )

    def _draw_message_row(
        self,
        surface: VirtualListCanvas,
        item: dict,
        row_y: float,
        _row_height: int,
        _width: int,
        _index: int,
    ) -> None:
        surface.canvas.create_text(
            12,
            row_y + 24,
            anchor="w",
            text=str(item.get("message") or ""),
            fill=MUTED,
            font=self.font_normal,
            tags="list_dynamic",
        )

    def _game_card(self, parent: ctk.CTkFrame, summary: dict) -> None:
        viewport = getattr(parent, "_parent_canvas", None)
        viewport_width = viewport.winfo_width() if viewport is not None else 1
        row = ctk.CTkFrame(
            parent,
            width=max(1, viewport_width - 12),
            height=58,
            fg_color=CARD,
            corner_radius=3,
            border_width=1,
            border_color=theme.CARD_BORDER,
        )
        row._fluid_viewport_row = True
        row.grid_propagate(False)
        self._texture_frame(row, "theme/surface-card.png")
        row.pack(fill="x", padx=6, pady=3)
        self._configure_result_grid(row)
        when = summary["started_at"].strftime("%Y-%m-%d %H:%M") if summary["started_at"] else "?"
        self._cell(
            row,
            when,
            0,
            icon_name="calendar",
            padx=(22, 2),
        )
        self._cell(row, summary["kind"], 1, icon_name="swords")
        self._cell(row, f"  {summary['map'] or '?'}", 2, icon_name="map")
        self._cell(
            row,
            _fmt_duration(summary["duration"]),
            3,
            icon_name="clock",
        )
        warning = self.notify
        btn = self._accent_button(
            row,
            "Download",
            148,
            command=lambda: None,
            icon_name="download",
            height=40,
        )
        btn.configure(command=lambda s=summary, b=btn, w=warning: self._download(s, b, w))
        btn.grid(
            row=0,
            column=5,
            padx=(4, 15),
            pady=(12, 2),
        )
        btn._download_complete = False
        btn._download_face_state = "normal"
        self._dl_buttons.append(btn)  # tracked so stale buttons can be pruned after row rebuilds
        self._matchup(row, summary, align_table=True).grid(
            row=0, column=4, sticky="ew", padx=(4, 8), pady=(10, 2)
        )

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
        if isinstance(frame, VirtualListCanvas):
            frame.clear()
        else:
            for child in frame.winfo_children():
                child.destroy()
        status.configure(text=f"Searching '{query}'…")
        self._suggest_token += 1  # newer search invalidates an older one's async refresh
        args = (query, frame, status, target_entry, after_pick, on_ready, self._suggest_token)
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
        if not self.p_status.winfo_manager():
            self._show_profile_status()
        self.p_status.configure(text=message)

    def _suggest_worker(
        self, query, frame, status, target_entry, after_pick, on_ready=None, token=None
    ) -> None:
        try:
            players = aoe4world.search_players(query)
        except Exception as exc:  # noqa: BLE001 - never leave the suggestion box hung
            msg = self._api_error_text(exc)

            def fail() -> None:
                if token is not None and token != self._suggest_token:
                    return  # a newer search superseded this one
                status.configure(text=msg)
                if on_ready is not None:
                    on_ready()

            self.after(0, fail)
            return

        # Show the list at once; flags are fetched afterwards so a slow flagcdn
        # can never hold the results back (it used to block the whole render).
        def render() -> None:
            if token is not None and token != self._suggest_token:
                return  # superseded — the newer search owns the frame now
            self._render_suggestions(
                players, query, frame, status, target_entry, after_pick, on_ready
            )

        self.after(0, render)

        # Load flags over Happy Eyeballs + keep-alive, then refresh so they pop in,
        # unless a newer search has taken over in the meantime.
        if self._load_country_flags(players):

            def refresh() -> None:
                if token is not None and token != self._suggest_token:
                    return
                self._render_suggestions(
                    players, query, frame, status, target_entry, after_pick, None
                )

            self.after(0, refresh)

    def _render_suggestions(
        self, players, query, frame, status, target_entry, after_pick, on_ready=None
    ) -> None:
        if isinstance(frame, VirtualListCanvas):
            frame.clear()
        else:
            for child in frame.winfo_children():
                child.destroy()
        self.after(0, lambda: self._scroll_to_top(frame))
        if on_ready is not None:
            on_ready()  # waiting for the user now -> a fresh search is allowed
        if not players:
            status.configure(text=f"No profile named '{query}'.")
            return
        status.configure(text=f"{len(players)} match(es) for '{query}' - pick one")
        if isinstance(frame, VirtualListCanvas):
            frame._suggest_target_entry = target_entry
            frame._suggest_after_pick = after_pick
            prepared = []
            for player in players:
                item = dict(player)
                item["_canvas_key"] = ("suggestion", int(player["profile_id"]))
                prepared.append(item)
            frame.set_items(
                prepared,
                renderer=self._draw_suggestion_row,
                row_height=70,
            )
            self._set_scrollbar_visible(frame, len(players) > 5)
            return
        for player in players:
            rank = player["rank"] if player["rank"] != "unranked" else "-"
            rnum = f" #{player['rank_num']}" if player["rank_num"] else ""
            viewport = getattr(frame, "_parent_canvas", None)
            viewport_width = viewport.winfo_width() if viewport is not None else 1
            row = ctk.CTkFrame(
                frame,
                width=max(1, viewport_width - 12),
                height=64,
                fg_color=CARD,
                corner_radius=3,
                border_width=1,
                border_color=theme.CARD_BORDER,
            )
            row._fluid_viewport_row = True
            row.pack_propagate(False)
            self._texture_frame(row, "theme/surface-card.png")
            row.pack(fill="x", padx=6, pady=4)
            pid = player["profile_id"]
            select = self._accent_button(
                row,
                "Select",
                80,
                command=lambda p=pid: self._pick_suggestion(p, frame, target_entry, after_pick),
                height=48,
            )
            select.pack(side="right", padx=10, pady=8)

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(
                side="left",
                fill="x",
                expand=True,
                padx=12,
                pady=6,
            )
            ctk.CTkLabel(
                info,
                text=player["name"],
                text_color=TEXT,
                font=theme.player_font(player["name"], size=17),
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
        self._set_scrollbar_visible(frame, len(players) > 5)

    def _pick_suggestion(self, profile_id, frame, target_entry, after_pick) -> None:
        target_entry.delete(0, "end")
        target_entry.insert(0, str(profile_id))
        if isinstance(frame, VirtualListCanvas):
            frame.clear()
        else:
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
            if isinstance(scroll_frame, VirtualListCanvas):
                scroll_frame.yview_moveto(0.0)
            else:
                scroll_frame._parent_canvas.yview_moveto(0.0)

    @staticmethod
    def _widget_is_inside(widget: tk.Misc, ancestor: tk.Misc) -> bool:
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    @classmethod
    def _wheel_is_over(cls, event: tk.Event, widget: tk.Misc) -> bool:
        """Hit-test in root coordinates, with ancestry as a safe fallback."""

        with contextlib.suppress(Exception):
            x_root = int(event.x_root)
            y_root = int(event.y_root)
            left = widget.winfo_rootx()
            top = widget.winfo_rooty()
            if (
                left <= x_root < left + widget.winfo_width()
                and top <= y_root < top + widget.winfo_height()
            ):
                return True
        return cls._widget_is_inside(event.widget, widget)

    def _route_match_mousewheel(self, event: tk.Event) -> str | None:
        """Dismiss the roster tip, then let CTk perform its native wheel scroll.

        Falling through to the native widget path gives games, matches and
        downloads the same pixel-based CustomTkinter behaviour and avoids a
        redundant full-window repaint on every wheel tick.
        """

        del event
        self._hide_roster_tip()
        return None

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
        self._h2h_mode = None
        self._h2h_start_page = 1
        self._h2h_page_cache = {}
        self.h_filter_btn.configure(text="Date filter")
        self.h_filter_mode.set("All modes")
        self.h_filter_clear.pack_forget()
        self.h_filter_box.pack_forget()
        self.h_pager.place_forget()
        self.matches._parent_frame.pack_configure(pady=(0, 4))
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
        self._h2h_page = 0
        self._h2h_start_page = 1
        self._h2h_total = None
        self._h2h_page_cache = {}
        self._set_h2h_busy(True)
        self._clear_surface_children(self.matches)
        self.h_pager.place_forget()
        self.matches._parent_frame.pack_configure(pady=(0, 4))
        # the date/mode filter becomes available once a search is running
        self.h_filter_box.pack(side="left", padx=0, pady=19, before=self.search_btn)
        if not self.h_filter_clear.winfo_manager():
            self.h_filter_clear.pack(
                side="left", padx=0, pady=19, after=self.search_btn
            )
        self.status.configure(text="Searching…")
        threading.Thread(target=self._h2h_page_begin, args=(id1, id2), daemon=True).start()

    def _h2h_page_begin(self, id1: int, id2: int) -> None:
        """Load the first h2h page; jump straight to the [from, to] window's start
        when an upper-bound date filter is set (the API has no 'until')."""
        try:
            start_page = 1
            if self._h2h_until:
                since = (self._h2h_until + timedelta(days=1)).isoformat()
                _, prefix = aoe4world.player_games(
                    id1, 1, since=since, leaderboard=self._h2h_mode, opponent=id2
                )
                if prefix:
                    start_page = prefix // 50 + 1
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._h2h_failed(msg))
            return
        self._h2h_start_page = start_page
        self._h2h_page_cache = {}
        self._h2h_worker(id1, id2, start_page)

    def on_h2h_prev_page(self) -> None:
        if self._searching or self._h2h_ids is None or self._h2h_page <= self._h2h_start_page:
            return
        self._lock_h2h_pager()  # lock now, before any await -> no rapid double-fire
        self._h2h_go_to_page(self._h2h_page - 1)

    def on_h2h_next_page(self) -> None:
        if self._searching or self._h2h_ids is None:
            return
        _, raw_count = self._h2h_page_cache.get(self._h2h_page, ([], 0))
        if raw_count < 50:  # last page -> nothing more to load
            return
        self._lock_h2h_pager()
        self._h2h_go_to_page(self._h2h_page + 1)

    def _lock_h2h_pager(self) -> None:
        """Lock navigation in the controller without repainting either button.

        The synchronous ``_searching`` guard rejects rapid repeat clicks. Changing
        CTk's visual state here rebuilt both buttons for one frame and looked like
        a small horizontal jump.
        """
        self._set_h2h_busy(True)  # _searching=True + Search disabled

    def _refresh_h2h(self) -> None:
        """↻ Reload this head-to-head from its first page with a fresh API call —
        e.g. to pull in a match that just finished. Keeps the active date/mode
        filters; drops the page cache."""
        if self._searching or self._h2h_ids is None:
            return
        self._start_h2h(*self._h2h_ids)

    def _h2h_go_to_page(self, page: int) -> None:
        if page in self._h2h_page_cache:
            self._h2h_render_page(page)
            return
        if self._h2h_ids is None:
            return
        self.status.configure(text="Searching…")  # pager already locked by the caller
        id1, id2 = self._h2h_ids
        threading.Thread(target=self._h2h_worker, args=(id1, id2, page), daemon=True).start()

    def _h2h_worker(self, id1: int, id2: int, page: int) -> None:
        try:
            games, total = aoe4world.player_games(
                id1, page, since=self._h2h_since, leaderboard=self._h2h_mode, opponent=id2
            )
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
        self._h2h_page_cache[page] = (summaries, len(games))
        self._h2h_total = total
        self.after(0, lambda: self._h2h_render_page(page))

    def _h2h_render_page(self, page: int) -> None:
        self._set_h2h_busy(False)
        self._h2h_page = page
        self.h_refresh_btn.pack(side="right", padx=(10, 2), pady=1)
        summaries, raw_count = self._h2h_page_cache.get(page, ([], 0))
        self._hide_roster_tip()  # a page swap destroys the cards; drop any open hover first
        for summary in summaries:
            summary["_canvas_key"] = ("game", int(summary.get("game_id") or 0))
        self.matches.set_items(
            summaries, renderer=self._draw_game_row, row_height=64
        )
        self._set_scrollbar_visible(self.matches, len(summaries) > 5)
        self.after(0, lambda: self._scroll_to_top(self.matches))
        count = len(summaries)
        self.status.configure(text=f"{count} match(es)." if count else "No matches found.")
        self._h2h_update_pager(page, raw_count)

    def _h2h_update_pager(self, page: int, raw_count: int) -> None:
        has_prev = page > self._h2h_start_page
        has_next = raw_count >= 50  # a full page means more remain
        page_text = f"Page {page - self._h2h_start_page + 1}"
        if self.h_pager_label.cget("text") != page_text:
            self.h_pager_label.configure(text=page_text)
        if has_prev or has_next:
            # Re-running pack/place for an overlay already on screen caused a
            # redundant transient geometry pass on every page transition.
            if not self.h_pager.place_info():
                self.matches._parent_frame.pack_configure(pady=(0, 39))
                self.h_pager.place(relx=0.5, x=0, rely=1, y=-4, anchor="s")
                self.h_pager.lift()
        elif self.h_pager.place_info():
            self.h_pager.place_forget()
            self.matches._parent_frame.pack_configure(pady=(0, 4))

    # ---- date-range filter (calendar) -------------------------------------

    def _make_filter_box(self, parent, on_open, on_reset, on_mode_change):
        """A (hidden) box holding the 'Date filter' button, a game-mode dropdown, and
        a reset (✕) button. Same height as the search/entry widgets. Date and mode are
        independent server-side filters; either or both can be active."""
        box = ctk.CTkFrame(parent, fg_color="transparent")
        btn = ctk.CTkButton(
            box,
            text="Date filter",
            image=self._asset_image("theme/icon-calendar.png", (26, 26)),
            compound="left",
            width=138,
            height=48,
            font=self.font_bold,
            command=on_open, **theme.button_style("neutral"),
        )
        btn.pack(side="left")
        mode = ModeMenuButton(
            box,
            values=list(_MODE_LEADERBOARDS),
            font=self.font_bold,
            command=on_mode_change,
        )
        mode.set("All modes")
        mode.pack(side="left", padx=0)
        clear = ctk.CTkButton(
            parent,
            text="",
            image=self._asset_image("theme/button-clear-face.png", (140, 40)),
            compound="left",
            width=138,
            height=48,
            font=self.font_bold,
            command=on_reset, **theme.button_style("neutral"),
        )
        self._set_full_face_button_asset(
            clear, "theme/button-clear-face.png", (140, 40)
        )
        return box, btn, clear, mode

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
        self.p_filter_btn.configure(text=f"{from_date}  →  {to_date}")
        self.p_filter_clear.pack(
            side="left", padx=0, pady=19, after=self.p_search_btn
        )
        if self._pg_profile is not None:
            self._start_profile(self._pg_profile)

    def _apply_profile_mode(self, label: str) -> None:
        self._pg_mode = _MODE_LEADERBOARDS.get(label)
        if self._pg_profile is not None:
            self._start_profile(self._pg_profile)

    def _reset_profile(self) -> None:
        """✕ resets the tab's top to its initial (just-opened) state."""
        self._pg_profile = None
        self._pg_since = None
        self._pg_until = None
        self._pg_mode = None
        self._pg_page = 0
        self._pg_start_page = 1
        self._pg_total = None
        self._pg_page_cache = {}
        self.p_entry.delete(0, "end")
        self.p_filter_btn.configure(text="Date filter")
        self.p_filter_mode.set("All modes")
        self.p_filter_clear.pack_forget()
        self.p_filter_box.pack_forget()
        self.p_status.pack_forget()
        self.notification_count_slot.pack_forget()
        self.pager.place_forget()
        self.games._parent_frame.pack_configure(pady=(0, 4))
        self.p_refresh_btn.pack_forget()
        self._clear_surface_children(self.games)
        self.p_status.configure(text="")
        self.notify.configure(text="")

    def _open_h2h_filter(self) -> None:
        self._pick_date_range(self._apply_h2h_filter)

    def _apply_h2h_filter(self, from_date: date, to_date: date) -> None:
        if to_date < from_date:
            from_date, to_date = to_date, from_date
        self._h2h_since = from_date.isoformat()
        self._h2h_until = to_date
        self.h_filter_btn.configure(text=f"{from_date}  →  {to_date}")
        self.h_filter_clear.pack(side="left", padx=0, pady=19, after=self.search_btn)
        if self._h2h_ids:
            self._start_h2h(*self._h2h_ids)

    def _apply_h2h_mode(self, label: str) -> None:
        self._h2h_mode = _MODE_LEADERBOARDS.get(label)
        if self._h2h_ids:
            self._start_h2h(*self._h2h_ids)

    def _reset_h2h(self) -> None:
        """✕ resets the tab's top to its initial (just-opened) state."""
        self._h2h_ids = None
        self._h2h_page = 0
        self._h2h_start_page = 1
        self._h2h_total = None
        self._h2h_page_cache = {}
        self._h2h_since = None
        self._h2h_until = None
        self._h2h_mode = None
        self.entry1.delete(0, "end")
        self.entry2.delete(0, "end")
        self.h_filter_btn.configure(text="Date filter")
        self.h_filter_mode.set("All modes")
        self.h_filter_clear.pack_forget()
        self.h_filter_box.pack_forget()
        self.h_pager.place_forget()
        self.matches._parent_frame.pack_configure(pady=(0, 4))
        self.h_refresh_btn.pack_forget()
        self._clear_surface_children(self.matches)
        self.status.configure(text="")
        self.notify.configure(text="")

    # ---- profile games (paginated on demand) ------------------------------

    def on_profile_search(self) -> None:
        if self._pg_searching:
            return
        value = self.p_entry.get().strip()
        self._drop_profile_filter()  # a fresh search isn't bound by an old date range
        if not self.p_status.winfo_manager():
            self._show_profile_status()
        self._set_profile_busy(True)
        self.p_status.configure(text="Resolving…")
        threading.Thread(target=self._profile_resolve, args=(value,), daemon=True).start()

    def _drop_profile_filter(self) -> None:
        self._pg_since = None
        self._pg_until = None
        self._pg_mode = None
        self.p_filter_btn.configure(text="Date filter")
        self.p_filter_mode.set("All modes")
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
        self.pager.place_forget()
        self.games._parent_frame.pack_configure(pady=(0, 4))
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
        self._pg_start_page = 1
        self._pg_total = None
        self._pg_page_cache = {}
        self._clear_surface_children(self.games)
        self.pager.place_forget()
        self.games._parent_frame.pack_configure(pady=(0, 4))
        # the date filter becomes available once a player is selected
        self.p_filter_box.pack(side="left", padx=0, pady=19, before=self.p_search_btn)
        if not self.p_filter_clear.winfo_manager():
            self.p_filter_clear.pack(
                side="left", padx=0, pady=19, after=self.p_search_btn
            )
        if not self.p_status.winfo_manager():
            self._show_profile_status()
        self._set_profile_busy(True)
        self.p_status.configure(text="Loading…")
        threading.Thread(target=self._profile_begin, args=(pid,), daemon=True).start()

    def _profile_begin(self, pid: int) -> None:
        """Load the first page of a profile. With an upper bound the API can't
        express, jump straight to the page where the [from, to] window begins
        instead of paging through everything newer than 'to'."""
        try:
            start_page = 1
            if self._pg_until:
                since = (self._pg_until + timedelta(days=1)).isoformat()
                # prefix must be counted within the same mode, or the page offset
                # would be wrong when a leaderboard filter is also active.
                _, prefix = aoe4world.player_games(
                    pid, 1, since=since, leaderboard=self._pg_mode
                )
                if prefix:
                    start_page = prefix // 50 + 1
        except Exception as exc:  # noqa: BLE001 - report instead of wedging the UI
            msg = self._api_error_text(exc)
            self.after(0, lambda: self._profile_failed(msg))
            return
        self._pg_start_page = start_page
        self._pg_page_cache = {}
        self._profile_worker(pid, start_page)

    def on_prev_page(self) -> None:
        if self._pg_searching or self._pg_profile is None or self._pg_page <= self._pg_start_page:
            return
        self._lock_profile_pager()  # lock now, before any await -> no rapid double-fire
        self._go_to_page(self._pg_page - 1)

    def on_next_page(self) -> None:
        if self._pg_searching or self._pg_profile is None:
            return
        _, raw_count = self._pg_page_cache.get(self._pg_page, ([], 0))
        if raw_count < 50:  # last page -> nothing more to load
            return
        self._lock_profile_pager()
        self._go_to_page(self._pg_page + 1)

    def _lock_profile_pager(self) -> None:
        """Lock navigation immediately without changing the button rendering.

        ``_pg_searching`` is checked before every page action, so it provides the
        same double-click protection without CTk's one-frame disabled-state redraw.
        """
        self._pg_searching = True

    def _refresh_profile(self) -> None:
        """↻ Reload the current profile from its first page with a fresh API call —
        e.g. to pull in a match that just finished, whose replay may still be
        arriving. Keeps the active date/mode filters; drops the page cache."""
        if self._pg_searching or self._pg_profile is None:
            return
        self._start_profile(self._pg_profile)

    def _go_to_page(self, page: int) -> None:
        """Show ``page``: instantly from the RAM cache when already fetched, else
        fetch it. Only the current page's cards are ever built, so the widget/handle
        count stays bounded however far you page. (The caller has already locked
        the pager.)"""
        if page in self._pg_page_cache:
            self._render_page(page)
            return
        self.p_search_btn.configure(state="disabled")
        self.p_status.configure(text="Loading…")
        threading.Thread(
            target=self._profile_worker, args=(self._pg_profile, page), daemon=True
        ).start()

    def _profile_worker(self, pid: int, page: int) -> None:
        try:
            games, total = aoe4world.player_games(
                pid, page, since=self._pg_since, leaderboard=self._pg_mode
            )
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
        self._pg_page_cache[page] = (summaries, len(games))
        self._pg_total = total
        self.after(0, lambda: self._render_page(page))

    def _render_page(self, page: int) -> None:
        """Replace the on-screen cards with this page's, destroying the previous
        page's widgets so only one page's worth ever exist at once."""
        self._pg_searching = False
        self._pg_page = page
        self.p_search_btn.configure(state="normal")
        self.p_refresh_btn.pack(side="right", padx=(10, 2), pady=1)
        summaries, raw_count = self._pg_page_cache.get(page, ([], 0))
        self._hide_roster_tip()  # a page swap destroys the cards; drop any open hover first
        for summary in summaries:
            summary["_canvas_key"] = ("game", int(summary.get("game_id") or 0))
        self.games.set_items(
            summaries, renderer=self._draw_game_row, row_height=64
        )
        self._set_scrollbar_visible(self.games, len(summaries) > 5)
        self.after(0, lambda: self._scroll_to_top(self.games))
        count = len(summaries)
        if self._pg_until:
            text = f"{count} MATCHES" if count else "NO MATCHES"
        else:
            total = self._pg_total or count
            text = f"{total} MATCHES" if total else "NO MATCHES"
        self.p_status.configure(text=text)
        self._update_pager(page, raw_count)

    def _update_pager(self, page: int, raw_count: int) -> None:
        has_prev = page > self._pg_start_page
        has_next = raw_count >= 50  # a full page means more remain
        page_text = f"Page {page - self._pg_start_page + 1}"
        if self.pager_label.cget("text") != page_text:
            self.pager_label.configure(text=page_text)
        if has_prev or has_next:  # only show the bar when there's somewhere to go
            if not self.pager.place_info():
                self.games._parent_frame.pack_configure(pady=(0, 39))
                self.pager.place(relx=0.5, x=0, rely=1, y=-4, anchor="s")
                self.pager.lift()
        elif self.pager.place_info():
            self.pager.place_forget()
            self.games._parent_frame.pack_configure(pady=(0, 4))

    # ---- download ---------------------------------------------------------

    def _set_download_button_face(
        self, btn: ctk.CTkButton | CanvasButtonProxy, state: str
    ) -> None:
        """Swap one same-sized bitmap without rebuilding the CTk button geometry."""

        if isinstance(btn, CanvasButtonProxy):
            game_id = btn.key[1] if isinstance(btn.key, tuple) else btn.key
            self._canvas_download_states[int(game_id)] = state
            btn._download_complete = state == "downloaded"
            btn._download_face_state = state
            if state != "normal":
                btn.surface._clear_hover()
                btn.surface.canvas.configure(cursor="arrow")
            btn.surface.refresh()
            return

        faces = {
            "normal": "theme/button-download-face.png",
            "loading": "theme/button-downloading-face.png",
            "downloaded": "theme/button-downloaded-face.png",
        }
        completed = state == "downloaded"
        previous_completed = getattr(btn, "_download_complete", False)
        btn._download_complete = completed
        if getattr(btn, "_download_face_state", None) == state:
            return

        # Width, height, text and CTk state stay immutable. Updating any of those
        # options makes CustomTkinter briefly rebuild its internal grid. The
        # controller guards clicks while downloading, so only the same-sized image
        # changes here; the colour style changes once, when completion turns green.
        options = {"image": self._asset_image(faces[state], (144, 32))}
        if completed != previous_completed:
            options.update(theme.button_style("success" if completed else "primary"))
        btn.configure(**options)
        btn._download_face_state = state

    def _set_downloads_enabled(self, enabled: bool) -> None:
        """Record the global lock without repainting every Download button.

        ``_downloading`` is the authoritative synchronous click guard. Keeping the
        CTk widgets in one visual state prevents the mass disabled/normal redraw
        that used to produce a momentary shift in the action column.
        """
        del enabled
        self._dl_buttons = [b for b in self._dl_buttons if b.winfo_exists()]

    def _download(
        self,
        summary: dict,
        btn: ctk.CTkButton | CanvasButtonProxy,
        warning: ctk.CTkLabel,
    ) -> None:
        if self._downloading or getattr(btn, "_download_complete", False):
            # One replay at a time; a completed face is intentionally inert too.
            return
        self._downloading = True
        self._set_downloads_enabled(False)  # disable every Download button while this runs
        self._set_download_button_face(btn, "loading")
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

    def _download_done(
        self,
        btn: ctk.CTkButton | CanvasButtonProxy,
        ok: bool,
        warning: ctk.CTkLabel,
    ) -> None:
        self._downloading = False
        # the button/row may have been rebuilt (new search/reset) during the download
        if ok:
            with contextlib.suppress(Exception):
                self._set_download_button_face(btn, "downloaded")
            warning.configure(text="")  # a successful download clears any prior warning
            self._save_match_info()  # persist the summary (set by the worker) across runs
            self.refresh_downloads()
        else:
            # The replay is gone from the server: remove its Download button for that
            # match. On the virtual canvas that means marking it "deleted" so the row
            # renderer draws no button (the old CTk path destroyed the widget outright).
            if isinstance(btn, CanvasButtonProxy):
                self._set_download_button_face(btn, "deleted")
            else:
                with contextlib.suppress(Exception):
                    btn.destroy()
            self._show_deleted_warning(warning)
        self._set_downloads_enabled(True)  # re-enable the rest (a finished one stays disabled)

    def _show_deleted_warning(self, warning: ctk.CTkLabel) -> None:
        # clear first so a repeat failure visibly re-appears (re-written, not stale)
        with contextlib.suppress(Exception):
            warning.configure(text="")
            warning.after(
                80, lambda: warning.configure(text="Replay has been deleted!", text_color=LOSS)
            )

    def _download_error(
        self, btn: ctk.CTkButton | CanvasButtonProxy, message: str
    ) -> None:
        self._downloading = False
        with contextlib.suppress(Exception):
            self._set_download_button_face(btn, "normal")
        self._set_downloads_enabled(True)
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

    def _replay_order_key(self, path: Path) -> datetime:
        """Chronological sort key: the replay's own recorded time, so a manually added
        replay slots in by its match date instead of jumping to the top by filename.
        Cached per (path, mtime, size) so repeated refreshes don't re-read files;
        falls back to the file's mtime when the embedded timestamp can't be read."""
        try:
            st = path.stat()
        except OSError:
            return datetime.min
        key = (str(path), st.st_mtime_ns, st.st_size)
        cached = self._stamp_cache.get(key)
        if cached is not None:
            return cached
        stamp = replay.read_timestamp_optional(path)
        value = stamp.value if stamp is not None else datetime.fromtimestamp(st.st_mtime)
        self._stamp_cache[key] = value
        return value

    def refresh_downloads(self) -> None:
        folder = self.cfg.downloads_dir
        self._dl_files = (
            sorted(folder.glob("*.rec"), key=self._replay_order_key, reverse=True)
            if folder.exists() else []
        )
        self._prune_match_info(self._dl_files)  # forget replays deleted in-app or manually
        self._dl_page = min(max(1, self._dl_page), self._dl_total_pages())  # clamp after delete
        self._render_downloads_page()

    def _dl_total_pages(self) -> int:
        return max(1, (len(self._dl_files) + 49) // 50)

    def on_dl_prev_page(self) -> None:
        if self._dl_page > 1:
            self._dl_page -= 1
            self._render_downloads_page()
            self.after(0, lambda: self._scroll_to_top(self.downloads))

    def on_dl_next_page(self) -> None:
        if self._dl_page < self._dl_total_pages():
            self._dl_page += 1
            self._render_downloads_page()
            self.after(0, lambda: self._scroll_to_top(self.downloads))

    def _update_dl_pager(self) -> None:
        total_pages = self._dl_total_pages()
        if total_pages <= 1:  # 50 or fewer -> no navigation needed
            self.dl_pager.pack_forget()
            return
        page_text = f"Page {self._dl_page}"
        if self.dl_pager_label.cget("text") != page_text:
            self.dl_pager_label.configure(text=page_text)
        if not self.dl_pager.winfo_manager():
            # The list was packed first with expand=True. Insert the pager before
            # it in packing order so the list cannot consume the pager's parcel
            # when the split pane or the outer window becomes short.
            self.dl_pager.pack(
                side="bottom",
                pady=(0, 6),
                before=self.downloads,
            )

    def _render_downloads_page(self) -> None:
        self._hide_roster_tip()  # a page swap destroys the rows; drop any open hover first
        self._play_buttons.clear()
        if not self._dl_files:
            self._set_scrollbar_visible(self.downloads, False)
            self.dl_pager.pack_forget()
            self.dl_delete_all_btn.pack_forget()
            self.dl_count_label.configure(text="0 replays")
            self.downloads.set_items(
                [{"_canvas_key": "empty", "message": "No downloaded replays yet."}],
                renderer=self._draw_message_row,
                row_height=50,
            )
            return
        context = self._load_search_context()  # game_id -> [searched_id, opponent_id]
        start = (self._dl_page - 1) * 50
        visible_paths = self._dl_files[start : start + 50]
        items = []
        for path in visible_paths:
            game_id = aoe4world.game_id_from_name(path.name)
            info = self._info_cache.get(game_id) if game_id else None
            items.append(
                {
                    "_canvas_key": ("replay", str(path)),
                    "path": path,
                    "game_id": game_id,
                    "summary": info,
                }
            )
            if game_id and info is None and game_id not in self._info_fetching:
                ctx = context.get(str(game_id))
                ids = (int(ctx[0]), int(ctx[1])) if ctx and len(ctx) >= 2 else None
                self._info_fetching.add(game_id)
                args = (game_id, None, ids)
                threading.Thread(target=self._fetch_info, args=args, daemon=True).start()
        self.downloads.set_items(
            items,
            renderer=self._draw_download_row,
            row_height=87,
        )
        count = len(self._dl_files)
        visible_count = len(visible_paths)
        self._set_scrollbar_visible(self.downloads, visible_count > 2)
        self.dl_count_label.configure(text=f"{count} replay{'s' if count != 1 else ''}")
        if count > 1:  # a one-in-one-out user never sees a pointless "delete all"
            self.dl_delete_all_btn.pack(side="right")
        else:
            self.dl_delete_all_btn.pack_forget()
        self._update_dl_pager()

    def _render_download_summary(self, parent: ctk.CTkFrame, summary: dict) -> None:
        # Download rows are (re)built here — including asynchronously by _fetch_info
        # once a match's details arrive — so drop any open hover first; otherwise a
        # tooltip whose source label is about to be destroyed could be stranded.
        self._hide_roster_tip()
        for child in parent.winfo_children():
            child.destroy()
        when = (
            summary["started_at"].strftime("%Y-%m-%d %H:%M")
            if summary["started_at"]
            else "?"
        )
        ctk.CTkLabel(
            parent,
            text=f"{when} UTC  •  {summary['kind']}  •  {summary['map']}"
            f"  •  {_fmt_duration(summary['duration'])}",
            anchor="w",
            text_color=theme.TEXT_SOFT,
            font=self.font_meta,
        ).pack(anchor="w")
        self._download_matchup(parent, summary).pack(anchor="w", pady=(7, 0))

    def _download_matchup(self, parent: ctk.CTkFrame, summary: dict) -> ctk.CTkFrame:
        """Use the shared player renderer, flush with the downloaded row metadata."""

        return self._matchup(
            parent,
            summary,
            flush_left=True,
            show_replay_icon=True,
        )

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

    def _write_search_context(self, data: dict) -> None:
        with contextlib.suppress(Exception):
            path = self._search_context_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)

    def _save_search_context(self, game_id: int, id1: int, id2: int) -> None:
        data = self._load_search_context()
        data[str(game_id)] = [int(id1), int(id2)]
        self._write_search_context(data)

    # --- match-info cache: a match summary is static once the match is over, so we
    # persist it and reuse it across runs — no per-replay API call on startup. An
    # entry is dropped when its replay file is gone (deleted in-app or manually).
    # All writes run on the UI thread, so concurrent fetches never clash. ----------

    def _match_info_path(self) -> Path:
        return self.cfg.downloads_dir / ".match-info.json"

    @staticmethod
    def _encode_summary(summary: dict) -> dict:
        d = dict(summary)
        started = d.get("started_at")
        d["started_at"] = started.isoformat() if isinstance(started, datetime) else None
        return d

    @staticmethod
    def _decode_summary(d: dict) -> dict:
        d = dict(d)
        started = d.get("started_at")
        d["started_at"] = None
        if isinstance(started, str):
            with contextlib.suppress(ValueError):
                d["started_at"] = datetime.fromisoformat(started)
        return d

    def _load_match_info(self) -> dict[int, dict]:
        with contextlib.suppress(Exception):
            raw = json.loads(self._match_info_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    int(k): self._decode_summary(v)
                    for k, v in raw.items() if k.isdigit() and isinstance(v, dict)
                }
        return {}

    def _save_match_info(self) -> None:
        with contextlib.suppress(Exception):
            path = self._match_info_path()
            # snapshot the dict (a concurrent fetch may mutate it) before serialising
            data = {str(g): self._encode_summary(s) for g, s in list(self._info_cache.items())}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)

    def _prune_match_info(self, files: list[Path]) -> None:
        """Drop cached info + search context for replays whose file is gone, so
        deleting a replay (in-app or manually) drops its stored info too."""
        alive = {aoe4world.game_id_from_name(p.name) for p in files}
        alive.discard(None)
        stale = [g for g in list(self._info_cache) if g not in alive]
        if stale:
            for g in stale:
                self._info_cache.pop(g, None)
            self._save_match_info()
        ctx = self._load_search_context()
        kept = {k: v for k, v in ctx.items() if not (k.isdigit() and int(k) not in alive)}
        if len(kept) != len(ctx):
            self._write_search_context(kept)

    def _fetch_info(
        self,
        game_id: int,
        label: ctk.CTkLabel | None,
        ids: tuple[int, int] | None = None,
    ) -> None:
        try:
            summary = aoe4world.game_summary(game_id, ids)
        except Exception:
            summary = None
        if not summary:
            self.after(0, lambda: self._info_fetching.discard(game_id))
            return
        self._info_cache[game_id] = summary

        def update() -> None:
            self._info_fetching.discard(game_id)
            if label is not None:
                # Legacy fallback for any non-virtual surface.
                with contextlib.suppress(Exception):
                    self._render_download_summary(label.master, summary)
            elif isinstance(self.downloads, VirtualListCanvas):
                for item in self.downloads.items:
                    if isinstance(item, dict) and item.get("game_id") == game_id:
                        item["summary"] = summary
                self.downloads.refresh()
            self._save_match_info()  # persist so this replay isn't refetched next run

        self.after(0, update)

    def _replay_actions_locked(self) -> bool:
        """Play/Delete are blocked while we're mid-playback (worker busy) or while
        AoE4 is actually running (from the event-driven Steam-registry watch)."""
        return self._playing or self._game_running

    def _apply_action_lock(self) -> None:
        """Repaint the Play/Delete controls to match the current lock state."""
        if not hasattr(self, "downloads"):
            return
        with contextlib.suppress(tk.TclError):
            self.downloads._clear_hover()
            self.downloads.canvas.configure(cursor="arrow")
            self.downloads.refresh(immediate=True)
        if hasattr(self, "dl_delete_all_btn"):
            self.dl_delete_all_btn.configure(
                state="disabled" if self._replay_actions_locked() else "normal"
            )

    def _set_playing(self, playing: bool) -> None:
        """Keep replay action hitboxes and their visual state in sync."""
        if self._playing == playing:
            return
        self._playing = playing
        self._apply_action_lock()

    def _start_game_watch(self) -> None:
        """Reflect AoE4's open/closed state (launched by us or externally) on the
        Play/Delete controls, via an event-driven registry watch — no polling: the
        watcher thread sleeps until Steam flips the value, then calls back here."""
        self._game_running = launch.steam_app_running(self.cfg.app_id)  # initial state
        self._apply_action_lock()

        def on_change(running: bool) -> None:
            # Called from the watcher thread; hop to the UI thread to repaint.
            self.after(0, lambda r=running: self._on_game_state_change(r))

        with contextlib.suppress(Exception):
            self._game_watcher = launch.SteamRunWatcher(self.cfg.app_id, on_change)
            self._game_watcher.start()

    def _on_game_state_change(self, running: bool) -> None:
        if running == self._game_running:
            return
        self._game_running = running
        self._apply_action_lock()

    def _delete(self, path: Path) -> None:
        if self._replay_actions_locked():
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
        if self._game_running or launch.is_game_running():
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
        self._set_playing(True)  # block replay actions while Steam restarts
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
        self._set_playing(False)  # _continue_play / _start_play re-acquires it
        for btn in self._play_buttons:
            btn.configure(state="normal")
        self.notify.configure(text="")
        if err:
            self.status.configure(text="Ready.")
            self._info("Steam isn't ready yet", err)
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
        self._set_playing(True)
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
        self._cancel_visible = show
        if show:
            self._cancel_button.configure(state="normal")
            self._place_cancel_button()
        else:
            self._cancel_button.place_forget()
        self._render_toolbar_notice()

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
        shell = ctk.CTkFrame(parent, **theme.panel_style(strong=True))
        shell.pack(fill="both", expand=True, padx=24, pady=(8, 9))
        self._texture_frame(shell)
        self.saved_section_bar = self._section_bar(shell, "file", "Saved builds")
        container = ctk.CTkFrame(shell, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=14, pady=(0, 9))
        self._texture_frame(container)
        self.saved_total = ctk.CTkLabel(
            container, text="", text_color=GOLD_BRIGHT, font=self.font_meta
        )
        # Danger zone (packed first at the bottom so the list fills the space above it):
        # bulk deletes for reclaiming disk once old replays are gone from the server.
        # A top-only ornament avoids the old boxed bottom edge colliding with
        # the main splitter line. Its centred diamond shares the split grip's
        # horizontal axis, visually tying the two controls together.
        danger = ctk.CTkFrame(
            container,
            fg_color=CARD,
            corner_radius=0,
            border_width=0,
        )
        danger.pack(side="bottom", fill="x", pady=(14, 0))
        self._texture_frame(danger, "theme/surface-card.png")
        theme.OrnateRule(
            danger,
            bg=CARD,
            color=GOLD_DARK,
            highlight=GOLD_BRIGHT,
            height=16,
            center=True,
        ).pack(fill="x")
        danger_content = ctk.CTkFrame(
            danger, fg_color="transparent", corner_radius=0
        )
        danger_content.pack(fill="x")
        ctk.CTkLabel(
            danger_content,
            text="",
            image=self._asset_image("theme/icon-warning-danger.png", (24, 24)),
            width=30,
            height=34,
            fg_color="transparent",
        ).pack(side="left", padx=(10, 0), pady=7)
        ctk.CTkLabel(
            danger_content,
            text="DANGER ZONE",
            text_color=LOSS,
            font=self.font_bold,
        ).pack(side="left", padx=(4, 12), pady=10)
        self._reset_btn = ctk.CTkButton(
            danger_content, text="Factory reset", height=36, font=self.font_bold,
            command=self._factory_reset,
            **theme.button_style("danger"),
        )
        self._reset_btn.pack(side="right", padx=10, pady=8)
        self.saved_list = ctk.CTkScrollableFrame(
            container,
            fg_color=PANEL,
            corner_radius=0,
            scrollbar_button_color=GOLD_DARK,
            scrollbar_button_hover_color=GOLD,
        )
        self.saved_list.pack(fill="both", expand=True)
        self._texture_scrollable(self.saved_list)
        self.saved_list.bind(
            "<Configure>",
            lambda _event: self.after_idle(self._draw_saved_empty_message),
            add="+",
        )
        self._saved_build_count = 0
        self._set_scrollbar_visible(self.saved_list, False)
        theme.add_corner_ornaments(shell, bg=theme.PANEL_ALT)
        # Mask only the straight lower border of the Saved Builds shell. The
        # corner ornaments remain intact, while the shared splitter below is the
        # sole horizontal rule under Danger Zone.
        self.saved_bottom_border_mask = tk.Frame(
            shell,
            height=3,
            bg=theme.PANEL_ALT,
            bd=0,
            highlightthickness=0,
        )
        self.saved_bottom_border_mask.place(
            x=34,
            rely=1,
            y=0,
            relwidth=1,
            width=-68,
            anchor="sw",
        )
        self.saved_bottom_border_mask.lift()

    def _refresh_saved_builds(self) -> None:
        if not hasattr(self, "saved_list"):
            return
        self._clear_surface_children(self.saved_list)
        texture_canvas = getattr(self.saved_list, "_surface_texture_label", None)
        if texture_canvas is not None:
            texture_canvas.delete("saved_empty_message")
        saved = buildcache.load_saved(self.cfg) or set()
        self._saved_build_count = len(saved)
        if not saved:
            self.saved_total.configure(text="")
            self.saved_total.pack_forget()
            self._set_scrollbar_visible(self.saved_list, False)
            self.after_idle(self._draw_saved_empty_message)
            return
        if not self.saved_total.winfo_manager():
            # CTkScrollableFrame.pack() manages its outer container; the public
            # object is the inner scrolling frame and is therefore not itself a
            # valid Tk ``before`` sibling.
            saved_list_container = getattr(
                self.saved_list, "_parent_frame", self.saved_list
            )
            self.saved_total.pack(
                anchor="w", pady=(0, 8), before=saved_list_container
            )
        self._set_scrollbar_visible(
            self.saved_list,
            len(saved) > (4 if self._compact_layout else 7),
        )
        self._saved_size_labels = {}
        for build_id in sorted(saved, reverse=True):
            row = ctk.CTkFrame(
                self.saved_list,
                fg_color=CARD,
                corner_radius=3,
                border_width=1,
                border_color=theme.CARD_BORDER,
            )
            self._texture_frame(row, "theme/surface-card.png")
            row.pack(fill="x", padx=6, pady=3)
            ctk.CTkButton(
                row, text="Remove", width=90, height=34, font=self.font_bold,
                command=lambda b=build_id: self._delete_saved_build(b),
                **theme.button_style("danger"),
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

    def _draw_saved_empty_message(self) -> None:
        """Paint the empty-state copy on the texture with no opaque label box."""

        if getattr(self, "_saved_build_count", 0):
            return
        canvas = getattr(self.saved_list, "_surface_texture_label", None)
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("saved_empty_message")
        canvas.create_text(
            24,
            24,
            text=(
                "No saved builds yet.\n\n"
                "After watching an old replay you'll be asked if you want to keep "
                "its build for instant replays."
            ),
            fill=MUTED,
            font=(theme.FONT_BODY, 16),
            justify="left",
            anchor="nw",
            width=max(240, self.saved_list.winfo_width() - 80),
            tags="saved_empty_message",
        )

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

    def _delete_all_replays(self) -> None:
        if self._downloading or self._playing:
            self._info("Busy", "Finish the current download or replay first.")
            return
        folder = self.cfg.downloads_dir
        recs = list(folder.glob("*.rec")) if folder.is_dir() else []
        if not recs:
            self._info("No replays", "There are no downloaded replays to delete.")
            return
        n = len(recs)
        if not self._confirm(
            "Delete all replays?",
            f"Permanently delete all {n} downloaded replay{'s' if n != 1 else ''}?\n\n"
            "This can't be undone. Builds and your Steam sign-in are kept — only the "
            "replay files are removed.",
            ok="Delete all", danger=True,
        ):
            return
        with contextlib.suppress(Exception):
            service.delete_all_replays(self.cfg)
        self.refresh_downloads()

    def _factory_reset(self) -> None:
        if self._downloading or self._playing or self._searching or self._pg_searching:
            self._info(
                "Busy", "Finish the current download, search or replay before resetting."
            )
            return
        if not self._confirm(
            "Factory reset",
            "This permanently erases EVERYTHING and returns the app to its first-run "
            "state:\n\n"
            "•  All saved & cached builds (the entire build store)\n"
            "•  All downloaded replays\n"
            "•  Your Steam sign-in\n"
            "•  All downloaded tool files\n\n"
            "This cannot be undone — every build must be re-downloaded and you'll need "
            "to sign in to Steam again.",
            ok="Reset everything", danger=True,
        ):
            return
        # Deleting the store can take a while (tens of GB) -> off the UI thread.
        self._reset_btn.configure(state="disabled", text="Resetting…")
        threading.Thread(target=self._factory_reset_worker, daemon=True).start()

    def _factory_reset_worker(self) -> None:
        error = None
        try:
            service.factory_reset(self.cfg)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the UI
            error = str(exc)
        self.after(0, lambda: self._factory_reset_done(error))

    def _factory_reset_done(self, error: str | None) -> None:
        if error:
            with contextlib.suppress(Exception):
                self._reset_btn.configure(state="normal", text="⚠  Factory reset")
            self._info_cache.clear()  # its replays are gone; don't rewrite stale entries
            with contextlib.suppress(Exception):
                self.refresh_downloads()
                self._refresh_saved_builds()
            self._info("Factory reset", f"Some files could not be removed:\n{error}")
            return
        # Success -> relaunch so the app comes back up in a clean first-run state
        # (Steam sign-in prompt, empty lists) instead of asking the user to restart.
        with contextlib.suppress(Exception):
            self._reset_btn.configure(text="Restarting…")
        self.after(400, self._restart_app)

    def _restart_app(self) -> None:
        """Launch a fresh instance with the exact original command (dev ``-m`` run or
        the packaged exe), inheriting the current environment (e.g. AOE4REPLAY_ROOT),
        then close this window so only the clean instance remains."""
        args = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
        self._cancel_window_resize()
        _release_single_instance()  # drop the mutex first so the new instance isn't blocked
        with contextlib.suppress(Exception):
            subprocess.Popen(args)
        with contextlib.suppress(Exception):
            self.destroy()

    def _on_close(self) -> None:
        self._cancel_window_resize()
        if self._game_watcher is not None:  # wake the registry-watch thread so it exits
            with contextlib.suppress(Exception):
                self._game_watcher.stop()
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
        self._set_playing(False)
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


def _release_single_instance() -> None:
    """Close our named-mutex handle so a relaunch (after a factory reset) can take the
    lock immediately instead of racing our still-exiting process and being told the
    app is 'already running'."""
    global _single_instance_handle
    if _single_instance_handle is None:
        return
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
    _single_instance_handle = None


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
