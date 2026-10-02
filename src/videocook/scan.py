"""Cheap full-title scan: per-frame statistics computed inside VapourSynth.

Every frame is downscaled (default 960 px wide, ~540p) to 16-bit luma and
measured with multithreaded VS filters; Python only collects frame props.

Per-frame metrics (all luma, 16-bit limited-range code values):
    luma, luma_min, luma_max  mean / min / max, normalised 0..1 over full scale
    flat       share of pixels whose Sobel magnitude is below FLAT
    dark_flat  share of pixels that are dark *and* flat (banding risk)
    edge       share of pixels whose Sobel magnitude is above EDGE
    noise      mean |Y - blur(Y)| over flat pixels, in 8-bit code values
    oor        share of pixels outside limited range (luma overflow/underflow)
    motion     mean absolute difference to the previous frame (0..1)
    scene      1 where SCDetect marks a scene change before this frame
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import vapoursynth as vs

from videocook.vsource import open_source

core = vs.core

# Thresholds on a 16-bit Sobel magnitude at scan resolution (step d -> ~4d).
FLAT = 512      # gentle gradients: slope below ~0.25 8-bit codes per pixel
EDGE = 20480    # strong edges: steps above ~20 8-bit codes
DARK = 18112    # 25% of the limited range above black (~8-bit code 70)
BLACK, WHITE = 4096, 60160

METRICS = ("luma", "luma_min", "luma_max", "flat", "dark_flat", "edge", "noise", "oor",
           "motion", "scene")


@dataclass
class ScanResult:
    fps: float
    num_frames: int
    width: int
    height: int
    data: dict[str, np.ndarray]

    def save(self, path: Path) -> None:
        np.savez_compressed(path, fps=self.fps, width=self.width, height=self.height, **self.data)

    @classmethod
    def load(cls, path: Path) -> "ScanResult":
        z = np.load(path)
        data = {k: z[k] for k in METRICS}
        return cls(float(z["fps"]), len(data["luma"]), int(z["width"]), int(z["height"]), data)


def build_stats_clip(src: vs.VideoNode, scan_width: int = 960) -> vs.VideoNode:
    h = round(src.height * scan_width / src.width / 2) * 2
    y = core.resize.Bilinear(src, scan_width, h, format=vs.GRAY16)
    sob = core.std.Sobel(y)
    blur = core.std.BoxBlur(y, hradius=1, vradius=1)
    prev = y[0] + y[:-1]

    def stats(clip: vs.VideoNode, prop: str, ref: vs.VideoNode | None = None) -> vs.VideoNode:
        return core.std.PlaneStats(clip, ref, prop=prop)

    out = stats(y, "L", prev)  # LAverage, LMin, LMax, LDiff
    flat = core.akarin.Expr(sob, f"x {FLAT} < 65535 0 ?")
    dark_flat = core.akarin.Expr([y, sob], f"x {DARK} < y {FLAT} < and 65535 0 ?")
    edge = core.akarin.Expr(sob, f"x {EDGE} > 65535 0 ?")
    noise = core.akarin.Expr([y, blur, sob], f"z {FLAT} < x y - abs 0 ?")
    oor = core.akarin.Expr(y, f"x {BLACK} < x {WHITE} > or 65535 0 ?")
    for clip, prop in ((flat, "F"), (dark_flat, "DF"), (edge, "E"), (noise, "N"), (oor, "O")):
        out = core.std.CopyFrameProps(out, stats(clip, prop), props=[f"{prop}Average"])
    sc = core.misc.SCDetect(y, threshold=0.14)
    out = core.std.CopyFrameProps(out, sc, props=["_SceneChangePrev"])
    return out


def run_scan(spec: dict, cache_dir: Path, scan_width: int = 960, hw: bool = True,
             frame_range: tuple[int, int] | None = None, progress: bool = True) -> ScanResult:
    core.num_threads = max(core.num_threads, 8)
    src = open_source(spec, cache_dir, hw=hw)
    if frame_range:
        src = src[frame_range[0] : frame_range[1]]
    clip = build_stats_clip(src, scan_width)
    n = clip.num_frames
    arr = {k: np.zeros(n, dtype=np.float32) for k in METRICS}
    t0 = time.perf_counter()
    for i, f in enumerate(clip.frames(close=True)):
        p = f.props
        arr["luma"][i] = p["LAverage"]
        arr["luma_min"][i] = p["LMin"] / 65535
        arr["luma_max"][i] = p["LMax"] / 65535
        arr["motion"][i] = p["LDiff"] if i else 0.0
        arr["flat"][i] = p["FAverage"]
        arr["dark_flat"][i] = p["DFAverage"]
        arr["edge"][i] = p["EAverage"]
        arr["oor"][i] = p["OAverage"]
        fa = p["FAverage"]
        arr["noise"][i] = (p["NAverage"] * 65535 / 256) / fa if fa > 0.01 else 0.0
        arr["scene"][i] = p.get("_SceneChangePrev", 0)
        if progress and i % 2000 == 0 and i:
            fps = i / (time.perf_counter() - t0)
            print(f"  scan {i}/{n} frames, {fps:.0f} fps, eta {(n - i) / fps / 60:.1f} min", flush=True)
    fps = float(src.fps) if src.fps.denominator else 24000 / 1001
    return ScanResult(fps, n, src.width, src.height, arr)


def detect_crop(spec: dict, cache_dir: Path, samples: int = 40) -> dict:
    """Black borders: rows/columns that stay near black in every sampled frame."""
    src = open_source(spec, cache_dir, hw=True)
    y = core.resize.Point(src, format=vs.GRAY16)
    n = y.num_frames
    idx = np.linspace(n * 0.05, n * 0.95, samples).astype(int)
    row_max = col_max = None
    for i in idx:
        plane = np.asarray(y.get_frame(int(i))[0], dtype=np.float32)
        r, c = plane.mean(axis=1), plane.mean(axis=0)
        row_max = r if row_max is None else np.maximum(row_max, r)
        col_max = c if col_max is None else np.maximum(col_max, c)
    thr = BLACK + 6 * 256  # ~6 codes above black

    def edges(v: np.ndarray) -> tuple[int, int]:
        live = np.nonzero(v > thr)[0]
        if not len(live):
            return 0, 0
        return int(live[0]), int(len(v) - 1 - live[-1])

    top, bottom = edges(row_max)
    left, right = edges(col_max)
    return {"top": top, "bottom": bottom, "left": left, "right": right,
            "active": [src.width - left - right, src.height - top - bottom]}


def shots(scan: ScanResult, min_len: int = 6) -> list[tuple[int, int]]:
    """Split the title at scene changes; merge fragments shorter than min_len."""
    cuts = [0] + [int(i) for i in np.nonzero(scan.data["scene"] > 0)[0] if i > 0] + [scan.num_frames]
    out: list[tuple[int, int]] = []
    for a, b in zip(cuts, cuts[1:]):
        if out and b - a < min_len:
            out[-1] = (out[-1][0], b)
        elif b > a:
            out.append((a, b))
    return out


def save_json(obj: dict, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
