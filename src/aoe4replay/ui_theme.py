"""AoE4-inspired visual tokens and lightweight Tk widgets.

This module is deliberately presentation-only.  It does not import application
services and owns no state beyond colours, typography, spacing and decorative
canvas drawing used by :mod:`aoe4replay.panel`.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

# Core surfaces tuned to the launcher's night-navy artwork.
BG = "#000b18"
BG_RAISED = "#04111e"
PANEL = "#000c1a"
PANEL_ALT = "#020f1f"
CARD = "#031322"
CARD_HOVER = "#061b2f"
CARD_BORDER = "#101d28"

# AoE4 metalwork
GOLD = "#d3a149"
GOLD_BRIGHT = "#edc064"
GOLD_DARK = "#6a4726"
GOLD_FAINT = "#30241a"
BRONZE = "#5f3f24"

# Actions
ACCENT = "#012a61"
ACCENT_HOVER = "#063b78"
DANGER = "#571713"
DANGER_HOVER = "#762018"
NEUTRAL = "#010d19"
NEUTRAL_HOVER = "#072038"
PLAY_GREEN = "#113218"
PLAY_GREEN_HOVER = "#184a24"

# Semantic text
GREEN = "#72d962"
LOSS = "#f56257"
TEXT = "#f0e7d8"
TEXT_SOFT = "#d3c4af"
MUTED = "#a4937e"
DIVIDER = "#4a321c"

# Fonts available on supported Windows installs. Tk falls back per glyph for
# player names that use writing systems not covered by these serif families.
FONT_DISPLAY = "Palatino Linotype"
FONT_BODY = "Palatino Linotype"
FONT_SYMBOL = "Segoe UI Symbol"


def font(size: int, *, weight: str = "normal", family: str = FONT_BODY) -> ctk.CTkFont:
    """Return a consistently configured theme font."""

    return ctk.CTkFont(family=family, size=size, weight=weight)


def player_font(text: str, size: int = 17, *, weight: str = "bold") -> ctk.CTkFont:
    """Use a local Windows font with CJK coverage when a player name needs it."""

    codepoints = (ord(char) for char in text)
    has_cjk = any(
        0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0xF900 <= point <= 0xFAFF
        for point in codepoints
    )
    family = "Microsoft YaHei UI" if has_cjk else FONT_BODY
    return font(size, weight=weight, family=family)


def button_style(kind: str = "primary") -> dict[str, object]:
    """Shared CTkButton options for a visual action role."""

    colors = {
        "primary": (ACCENT, ACCENT_HOVER, GOLD_DARK),
        "danger": (DANGER, DANGER_HOVER, "#8f3a2e"),
        "success": (PLAY_GREEN, PLAY_GREEN_HOVER, "#397541"),
        "neutral": (NEUTRAL, NEUTRAL_HOVER, GOLD_DARK),
        "gold": (NEUTRAL, NEUTRAL_HOVER, GOLD_DARK),
    }
    fg, hover, border = colors[kind]
    style = {
        "fg_color": fg,
        "hover_color": hover,
        "border_color": border,
        "border_width": 1,
        "text_color": GOLD_BRIGHT if kind == "gold" else TEXT,
        "corner_radius": 3,
    }
    if kind == "gold":
        style["text_color_disabled"] = GOLD
    return style


def entry_style() -> dict[str, object]:
    """Shared CTkEntry options."""

    return {
        "fg_color": "#010f1e",
        "border_color": GOLD_DARK,
        "border_width": 1,
        "text_color": TEXT,
        "placeholder_text_color": MUTED,
        "corner_radius": 3,
    }


def panel_style(*, strong: bool = False) -> dict[str, object]:
    """Shared bordered-panel options."""

    return {
        "fg_color": PANEL_ALT if strong else PANEL,
        "border_color": BRONZE if strong else GOLD_DARK,
        "border_width": 1,
        "corner_radius": 3,
    }


class OrnateRule(tk.Canvas):
    """Responsive double-line divider with an AoE4-style centre diamond."""

    def __init__(
        self,
        master,
        *,
        bg: str = PANEL,
        color: str = GOLD_DARK,
        highlight: str = GOLD,
        height: int = 16,
        center: bool = True,
    ) -> None:
        super().__init__(
            master,
            height=height,
            bg=bg,
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
        )
        self._line = color
        self._highlight = highlight
        self._center = center
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event=None) -> None:
        self.delete("ornament")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        mid_y = height // 2
        if not self._center or width < 48:
            self.create_line(0, mid_y, width, mid_y, fill=self._line, tags="ornament")
            return
        mid_x = width // 2
        gap = 13
        self.create_line(0, mid_y - 2, mid_x - gap, mid_y - 2, fill=self._line, tags="ornament")
        self.create_line(mid_x + gap, mid_y - 2, width, mid_y - 2, fill=self._line, tags="ornament")
        self.create_line(0, mid_y + 2, mid_x - gap - 5, mid_y + 2, fill=GOLD_FAINT, tags="ornament")
        self.create_line(
            mid_x + gap + 5,
            mid_y + 2,
            width,
            mid_y + 2,
            fill=GOLD_FAINT,
            tags="ornament",
        )
        radius = 6
        diamond = (
            mid_x,
            mid_y - radius,
            mid_x + radius,
            mid_y,
            mid_x,
            mid_y + radius,
            mid_x - radius,
            mid_y,
        )
        self.create_polygon(
            diamond,
            fill=BG_RAISED,
            outline=self._line,
            width=1,
            tags="ornament",
        )
        inner = 2
        self.create_polygon(
            mid_x,
            mid_y - inner,
            mid_x + inner,
            mid_y,
            mid_x,
            mid_y + inner,
            mid_x - inner,
            mid_y,
            fill=self._highlight,
            outline="",
            tags="ornament",
        )


class TabRule(tk.Canvas):
    """Two-line navigation underline with a centre lozenge."""

    def __init__(self, master, *, bg: str = BG) -> None:
        super().__init__(
            master,
            height=16,
            bg=bg,
            bd=0,
            highlightthickness=0,
        )
        self._active = False
        self.bind("<Configure>", self._redraw, add="+")

    def set_active(self, active: bool) -> None:
        self._active = active
        self._redraw()

    def _redraw(self, _event=None) -> None:
        self.delete("tab_rule")
        if not self._active:
            return
        width = max(1, self.winfo_width())
        center = width // 2
        gap = 9
        self.create_line(
            0, 9, center - gap, 9, fill=GOLD, width=1, tags="tab_rule"
        )
        self.create_line(
            center + gap, 9, width, 9, fill=GOLD, width=1, tags="tab_rule"
        )
        self.create_line(
            5, 13, center - gap - 4, 13,
            fill=GOLD_DARK,
            width=1,
            tags="tab_rule",
        )
        self.create_line(
            center + gap + 4, 13, width - 5, 13,
            fill=GOLD_DARK,
            width=1,
            tags="tab_rule",
        )
        self.create_polygon(
            center, 2,
            center + 7, 9,
            center, 16,
            center - 7, 9,
            fill=BG,
            outline=GOLD,
            width=1,
            tags="tab_rule",
        )
        self.create_polygon(
            center, 6,
            center + 3, 9,
            center, 12,
            center - 3, 9,
            fill=GOLD_BRIGHT,
            outline="",
            tags="tab_rule",
        )


class PanelBottomRule(OrnateRule):
    """Thin inset linework used at the bottom of the filter panel."""

    def __init__(self, master, *, bg: str = PANEL_ALT) -> None:
        super().__init__(
            master,
            bg=bg,
            color=GOLD_DARK,
            highlight=GOLD_BRIGHT,
            height=14,
            center=True,
        )


class CornerOrnament(tk.Canvas):
    """Small line-work corner used to finish framed sections."""

    def __init__(self, master, *, bg: str = PANEL, flip_x: bool = False, flip_y: bool = False):
        super().__init__(
            master,
            width=30,
            height=30,
            bg=bg,
            bd=0,
            highlightthickness=0,
        )
        points = [(2, 27), (2, 9), (6, 9), (6, 6), (9, 6), (9, 2), (27, 2)]
        accent = [(5, 27), (5, 13), (9, 13), (9, 9), (13, 9), (13, 5), (27, 5)]
        inset = [(8, 24), (8, 17), (12, 17), (12, 12), (17, 12), (17, 8), (24, 8)]
        if flip_x:
            points = [(29 - x, y) for x, y in points]
            accent = [(29 - x, y) for x, y in accent]
            inset = [(29 - x, y) for x, y in inset]
        if flip_y:
            points = [(x, 29 - y) for x, y in points]
            accent = [(x, 29 - y) for x, y in accent]
            inset = [(x, 29 - y) for x, y in inset]
        self.create_line(*sum(points, ()), fill=GOLD_DARK, width=1)
        self.create_line(*sum(accent, ()), fill=BRONZE, width=1)
        self.create_line(*sum(inset, ()), fill=GOLD_FAINT, width=1)


def add_corner_ornaments(frame, *, bg: str = PANEL) -> tuple[CornerOrnament, ...]:
    """Place four non-interactive decorative corners on a framed widget."""

    corners = (
        (False, False, {"x": 1, "y": 1}),
        (True, False, {"relx": 1, "x": -1, "y": 1, "anchor": "ne"}),
        (False, True, {"x": 1, "rely": 1, "y": -1, "anchor": "sw"}),
        (True, True, {"relx": 1, "rely": 1, "x": -1, "y": -1, "anchor": "se"}),
    )
    widgets = []
    for flip_x, flip_y, place in corners:
        ornament = CornerOrnament(frame, bg=bg, flip_x=flip_x, flip_y=flip_y)
        ornament.place(**place)
        widgets.append(ornament)
    return tuple(widgets)
