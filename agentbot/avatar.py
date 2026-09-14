import colorsys
import random
from pathlib import Path

from PIL import Image

GRID = 24
SCALE = 8
FACE_CELLS = 4
CELL_SIZE = GRID // FACE_CELLS  # 6px per cell

BLACK = (17, 17, 17, 255)
WHITE = (255, 255, 255, 255)


def random_color() -> tuple[int, int, int, int]:
    h = random.random()
    s = random.uniform(0.55, 0.85)
    l = random.uniform(0.45, 0.65)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def random_face() -> list[list[bool]]:
    """4x4 grid with left-right symmetry. Left 2 columns are random,
    right 2 columns mirror them. 2*4 = 8 independent bits = 256 patterns.
    Excludes all-off and all-on."""
    while True:
        left = [[random.choice([True, False]) for _ in range(2)] for _ in range(FACE_CELLS)]
        grid = []
        for row in left:
            grid.append(row + list(reversed(row)))
        on_count = sum(c for row in grid for c in row)
        if 0 < on_count < FACE_CELLS * FACE_CELLS:
            return grid


def make_avatar(out_path: Path, color: tuple = None, face: list = None):
    if color is None:
        color = random_color()
    if face is None:
        face = random_face()

    img = Image.new("RGBA", (GRID, GRID), color)
    fg = WHITE if _is_dark(color) else BLACK
    for row in range(FACE_CELLS):
        for col in range(FACE_CELLS):
            if face[row][col]:
                x0 = col * CELL_SIZE
                y0 = row * CELL_SIZE
                for y in range(y0, y0 + CELL_SIZE):
                    for x in range(x0, x0 + CELL_SIZE):
                        img.putpixel((x, y), fg)

    img.resize((GRID * SCALE, GRID * SCALE), Image.NEAREST).save(out_path)
    return color, face


def _is_dark(color: tuple) -> bool:
    r, g, b = color[:3]
    return (r * 299 + g * 587 + b * 114) / 1000 < 128
