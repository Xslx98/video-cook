"""Frame rendering for human/Claude review: full frames, zoomed crops, sheets, triptychs."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import vapoursynth as vs
from PIL import Image, ImageDraw, ImageFont

core = vs.core

MATRIX = {"bt709": "709", "bt470bg": "470bg", "smpte170m": "170m", "bt2020nc": "2020ncl"}
TRANSFER = {"bt709": "709", "smpte2084": "st2084", "arib-std-b67": "std-b67"}
PRIMARIES = {"bt709": "709", "bt2020": "2020", "smpte170m": "170m", "bt470bg": "470bg"}


def to_rgb(clip: vs.VideoNode, colour: dict) -> vs.VideoNode:
    """RGB24 preview of a YUV clip; PQ/HLG sources are tone-mapped to BT.709 SDR."""
    matrix = MATRIX.get(colour.get("matrix") or "", "709" if clip.height >= 720 else "170m")
    transfer = colour.get("transfer")
    if transfer in ("smpte2084", "arib-std-b67"):
        lin = core.resize.Bicubic(
            clip, format=vs.RGBS, matrix_in_s=matrix, range_in_s="limited",
            transfer_in_s=TRANSFER[transfer], transfer_s="linear",
            primaries_in_s=PRIMARIES.get(colour.get("primaries") or "", "2020"), primaries_s="709",
            nominal_luminance=203,
        )
        # Hable/Uncharted-2 filmic curve with white point at ~12x reference white.
        hable = "x 0.15 * 0.05 + x * 0.004 + x 0.15 * 0.5 + x * 0.06 + / 0.0667 -"
        w = 12.0
        wscale = (lambda v: ((v * (0.15 * v + 0.05) + 0.004) / (v * (0.15 * v + 0.5) + 0.06)) - 0.0667)(w)
        tm = core.akarin.Expr(lin, f"x 0 max 2 * X! X@ {hable.replace('x', 'X@')} {wscale} / 0 max 1 min")
        return core.resize.Bicubic(tm, format=vs.RGB24, transfer_in_s="linear", transfer_s="709",
                                   dither_type="error_diffusion")
    return core.resize.Bicubic(clip, format=vs.RGB24, matrix_in_s=matrix, range_in_s="limited",
                               dither_type="error_diffusion")


def frame_array(rgb: vs.VideoNode, n: int) -> np.ndarray:
    f = rgb.get_frame(n)
    return np.dstack([np.asarray(f[p]) for p in range(3)])


def luma_array(clip: vs.VideoNode, n: int) -> np.ndarray:
    y = core.resize.Point(clip, format=vs.GRAY16)
    return np.asarray(y.get_frame(n)[0], dtype=np.float32)


def choose_crop(luma: np.ndarray, bucket: str, cw: int, ch: int) -> tuple[int, int]:
    """Top-left corner of the crop that best shows the bucket's risk (16-bit limited luma)."""
    h, w = luma.shape
    gy, gx = np.gradient(luma)
    grad = np.hypot(gx, gy)
    black, dark = 4096, 18112
    best, best_xy = -np.inf, ((w - cw) // 2, (h - ch) // 2)
    for y0 in np.linspace(0, h - ch, 7).astype(int):
        for x0 in np.linspace(0, w - cw, 9).astype(int):
            g = grad[y0:y0 + ch, x0:x0 + cw]
            lum = luma[y0:y0 + ch, x0:x0 + cw]
            if bucket in ("dark_flat", "bright_gradient"):
                # dark-but-not-crushed (or bright) pixels on gentle gradients
                band = ((lum > black + 3 * 256) & (lum < dark)) if bucket == "dark_flat" else lum >= dark
                smooth = ((g < 256) & band).mean()
                # a gradient spans several code values; uniform patches are not interesting
                spread = min(1.0, (np.percentile(lum, 90) - np.percentile(lum, 10)) / (8 * 256))
                score = smooth * (0.3 + spread)
            elif bucket == "dark_noisy":
                score = g.mean() * ((lum > black + 3 * 256) & (lum < 20000)).mean()
            else:  # detail / motion / static / normal: busiest region
                score = np.percentile(g, 90)
            if score > best:
                best, best_xy = score, (int(x0), int(y0))
    return best_xy


def boost_dark(arr: np.ndarray, ceiling: int = 80) -> np.ndarray:
    """Stretch 0..ceiling (8-bit RGB) to the full range so dark banding becomes visible."""
    return np.clip(arr.astype(np.float32) * (255.0 / ceiling), 0, 255).astype(np.uint8)


def save_png(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path, optimize=True)


def crop_zoom(arr: np.ndarray, x: int, y: int, cw: int, ch: int, zoom: int) -> np.ndarray:
    c = arr[y:y + ch, x:x + cw]
    return c.repeat(zoom, axis=0).repeat(zoom, axis=1) if zoom > 1 else c


def _font(size: int = 20) -> ImageFont.ImageFont:
    for name in ("consola.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def label(arr: np.ndarray, text: str) -> np.ndarray:
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    f = _font(max(14, arr.shape[1] // 60))
    box = d.textbbox((8, 6), text, font=f)
    d.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
    d.text((8, 6), text, fill=(255, 255, 0), font=f)
    return np.asarray(img)


def contact_sheet(tiles: list[np.ndarray], cols: int = 4, width: int = 480) -> np.ndarray:
    th = []
    for t in tiles:
        img = Image.fromarray(t)
        h = round(img.height * width / img.width)
        th.append(np.asarray(img.resize((width, h), Image.LANCZOS)))
    rows = (len(th) + cols - 1) // cols
    h = max(t.shape[0] for t in th)
    sheet = np.zeros((rows * h, cols * width, 3), dtype=np.uint8)
    for i, t in enumerate(th):
        r, c = divmod(i, cols)
        sheet[r * h:r * h + t.shape[0], c * width:(c + 1) * width] = t
    return sheet


def amplified_diff(a: np.ndarray, b: np.ndarray, gain: float = 8.0) -> np.ndarray:
    d = (a.astype(np.int16) - b.astype(np.int16)) * gain + 128
    return np.clip(d, 0, 255).astype(np.uint8)


def hstack(*arrs: np.ndarray, gap: int = 6) -> np.ndarray:
    h = max(a.shape[0] for a in arrs)
    parts = []
    for i, a in enumerate(arrs):
        if a.shape[0] < h:
            a = np.vstack([a, np.zeros((h - a.shape[0], a.shape[1], 3), np.uint8)])
        parts.append(a)
        if i < len(arrs) - 1:
            parts.append(np.full((h, gap, 3), 40, np.uint8))
    return np.hstack(parts)


def vstack(*arrs: np.ndarray, gap: int = 6) -> np.ndarray:
    w = max(a.shape[1] for a in arrs)
    parts = []
    for i, a in enumerate(arrs):
        if a.shape[1] < w:
            a = np.hstack([a, np.zeros((a.shape[0], w - a.shape[1], 3), np.uint8)])
        parts.append(a)
        if i < len(arrs) - 1:
            parts.append(np.full((gap, w, 3), 40, np.uint8))
    return np.vstack(parts)
