"""Assessment stage: scan → buckets → sampled frames → report for Claude and the user."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from videocook import images, sampling
from videocook.scan import ScanResult, detect_crop, run_scan
from videocook.vsource import open_source

BUCKET_COLOURS = {
    "dark_flat": "#3b4cc0", "dark_noisy": "#7b3294", "bright_gradient": "#f4a582",
    "high_detail": "#1a9850", "high_motion": "#d73027", "static": "#999999", "normal": "#dddddd",
    "blank": "#000000",
}


def timeline_png(scan: ScanResult, result: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = scan.data
    t = np.arange(scan.num_frames) / scan.fps / 60
    k = max(1, scan.num_frames // 4000)  # decimate for plotting

    def sm(v: np.ndarray) -> np.ndarray:
        w = max(1, int(scan.fps))
        return np.convolve(v, np.ones(w) / w, mode="same")[::k]

    fig, ax = plt.subplots(6, 1, figsize=(14, 10), sharex=True,
                           gridspec_kw={"height_ratios": [1, 1, 1, 1, 1, 0.4]})
    series = [
        ("luma (0-1)", sampling._norm_luma(d["luma"])),
        ("dark-flat share", d["dark_flat"]),
        ("edge density", d["edge"]),
        ("noise (8-bit codes)", d["noise"]),
        ("motion", d["motion"]),
    ]
    for a, (name, v) in zip(ax, series):
        a.plot(t[::k], sm(v), lw=0.7, color="#333")
        a.set_ylabel(name, fontsize=8)
        a.tick_params(labelsize=7)
    for s in result["shot_list"]:
        ax[5].axvspan(s["start"] / scan.fps / 60, s["end"] / scan.fps / 60,
                      color=BUCKET_COLOURS[s["bucket"]], lw=0)
    for p in result["picks"]:
        for a in ax[:5]:
            a.axvline(p["time"] / 60, color=BUCKET_COLOURS[p["bucket"]], lw=0.8, alpha=0.8)
    ax[5].set_yticks([])
    ax[5].set_xlabel("minutes")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in BUCKET_COLOURS.values()]
    fig.legend(handles, list(BUCKET_COLOURS), loc="lower center", ncol=8, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=90)
    plt.close(fig)


def render_samples(spec: dict, cache_dir: Path, colour: dict, picks: list[dict], out: Path,
                   frame_offset: int = 0) -> list[dict]:
    clip = open_source(spec, cache_dir, hw=False)
    rgb = images.to_rgb(clip, colour)
    cw, ch = (960, 540) if clip.width > 2000 else (480, 270)
    zoom = 1 if clip.width > 2000 else 2
    tiles = []
    for i, p in enumerate(picks, 1):
        n = p["frame"] + frame_offset
        arr = images.frame_array(rgb, n)
        lum = images.luma_array(clip, n)
        x, y = images.choose_crop(lum, p["bucket"], cw, ch)
        tag = f"{i:02d}_{p['bucket']}_f{n}"
        full = out / "frames" / f"{tag}.png"
        crop = out / "crops" / f"{tag}_x{x}y{y}.png"
        images.save_png(arr, full)
        crop_arr = images.crop_zoom(arr, x, y, cw, ch, zoom)
        images.save_png(crop_arr, crop)
        p.update({"image": str(full), "crop": str(crop), "crop_box": [x, y, cw, ch], "zoom": zoom})
        if p["bucket"] in ("dark_flat", "dark_noisy"):
            boosted = crop.with_name(crop.stem + "_boost.png")
            images.save_png(images.boost_dark(crop_arr), boosted)
            p["crop_boost"] = str(boosted)
        m, s = divmod(p["time"], 60)
        tiles.append(images.label(arr, f"#{i} {p['bucket']} f{n} {int(m)}:{s:04.1f}"))
    images.save_png(images.contact_sheet(tiles), out / "sheet.png")
    return picks


def report_md(probe_md: str | None, result: dict, crop: dict | None) -> str:
    g = result["global"]
    dur = result["duration"]
    lines = ["# Assessment report", "",
             f"- frames {result['frames']} @ {result['fps']:.3f} fps ({dur / 60:.1f} min), "
             f"{result['shots']} shots",
             f"- dark share {g['dark_share']:.1%}, mean luma {g['mean_luma']:.2f}, "
             f"median noise {g['median_noise']:.2f} codes, median edge density {g['median_edge']:.3f}",
             f"- frames with >1% out-of-range luma: {g['oor_frames_share']:.1%}; "
             f"near-duplicate frames: {g['near_duplicate_frames_share']:.1%}"]
    if crop:
        lines.append(f"- black borders: top {crop['top']}, bottom {crop['bottom']}, left {crop['left']}, "
                     f"right {crop['right']} → active {crop['active'][0]}x{crop['active'][1]}")
    lines += ["", "## Frame composition", "", "| bucket | share | shots | sampled | exposes |",
              "|---|---|---|---|---|"]
    for b, c in result["composition"].items():
        lines.append(f"| {b} | {c['share']:.1%} | {c['shots']} | {result['allocation'].get(b, 0)} | "
                     f"{sampling.BUCKETS[b][1]} |")
    lines += ["", "## Sampled frames", "", "| # | frame | time | bucket | role | luma | dark-flat | edge | noise | motion |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(result["picks"], 1):
        m = p["metrics"]
        mm, ss = divmod(p["time"], 60)
        lines.append(f"| {i} | {p['frame']} | {int(mm)}:{ss:04.1f} | {p['bucket']} | {p['role']} | "
                     f"{m['luma']:.2f} | {m['dark_flat']:.2f} | {m['edge']:.3f} | {m['noise']:.2f} | {m['motion']:.4f} |")
    lines += ["", "Images: `assess/sheet.png` (overview), `assess/frames/*.png` (full), "
              "`assess/crops/*.png` (zoomed risk regions), `assess/timeline.png`."]
    if probe_md:
        lines += ["", "---", "", probe_md]
    return "\n".join(lines) + "\n"


def run(job_dir: Path, spec: dict, colour: dict, budget: int,
        frame_range: tuple[int, int] | None = None, rescan: bool = False) -> dict:
    out = job_dir / "assess"
    out.mkdir(parents=True, exist_ok=True)
    cache = job_dir / "cache"
    npz = out / "scan.npz"
    if npz.exists() and not rescan:
        scan = ScanResult.load(npz)
    else:
        scan = run_scan(spec, cache, frame_range=frame_range)
        scan.save(npz)
    result = sampling.assess(scan, budget)
    offset = frame_range[0] if frame_range else 0
    result["frame_offset"] = offset
    result["picks"] = render_samples(spec, cache, colour, result["picks"], out, offset)
    crop = detect_crop(spec, cache)
    result["crop"] = crop
    timeline_png(scan, result, out / "timeline.png")
    (out / "assessment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    probe_md = (job_dir / "probe.md").read_text(encoding="utf-8") if (job_dir / "probe.md").exists() else None
    (out / "report.md").write_text(report_md(probe_md, result, crop), encoding="utf-8")
    return result
