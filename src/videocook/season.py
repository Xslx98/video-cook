"""Season summary across episode assessments (design §6, §13)."""

from __future__ import annotations

import json

import numpy as np

from videocook.job import Job

KEYS = ("dark_share", "mean_luma", "median_noise", "median_edge", "median_motion", "oor_frames_share")


def summary(job: Job) -> str:
    rows = []
    for u in job.units():
        f = job.dir / "assess" / u["id"] / "assessment.json"
        if f.exists():
            g = json.loads(f.read_text(encoding="utf-8"))["global"]
            rows.append((u["id"], g))
    if not rows:
        return "no episode assessments yet"
    med = {k: float(np.median([g[k] for _, g in rows])) for k in KEYS}
    mad = {k: float(np.median([abs(g[k] - med[k]) for _, g in rows])) or 1e-6 for k in KEYS}
    lines = ["# Season summary", "", "| episode | dark | luma | noise | edge | motion | OOR | flags |",
             "|---|---|---|---|---|---|---|---|"]
    flagged = []
    for ep, g in rows:
        flags = [k for k in KEYS if abs(g[k] - med[k]) / mad[k] > 4]
        if abs(g["dark_share"] - med["dark_share"]) > 0.10 and "dark_share" not in flags:
            flags.append("dark_share")
        if flags:
            flagged.append(ep)
        lines.append(f"| {ep} | {g['dark_share']:.1%} | {g['mean_luma']:.2f} | {g['median_noise']:.2f} | "
                     f"{g['median_edge']:.3f} | {g['median_motion']:.4f} | {g['oor_frames_share']:.1%} | "
                     f"{', '.join(flags)} |")
    lines += ["", f"Episodes deviating from the season: {', '.join(flagged) or 'none'}"]
    text = "\n".join(lines) + "\n"
    (job.dir / "assess" / "season.md").write_text(text, encoding="utf-8")
    return text
