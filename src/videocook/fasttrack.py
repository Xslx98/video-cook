"""Fast track (design §18): is the new source similar enough to the reference job?"""

from __future__ import annotations

import json
from pathlib import Path

from videocook.job import Job


def _facts(probe: dict) -> dict:
    ms = probe["main_stream"]
    v, h = ms["video"], ms["hdr"]
    return {
        "resolution": f"{v['width']}x{v['height']}",
        "fps": round(v["fps"], 3),
        "frame_rate_mode": v.get("frame_rate_mode"),
        "colour": (h.get("primaries"), h.get("transfer"), h.get("matrix")),
        "hdr10": h.get("hdr10"),
        "dolby_vision": h.get("dolby_vision"),
        "interlace": (ms.get("interlace") or {}).get("verdict", "progressive"),
        "audio_layout": [(a["codec"], a.get("channels")) for a in ms["audio"]],
        "lossy_source": ms["reencode"]["is_lossy_reencode"],
    }


def _dark_share(job_dir: Path) -> float | None:
    files = list((job_dir / "assess").rglob("assessment.json"))
    if not files:
        return None
    vals = [json.loads(f.read_text(encoding="utf-8"))["global"]["dark_share"] for f in files]
    return sum(vals) / len(vals)


def check(job: Job, ref: Job | None = None) -> dict:
    ref = ref or Job.resolve(job.data["job"]["derived_from"])
    a, b = _facts(job.probe()), _facts(ref.probe())
    mismatches = [f"{k}: {b[k]} → {a[k]}" for k in a if a[k] != b[k]]
    da, db = _dark_share(job.dir), _dark_share(ref.dir)
    if da is not None and db is not None and abs(da - db) > 0.10:
        mismatches.append(f"dark share: {db:.1%} → {da:.1%} (>10 points)")
    elif da is None:
        mismatches.append("new job not assessed yet (run `vcook assess`)")
    return {"fast_track": not mismatches, "mismatches": mismatches, "reference": str(ref.dir)}
