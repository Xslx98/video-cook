"""Jellyfin-friendly output names (design §15)."""

from __future__ import annotations

import re

from videocook.job import Job

AUDIO_LABEL = {"truehd": "TrueHD", "dts": "DTS", "ac3": "AC3", "eac3": "EAC3", "aac": "AAC",
               "flac": "FLAC", "opus": "Opus", "pcm_bluray": "LPCM"}


def safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', " ", name).strip().rstrip(".")


def resolution_label(height: int, width: int = 0) -> str:
    if height > 1100 or width > 2000:
        return "2160p"
    if height > 800 or width > 1400:
        return "1080p"
    if height > 600:
        return "720p"
    return f"{height}p"


def audio_label(job: Job) -> str:
    if job.data["output"].get("audio_label"):
        return job.data["output"]["audio_label"]
    kept = [t for t in job.data["audio"]["tracks"] if t["action"] != "drop"]
    if not kept:
        return ""
    first = next((t for t in kept if t.get("default")), kept[0])
    if first["action"] == "flac":
        return "FLAC"
    if first["action"] == "opus":
        return "Opus"
    return AUDIO_LABEL.get(first.get("codec", ""), first.get("codec", "").upper())


def tags(job: Job, height: int, width: int) -> str:
    facts = job.stream_facts()
    res = job.data["output"].get("resolution_label") or resolution_label(height, width)
    video = "x265 10bit" if job.data["video"]["mode"] == "encode" else \
        (job.probe()["main_stream"]["video"]["codec"] or "").upper()
    parts = [res, video]
    if facts["hdr"].get("hdr10"):
        parts.append("HDR10")
    a = audio_label(job)
    if a:
        parts.append(a)
    return "[" + " ".join(parts) + "]"


def relative_path(job: Job, unit: dict, height: int, width: int) -> str:
    j = job.data["job"]
    title = safe(j["title"] or j["name"])
    folder = f"{title} ({j['year']})" if j.get("year") else title
    tag = tags(job, height, width)
    if not job.is_series:
        return f"{folder}/{folder} {tag}.mkv"
    m = re.match(r"S(\d+)E(\d+)", unit["id"])
    season = int(m.group(1)) if m else 1
    kind = unit.get("kind", "main")
    base = f"{folder}/Season {season:02d}"
    if kind != "main":
        label = unit.get("label") or unit["id"]
        return f"{base}/Extras/{title} - {safe(label)} {tag}.mkv"
    return f"{base}/{title} - {unit['id']} {tag}.mkv"
