"""Open a job's source as a VapourSynth clip.

The same function is used by the scan, the sampler and the generated encode
scripts, so frame numbers agree across every stage.

A source spec is a plain dict (stored in job.toml under [source]):

    {"kind": "file", "path": "..."}
    {"kind": "bdmv", "root": "...", "playlist": "00503",
     "items": [{"clip": "00337", "first": 0, "last": 15923}, ...]}

`first`/`last` are inclusive frame numbers inside each m2ts, resolved once at
job creation (`resolve_bdmv_items`) so the scripts need no probing.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import vapoursynth as vs

from videocook import bluray

core = vs.core


def _cachefile(cache_dir: Path, media: Path) -> str:
    key = hashlib.sha1(str(media).encode("utf-8")).hexdigest()[:12]
    cache_dir.mkdir(parents=True, exist_ok=True)
    return str(cache_dir / f"{media.stem[:40]}.{key}.lwi")


def _lsmas(media: Path, cache_dir: Path, hw: bool) -> vs.VideoNode:
    return core.lsmas.LWLibavSource(
        str(media), cachefile=_cachefile(cache_dir, media), prefer_hw=3 if hw else 0
    )


def open_source(spec: dict, cache_dir: str | Path, hw: bool = False) -> vs.VideoNode:
    cache_dir = Path(cache_dir)
    if spec["kind"] == "file":
        return _lsmas(Path(spec["path"]), cache_dir, hw)
    if spec["kind"] == "bdmv":
        stream = Path(spec["root"]) / "BDMV" / "STREAM"
        parts = []
        for item in spec["items"]:
            clip = _lsmas(stream / f"{item['clip']}.m2ts", cache_dir, hw)
            parts.append(clip[item["first"] : item["last"] + 1])
        return core.std.Splice(parts, mismatch=False) if len(parts) > 1 else parts[0]
    raise ValueError(f"unsupported source kind {spec['kind']!r}")


def resolve_bdmv_items(root: Path, playlist: str) -> list[dict]:
    """Translate playlist in/out times into frame ranges of each m2ts.

    MPLS in/out times are presentation timestamps on the clip's own clock; the
    first displayed frame of an m2ts carries the clip's start PTS.
    """
    from videocook.toolchain import run_json

    pl = bluray.parse_mpls(root / "BDMV" / "PLAYLIST" / f"{playlist}.mpls")
    items = []
    starts: dict[str, tuple[float, Fraction]] = {}
    for it in pl.items:
        m2ts = root / "BDMV" / "STREAM" / f"{it.clip}.m2ts"
        if it.clip not in starts:
            info = run_json("ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=start_time,r_frame_rate", "-of", "json", m2ts)
            st = info["streams"][0]
            starts[it.clip] = (float(st["start_time"]), Fraction(st["r_frame_rate"]))
        start, fps = starts[it.clip]
        first = max(0, round((it.in_time / bluray.TICKS - start) * fps))
        last = round((it.out_time / bluray.TICKS - start) * fps) - 1
        items.append({"clip": it.clip, "first": first, "last": last})
    return items
