"""Regenerate the application icon from assets/logo.png.

Writes two copies of the same multi-size .ico:
  * packaging/app.ico        -> embedded as the .exe icon by PyInstaller
  * src/aoe4replay/assets/app.ico -> the running window's title-bar/taskbar icon

Run from the repo root: .venv\\Scripts\\python packaging/_make_icon.py
"""

from pathlib import Path

from PIL import Image

src = Path("src/aoe4replay/assets/logo.png")
outputs = [Path("packaging/app.ico"), Path("src/aoe4replay/assets/app.ico")]

img = Image.open(src).convert("RGBA")
w, h = img.size
side = max(w, h)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(img, ((side - w) // 2, (side - h) // 2), img)
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
for out in outputs:
    canvas.save(out, format="ICO", sizes=sizes)
    print("wrote", out)
print("from source", img.size)
