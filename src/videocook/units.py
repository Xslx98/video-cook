"""Source-structure edits after intake: switch playlist, add units, ISO and DVD helpers."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from videocook.job import Job
from videocook.toolchain import run


def set_playlist(job: Job, playlist: str, unit_id: str | None = None) -> None:
    from videocook.vsource import resolve_bdmv_items

    if unit_id:
        ep = next(e for e in job.data["episodes"] if e["id"] == unit_id)
        src = ep["source"]
    else:
        src = job.data["source"]
    if src.get("kind") != "bdmv":
        raise ValueError("playlist can only be set on Blu-ray sources")
    root = Path(src["root"])
    src["playlist"] = playlist
    src["items"] = resolve_bdmv_items(root, playlist)
    job.save()


def add_unit(job: Job, kind: str, label: str, playlist: str | None = None, file: str | None = None,
             unit_id: str | None = None) -> dict:
    from videocook.vsource import resolve_bdmv_items

    if playlist:
        roots = [e["source"]["root"] for e in job.data["episodes"] if e["source"].get("kind") == "bdmv"]
        root = Path(roots[0] if roots else job.data["source"]["root"])
        source = {"kind": "bdmv", "root": str(root), "playlist": playlist,
                  "items": resolve_bdmv_items(root, playlist)}
    elif file:
        source = {"kind": "file", "path": file}
    else:
        raise ValueError("give --playlist or --file")
    if not job.is_series:
        raise ValueError("extra units are only supported on series jobs")
    uid = unit_id or f"S01{label.upper()}"
    entry = {"id": uid, "kind": kind, "label": label, "source": source}
    job.data["episodes"].append(entry)
    job.save()
    return entry


def mount_iso(iso: Path) -> Path:
    """Mount read-only with Windows' built-in mounter; returns the drive root."""
    ps = (f"$i = Mount-DiskImage -ImagePath '{iso}' -Access ReadOnly -PassThru -StorageType ISO; "
          "($i | Get-Volume).DriveLetter")
    res = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    letter = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    if res.returncode != 0 or len(letter) != 1:
        raise RuntimeError(f"could not mount {iso}; mount it in Explorer and pass the drive letter instead. "
                           f"{res.stderr.strip()[:300]}")
    return Path(f"{letter}:/")


def dvd_main_title(video_ts: Path, work: Path) -> Path:
    """Stream-copy the longest title set (VTS_xx_1..n.VOB) into one MPEG-PS file."""
    video_ts = video_ts / "VIDEO_TS" if (video_ts / "VIDEO_TS").is_dir() else video_ts
    sets: dict[str, list[Path]] = {}
    for vob in sorted(video_ts.glob("VTS_*_*.VOB")):
        vts, part = vob.stem.split("_")[1:3]
        if part != "0":  # _0 is the menu
            sets.setdefault(vts, []).append(vob)
    if not sets:
        raise ValueError(f"no title VOBs in {video_ts}")
    vts, vobs = max(sets.items(), key=lambda kv: sum(v.stat().st_size for v in kv[1]))
    out = work / f"dvd_title_vts{vts}.mpg"
    if not out.exists():
        work.mkdir(parents=True, exist_ok=True)
        concat = "concat:" + "|".join(str(v) for v in vobs)
        res = run("ffmpeg", "-hide_banner", "-nostats", "-y", "-fflags", "+genpts", "-i", concat,
                  "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-f", "dvd", out, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"DVD title remux failed: {res.stderr[-1500:]}")
    (work / "dvd_title.json").write_text(json.dumps({"vts": vts, "vobs": [str(v) for v in vobs],
                                                     "ifo": str(video_ts / f"VTS_{vts}_0.IFO")}, indent=2))
    return out


def _cmd_playlist(args: argparse.Namespace) -> int:
    job = Job.resolve(args.job)
    set_playlist(job, args.playlist, args.unit)
    print(f"playlist set to {args.playlist}")
    return 0


def _cmd_unit_add(args: argparse.Namespace) -> int:
    job = Job.resolve(args.job)
    entry = add_unit(job, args.kind, args.label, args.playlist, args.file, args.id)
    print(f"added unit {entry['id']}")
    return 0


def add_commands(sub) -> None:
    p = sub.add_parser("playlist", help="switch the Blu-ray playlist of a job (or one episode)")
    p.add_argument("job")
    p.add_argument("playlist")
    p.add_argument("--unit")
    p.set_defaults(func=_cmd_playlist)

    p = sub.add_parser("unit-add", help="add an extra unit (NCOP/NCED/...) to a series job")
    p.add_argument("job")
    p.add_argument("--kind", default="extra", choices=["ncop", "nced", "extra", "main"])
    p.add_argument("--label", required=True)
    p.add_argument("--playlist")
    p.add_argument("--file")
    p.add_argument("--id")
    p.set_defaults(func=_cmd_unit_add)
