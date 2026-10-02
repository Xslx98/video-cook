"""Stage 4a — filter check: source / filtered / amplified difference on the sampled frames."""

from __future__ import annotations

import json
from pathlib import Path

from videocook import images
from videocook.job import Job
from videocook.script import load_outputs, script_path


def picks_for(job: Job, unit_id: str) -> list[dict]:
    base = job.dir / "assess" / (unit_id if job.is_series else "")
    data = json.loads((base / "assessment.json").read_text(encoding="utf-8"))
    return data["picks"]


def triptychs(job: Job, unit_id: str, out_dir: Path | None = None, limit: int | None = None) -> list[Path]:
    facts = job.stream_facts()
    res, src = load_outputs(script_path(job, unit_id))
    res_rgb = images.to_rgb(res, facts["colour"])
    src_rgb = images.to_rgb(src, facts["colour"])
    out_dir = out_dir or job.sub("trial", "filters", unit_id)
    crop = job.data["video"].get("filters", {}).get("crop") or {}
    ox, oy = crop.get("left", 0), crop.get("top", 0)
    paths = []
    for i, p in enumerate(picks_for(job, unit_id)[:limit], 1):
        n = p["frame"]  # assessment and scripts share the (trimmed) numbering
        a = images.frame_array(src_rgb, n)
        b = images.frame_array(res_rgb, n)
        x, y, cw, ch = p["crop_box"]
        zoom = p["zoom"]
        # crop box is in source coordinates; the filtered frame may be cropped
        sa = images.crop_zoom(a, x, y, cw, ch, zoom)
        bx, by = max(0, x - ox), max(0, y - oy)
        sb = images.crop_zoom(b, bx, by, cw, ch, zoom)
        if sb.shape != sa.shape:
            sb = sa * 0
        diff = images.amplified_diff(sb, sa)
        row = images.hstack(images.label(sa, f"source f{n}"), images.label(sb, "filtered"),
                            images.label(diff, "difference x8"))
        if p["bucket"] in ("dark_flat", "dark_noisy"):
            row = images.vstack(row, images.hstack(images.label(images.boost_dark(sa), "source (boosted)"),
                                                   images.label(images.boost_dark(sb), "filtered (boosted)"),
                                                   images.label(diff, "difference x8")))
        path = out_dir / f"{i:02d}_{p['bucket']}_f{n}.png"
        images.save_png(row, path)
        paths.append(path)
    return paths
