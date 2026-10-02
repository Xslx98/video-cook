"""Assessment stage: scan → buckets → sampled frames → report for Claude and the user."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from videocook import images, sampling
from videocook.scan import ScanResult, detect_crop, run_scan
from videocook.vsource import open_trimmed

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

    fig, ax = plt.subplots(7, 1, figsize=(14, 11.5), sharex=True,
                           gridspec_kw={"height_ratios": [1, 1, 1, 1, 1, 1, 0.4]})
    series = [
        ("luma (0-1)", sampling._norm_luma(d["luma"])),
        ("dark-flat share", d["dark_flat"]),
        ("edge density", d["edge"]),
        ("noise (8-bit codes)", d["noise"]),
        ("motion", d["motion"]),
        ("CAMBI banding", d["cambi"]),
    ]
    for a, (name, v) in zip(ax, series):
        a.plot(t[::k], sm(v), lw=0.7, color="#333")
        a.set_ylabel(name, fontsize=8)
        a.tick_params(labelsize=7)
    for s in result["shot_list"]:
        ax[6].axvspan(s["start"] / scan.fps / 60, s["end"] / scan.fps / 60,
                      color=BUCKET_COLOURS[s["bucket"]], lw=0)
    for p in result["picks"]:
        for a in ax[:6]:
            a.axvline(p["time"] / 60, color=BUCKET_COLOURS[p["bucket"]], lw=0.8, alpha=0.8)
    ax[6].set_yticks([])
    ax[6].set_xlabel("minutes")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in BUCKET_COLOURS.values()]
    fig.legend(handles, list(BUCKET_COLOURS), loc="lower center", ncol=8, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=90)
    plt.close(fig)


def render_samples(spec: dict, cache_dir: Path, colour: dict, picks: list[dict], out: Path,
                   trim: list | None = None) -> list[dict]:
    import shutil

    for sub in ("frames", "crops"):  # never mix images from an older assessment
        shutil.rmtree(out / sub, ignore_errors=True)
    clip = open_trimmed(spec, cache_dir, trim, hw=False)
    rgb = images.to_rgb(clip, colour)
    cw, ch = (960, 540) if clip.width > 2000 else (480, 270)
    zoom = 1 if clip.width > 2000 else 2
    tiles = []
    for i, p in enumerate(picks, 1):
        n = p["frame"]
        arr = images.frame_array(rgb, n)
        lum = images.luma_array(clip, n)
        crop_kind = p["bucket"]
        if p.get("role") == "cambi-hotspot":  # look for the banded gradient, not the bucket's trait
            crop_kind = "dark_flat" if p["metrics"]["luma"] < 0.3 else "bright_gradient"
        x, y = images.choose_crop(lum, crop_kind, cw, ch)
        tag = f"{i:02d}_{p['bucket']}_f{n}"
        full = out / "frames" / f"{tag}.png"
        crop = out / "crops" / f"{tag}_x{x}y{y}.png"
        images.save_png(arr, full)
        crop_arr = images.crop_zoom(arr, x, y, cw, ch, zoom)
        images.save_png(crop_arr, crop)
        p.update({"image": str(full), "crop": str(crop), "crop_box": [x, y, cw, ch], "zoom": zoom})
        if crop_kind in ("dark_flat", "dark_noisy"):
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
             f"near-duplicate frames: {g['near_duplicate_frames_share']:.1%}",
             f"- CAMBI banding (scan resolution): median {g.get('cambi_median', 0):.2f}, "
             f"p95 {g.get('cambi_p95', 0):.2f}, max {g.get('cambi_max', 0):.2f}"]
    if crop:
        lines.append(f"- black borders: top {crop['top']}, bottom {crop['bottom']}, left {crop['left']}, "
                     f"right {crop['right']} → active {crop['active'][0]}x{crop['active'][1]}")
    lines += ["", "## Frame composition", "", "| bucket | share | shots | sampled | exposes |",
              "|---|---|---|---|---|"]
    for b, c in result["composition"].items():
        lines.append(f"| {b} | {c['share']:.1%} | {c['shots']} | {result['allocation'].get(b, 0)} | "
                     f"{sampling.BUCKETS[b][1]} |")
    lines += ["", "## Sampled frames", "", "| # | frame | time | bucket | role | luma | dark-flat | edge | noise | motion | CAMBI |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(result["picks"], 1):
        m = p["metrics"]
        mm, ss = divmod(p["time"], 60)
        lines.append(f"| {i} | {p['frame']} | {int(mm)}:{ss:04.1f} | {p['bucket']} | {p['role']} | "
                     f"{m['luma']:.2f} | {m['dark_flat']:.2f} | {m['edge']:.3f} | {m['noise']:.2f} | {m['motion']:.4f} | "
                     f"{m.get('cambi', 0):.2f} |")
    lines += ["", "Images: `assess/sheet.png` (overview), `assess/frames/*.png` (full), "
              "`assess/crops/*.png` (zoomed risk regions), `assess/timeline.png`."]
    if probe_md:
        lines += ["", "---", "", probe_md]
    return "\n".join(lines) + "\n"


def run(job_dir: Path, spec: dict, colour: dict, budget: int,
        trim: list | None = None, rescan: bool = False, out: Path | None = None) -> dict:
    out = out or job_dir / "assess"
    out.mkdir(parents=True, exist_ok=True)
    cache = job_dir / "cache"
    npz = out / "scan.npz"
    if npz.exists() and not rescan:
        scan = ScanResult.load(npz)
    else:
        scan = run_scan(spec, cache, trim=trim)
        scan.save(npz)
    result = sampling.assess(scan, budget)
    result["trim"] = trim or []
    result["picks"] = render_samples(spec, cache, colour, result["picks"], out, trim)
    crop = detect_crop(spec, cache, trim=trim)
    result["crop"] = crop
    timeline_png(scan, result, out / "timeline.png")
    (out / "assessment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    probe_md = (job_dir / "probe.md").read_text(encoding="utf-8") if (job_dir / "probe.md").exists() else None
    (out / "report.md").write_text(report_md(probe_md, result, crop), encoding="utf-8")
    return result
