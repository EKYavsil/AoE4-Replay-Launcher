"""Reconstruct and render the QR code DepotDownloader prints for ``-qr`` login.

DepotDownloader (via QRCoder) prints the Steam sign-in QR challenge to the
console as a block of characters and *never* prints the raw challenge URL. So we
rebuild the module matrix straight from that block and rasterise it ourselves
into a crisp image the panel can show.

Two things make the block awkward to read, both handled here:

* The dark module is emitted in the console's OEM code page (not UTF-8), so we
  decide dark-vs-light purely structurally — a cell is dark iff its byte is not
  a space — which is independent of whatever code page Steam/DepotDownloader use.
* Each module is two characters wide but only one text row tall, so the matrix
  is downsampled horizontally to square modules before rendering.
"""

from __future__ import annotations

_SPACE = 0x20
_CR = 0x0D
_LF = 0x0A


def _qr_line_bytes(raw: bytes) -> bytes | None:
    """Return the trimmed bytes if ``raw`` is a QR row, else ``None``.

    A QR row contains only spaces and at most one distinct other byte (the dark
    block glyph). Ordinary log lines have many distinct bytes and are rejected.
    """
    clean = raw.rstrip(bytes((_CR, _LF)))
    distinct = set(clean) - {_SPACE}
    if len(distinct) > 1:
        return None
    return clean


def block_to_matrix(lines: list[bytes]) -> list[list[bool]]:
    """Turn collected QR rows into a square ``True=dark`` module matrix.

    The block is square in modules and rendered one row per module, so the
    horizontal characters-per-module is ``width / rows`` (2 for DepotDownloader).
    """
    rows = [ln for ln in lines if ln is not None]
    if not rows:
        return []
    width = max(len(ln) for ln in rows)
    height = len(rows)
    cpm = max(1, round(width / height))  # characters per module, horizontally
    modules = width // cpm
    matrix: list[list[bool]] = []
    for ln in rows:
        out_row: list[bool] = []
        for m in range(modules):
            cell = ln[m * cpm : (m + 1) * cpm]
            out_row.append(any(b != _SPACE for b in cell))
        matrix.append(out_row)
    return matrix


def matrix_to_ascii(matrix: list[list[bool]]) -> str:
    """Render the matrix back to ``##``/``  `` pairs (for tests/inspection)."""
    return "\n".join("".join("##" if d else "  " for d in row) for row in matrix)


def matrix_to_terminal(matrix: list[list[bool]], quiet: int = 4) -> str:
    """Render the matrix as a scannable QR for an ANSI terminal.

    Two module rows are packed into one text row with the upper-half-block glyph
    (``▀``), and every module's colour is set explicitly (dark = black, light =
    white) via ANSI fg/bg, so the code is always dark-on-light regardless of the
    terminal's own theme, square, and compact enough to scan with a phone. A
    light quiet zone is added around it. ``""`` if the matrix is empty.
    """
    if not matrix:
        return ""
    width = max(len(r) for r in matrix)
    total_w = width + 2 * quiet

    def _pad(row: list[bool]) -> list[bool]:
        body = [bool(c) for c in row] + [False] * (width - len(row))
        return [False] * quiet + body + [False] * quiet

    blank = [False] * total_w
    grid = [list(blank) for _ in range(quiet)] + [_pad(r) for r in matrix]
    grid += [list(blank) for _ in range(quiet)]
    if len(grid) % 2:
        grid.append(list(blank))

    lines: list[str] = []
    for r in range(0, len(grid), 2):
        top, bot = grid[r], grid[r + 1]
        cells = []
        for x in range(total_w):
            fg = 30 if top[x] else 97  # dark module -> black, light -> white
            bg = 40 if bot[x] else 107
            cells.append(f"\x1b[{fg};{bg}m▀")
        lines.append("".join(cells) + "\x1b[0m")
    return "\n".join(lines)


def render_image(matrix: list[list[bool]], scale: int = 8):
    """Rasterise the (quiet-zone-inclusive) matrix into a crisp PIL image."""
    from PIL import Image

    height = len(matrix)
    width = len(matrix[0]) if height else 0
    img = Image.new("1", (width, height), 1)  # 1 = white
    px = img.load()
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                px[c, r] = 0
    return img.resize((width * scale, height * scale), Image.NEAREST)


class QrAssembler:
    """Feed console lines; emit a finished QR matrix once a full block arrives.

    DepotDownloader prints the QR after ``...sign in with this QR code:`` and
    reprints it after ``The QR code has changed:``. The block is square, so once
    we know the row width we know how many rows to expect; the program then goes
    silent (polling), so a row count — not a trailing marker — terminates it.
    """

    def __init__(self) -> None:
        self._collecting = False
        self._rows: list[bytes] = []
        self._expected: int | None = None

    def _start(self) -> None:
        self._collecting = True
        self._rows = []
        self._expected = None

    def feed_line(self, raw: bytes) -> list[list[bool]] | None:
        text = raw.decode("ascii", "ignore")
        low = text.lower()
        if "qr code:" in low or "qr code has changed" in low:
            self._start()
            return None
        if not self._collecting:
            return None

        row = _qr_line_bytes(raw)
        if row is None:
            # A real log line interrupts: emit what we have if it is usable.
            return self._finish_if_ready(force=True)

        self._rows.append(row)
        if self._expected is None and (set(row) - {_SPACE}):
            # First row with dark modules fixes the width, hence the (square)
            # height: modules are two chars wide, so height == width / 2.
            self._expected = max(len(row) // 2, 1)
        if self._expected is not None and len(self._rows) >= self._expected:
            return self._finish_if_ready()
        return None

    def _finish_if_ready(self, force: bool = False) -> list[list[bool]] | None:
        if not self._rows or not any(set(r) - {_SPACE} for r in self._rows):
            if force:
                self._collecting = False
            return None
        self._collecting = False
        return block_to_matrix(self._rows)
