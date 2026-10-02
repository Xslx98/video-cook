"""Source probing: what is this source, what does it contain, what is special about it.

Produces a JSON-serialisable dict (written to `probe.json` in the job dir) and a
Markdown summary for the skill to read.
"""

from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path

from videocook import bluray, fsutil
from videocook.toolchain import run, run_json

VIDEO_EXTS = {".mkv", ".mp4", ".m2ts", ".ts", ".mts", ".m4v", ".mov", ".avi", ".vob", ".webm"}
SIDECAR_EXTS = {".mka", ".ass", ".ssa", ".srt", ".sup", ".idx", ".sub", ".vtt", ".flac", ".ac3",
                ".dts", ".thd", ".eac3", ".m4a", ".opus", ".aac", ".xml", ".txt"}
LOSSLESS_AUDIO = {"pcm_bluray", "pcm_s16le", "pcm_s24le", "pcm_dvd", "flac", "alac", "truehd", "mlp"}


# ---------------------------------------------------------------------------
# Source kind


def detect_kind(path: Path) -> str:
    if path.is_file():
        ext = path.suffix.lower()
        if ext == ".iso":
            return "iso"
        if ext == ".mpls":
            return "bdmv"
        if ext in VIDEO_EXTS:
            return "file"
        raise ValueError(f"unsupported file type: {path}")
    if bluray.find_bdmv_root(path):
        return "bdmv"
    if (path / "VIDEO_TS").is_dir() or path.name.upper() == "VIDEO_TS":
        return "dvd"
    vids = [p for p in path.iterdir() if p.suffix.lower() in VIDEO_EXTS]
    if vids:
        return "folder"
    raise ValueError(f"cannot recognise source: {path}")


# ---------------------------------------------------------------------------
# Stream level helpers


def ffprobe(path: Path) -> dict:
    return run_json("ffprobe", "-v", "error", "-show_format", "-show_streams",
                    "-show_chapters", "-of", "json", path)


def mediainfo(path: Path) -> dict:
    return run_json("mediainfo", "--Output=JSON", path)


def first_frames_side_data(path: Path, frames: int = 3) -> list[str]:
    data = run_json("ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-read_intervals", f"%+#{frames}", "-show_frames",
                    "-show_entries", "frame=side_data_list", "-of", "json", path)
    kinds: list[str] = []
    for fr in data.get("frames", []):
        for sd in fr.get("side_data_list", []):
            kinds.append(sd.get("side_data_type", ""))
    return sorted(set(kinds))


def _fps(s: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = s.get(key)
        if v and v != "0/0":
            try:
                return float(Fraction(v))
            except (ValueError, ZeroDivisionError):
                pass
    return None


def hdr_info(stream: dict, side_kinds: list[str], frames_json: dict | None = None) -> dict:
    transfer = stream.get("color_transfer")
    stream_sd = [sd.get("side_data_type", "") for sd in stream.get("side_data_list", [])]
    dovi = next((sd for sd in stream.get("side_data_list", [])
                 if "DOVI" in sd.get("side_data_type", "")), None)
    info = {
        "transfer": transfer,
        "primaries": stream.get("color_primaries"),
        "matrix": stream.get("color_space"),
        "range": stream.get("color_range"),
        "hdr10": transfer == "smpte2084",
        "hlg": transfer == "arib-std-b67",
        "hdr10plus": any("2094-40" in k or "HDR10+" in k or "Dynamic Metadata" in k
                         for k in side_kinds),
        "dolby_vision": bool(dovi) or any("Dolby Vision" in k for k in side_kinds + stream_sd),
    }
    if dovi:
        info["dv_profile"] = dovi.get("dv_profile")
        info["dv_bl_compat_id"] = dovi.get("dv_bl_signal_compatibility_id")
        info["dv_el_present"] = dovi.get("el_present_flag")
    return info


def mastering_metadata(path: Path) -> dict | None:
    data = run_json("ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-read_intervals", "%+#1", "-show_frames",
                    "-show_entries", "frame=side_data_list", "-of", "json", path)
    out: dict = {}
    for fr in data.get("frames", []):
        for sd in fr.get("side_data_list", []):
            t = sd.get("side_data_type")
            if t == "Mastering display metadata":
                def n(key: str, scale: int) -> int:
                    return round(float(Fraction(sd[key])) * scale)
                # x265 --master-display order: G, B, R, WP in 0.00002 units, L in 0.0001
                out["master_display"] = (
                    f"G({n('green_x', 50000)},{n('green_y', 50000)})"
                    f"B({n('blue_x', 50000)},{n('blue_y', 50000)})"
                    f"R({n('red_x', 50000)},{n('red_y', 50000)})"
                    f"WP({n('white_point_x', 50000)},{n('white_point_y', 50000)})"
                    f"L({n('max_luminance', 10000)},{n('min_luminance', 10000)})"
                )
            elif t == "Content light level metadata":
                out["max_cll"] = f"{sd.get('max_content', 0)},{sd.get('max_average', 0)}"
    return out or None


def interlace_check(path: Path, frames: int = 600, start: float = 120) -> dict:
    """ffmpeg idet over a window: counts of progressive/TFF/BFF and repeated fields."""
    res = run("ffmpeg", "-hide_banner", "-nostats", "-ss", str(start), "-i", path,
              "-map", "0:v:0", "-vf", "idet", "-frames:v", str(frames), "-an", "-f", "null", "-",
              check=False)
    text = res.stderr
    out: dict = {}
    m = re.search(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*(\d+) Progressive:\s*(\d+) Undetermined:\s*(\d+)", text)
    if m:
        out.update(dict(zip(("tff", "bff", "progressive", "undetermined"), map(int, m.groups()))))
    m = re.search(r"Repeated Fields: Neither:\s*(\d+) Top:\s*(\d+) Bottom:\s*(\d+)", text)
    if m:
        neither, top, bottom = map(int, m.groups())
        out["repeated_fields_ratio"] = round((top + bottom) / max(1, neither + top + bottom), 3)
    if out and sum(out.get(k, 0) for k in ("tff", "bff", "progressive", "undetermined")) == 0:
        out["verdict"] = "unknown (no frames analysed)"
        return out
    if out:
        inter = out.get("tff", 0) + out.get("bff", 0)
        total = inter + out.get("progressive", 0)
        ratio = inter / max(1, total)
        out["interlaced_ratio"] = round(ratio, 3)
        rep = out.get("repeated_fields_ratio", 0)
        if ratio < 0.05:
            out["verdict"] = "progressive"
        elif 0.15 <= rep <= 0.3:
            out["verdict"] = "telecined (likely 3:2 pulldown, IVTC candidate)"
        elif ratio > 0.8:
            out["verdict"] = "interlaced"
        else:
            out["verdict"] = "mixed / hybrid"
    return out


# ---------------------------------------------------------------------------
# Lossy-source (re-encode) detection

ENCODER_TAG = re.compile(r"\b(x264|x265|svt|rav1e|aom|nvenc|qsv|amf|vvenc)\b", re.I)


def reencode_assessment(video: dict, mi_video: dict | None, kind: str) -> dict:
    """Is the video stream already a consumer lossy encode (not a disc stream)?"""
    reasons: list[str] = []
    codec = video.get("codec_name")
    w, h = video.get("width") or 0, video.get("height") or 0
    fps = _fps(video) or 24
    lib = (mi_video or {}).get("Encoded_Library", "") or ""
    if ENCODER_TAG.search(lib):
        reasons.append(f"encoder tag: {lib.split(':')[0][:60]}")
    bitrate = None
    for src in ((mi_video or {}).get("BitRate"), video.get("bit_rate")):
        if src:
            try:
                bitrate = int(float(src))
                break
            except ValueError:
                pass
    bpp = None
    if bitrate and w and h:
        bpp = bitrate / (w * h * fps)
        # Blu-ray AVC 1080p sits around 0.3-0.8 bpp; UHD HEVC around 0.15-0.4.
        threshold = 0.12 if codec == "hevc" and h > 1100 else 0.15
        if bpp < threshold:
            reasons.append(f"low bitrate: {bitrate / 1e6:.1f} Mbps ({bpp:.3f} bits/pixel)")
    if codec == "hevc" and h <= 1100 and kind != "bdmv":
        reasons.append("HEVC at <=1080p does not exist on Blu-ray: not a disc stream")
    if codec in ("av1", "vp9"):
        reasons.append(f"{codec} is a distribution codec")
    return {
        "is_lossy_reencode": bool(reasons),
        "reasons": reasons,
        "bitrate": bitrate,
        "bits_per_pixel": round(bpp, 4) if bpp else None,
        "encoder_settings": (mi_video or {}).get("Encoded_Library_Settings"),
    }


# ---------------------------------------------------------------------------
# Track summaries


def _mi_tracks(mi: dict, kind: str) -> list[dict]:
    return [t for t in mi.get("media", {}).get("track", []) if t.get("@type") == kind]


def _match_mi(stream: dict, mi_list: list[dict], used: set[int]) -> dict:
    """Pair an ffprobe stream with its MediaInfo track (by PID for TS, StreamOrder for MKV/MP4)."""
    pid = stream.get("id")
    want_fmt = {"truehd": "MLP FBA", "ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS",
                "flac": "FLAC", "aac": "AAC", "opus": "Opus", "pcm_bluray": "PCM"}.get(stream["codec_name"])
    for k, m in enumerate(mi_list):
        if k in used:
            continue
        if pid and m.get("ID"):
            try:
                same = int(str(m["ID"]).split("-")[0].split(" ")[0]) == int(pid, 16)
            except ValueError:
                same = False
            if same and (not want_fmt or (m.get("Format") or "").startswith(want_fmt.split()[0])):
                used.add(k)
                return m
        elif not pid and str(m.get("StreamOrder")) == str(stream["index"]):
            used.add(k)
            return m
    return {}


def audio_tracks(ff: dict, mi: dict) -> list[dict]:
    mi_audio = _mi_tracks(mi, "Audio")
    used: set[int] = set()
    pid_codecs: dict[str, list[str]] = {}
    for a in ff["streams"]:
        if a["codec_type"] == "audio" and a.get("id"):
            pid_codecs.setdefault(a["id"], []).append(a["codec_name"])
    out = []
    for s in (a for a in ff["streams"] if a["codec_type"] == "audio"):
        m = _match_mi(s, mi_audio, used)
        commercial = m.get("Format_Commercial_IfAny", "") or ""
        profile = s.get("profile", "") or ""
        objects = any(x in commercial + profile for x in ("Atmos", "DTS:X"))
        lossless = (s["codec_name"] in LOSSLESS_AUDIO or "DTS-HD MA" in profile
                    or "MA" in (m.get("Format_Profile") or ""))
        core_of_truehd = (s["codec_name"] == "ac3" and s.get("id")
                          and "truehd" in pid_codecs.get(s["id"], []))
        if core_of_truehd:
            plan = "drop (AC-3 compatibility core of the TrueHD track)"
        elif objects:
            plan = "passthrough (object audio)"
        elif lossless:
            plan = "FLAC"
        else:
            plan = "passthrough (lossy)"
        out.append({
            "index": s["index"],
            "codec": s["codec_name"],
            "profile": profile or commercial or None,
            "channels": s.get("channels"),
            "layout": s.get("channel_layout"),
            "language": s.get("tags", {}).get("language") or m.get("Language"),
            "title": s.get("tags", {}).get("title") or m.get("Title"),
            "lossless": lossless,
            "object_audio": objects,
            "bit_depth": m.get("BitDepth"),
            "default_plan": plan,
        })
    return out


def subtitle_tracks(ff: dict) -> list[dict]:
    out = []
    for s in ff["streams"]:
        if s["codec_type"] != "subtitle":
            continue
        disp = s.get("disposition", {})
        out.append({
            "index": s["index"],
            "codec": s["codec_name"],
            "language": s.get("tags", {}).get("language"),
            "title": s.get("tags", {}).get("title"),
            "forced": bool(disp.get("forced")),
            "default": bool(disp.get("default")),
        })
    return out


def video_summary(video: dict, mi_video: dict | None) -> dict:
    return {
        "codec": video.get("codec_name"),
        "profile": video.get("profile"),
        "width": video.get("width"),
        "height": video.get("height"),
        "pix_fmt": video.get("pix_fmt"),
        "bit_depth": (mi_video or {}).get("BitDepth"),
        "fps": round(_fps(video) or 0, 4),
        "fps_fraction": video.get("r_frame_rate"),
        "frame_rate_mode": (mi_video or {}).get("FrameRate_Mode"),
        "field_order": video.get("field_order"),
        "scan_type": (mi_video or {}).get("ScanType"),
        "chroma_location": video.get("chroma_location"),
        "sar": video.get("sample_aspect_ratio"),
        "dar": video.get("display_aspect_ratio"),
    }


def find_sidecars(video_path: Path) -> list[dict]:
    """Files sharing the video's basename (VCB-S style `name.sc.ass`, `name.mka`)."""
    stem = video_path.stem
    out = []
    for p in sorted(fsutil.iterdir(video_path.parent)):
        if p == video_path or not p.name.startswith(stem) or not fsutil.is_file(p):
            continue
        if p.suffix.lower() not in SIDECAR_EXTS:
            continue
        middle = p.name[len(stem):-len(p.suffix)]
        out.append({"path": str(p), "ext": p.suffix.lower(),
                    "tags": [t for t in middle.split(".") if t],
                    "size": fsutil.size(p), "long_path": fsutil.too_long(p)})
    # Font folders next to the release
    for d in fsutil.iterdir(video_path.parent):
        if d.name.lower() in ("fonts", "font", "字体") and fsutil.lp(d).is_dir():
            out.append({"path": str(d), "ext": "fonts-dir", "tags": [], "size": None,
                        "long_path": False})
    return out


# ---------------------------------------------------------------------------
# Entry points


def needs_interlace_check(video: dict, result: dict) -> bool:
    if video.get("codec_name") in ("mpeg2video", "vc1"):
        return True
    if video.get("field_order") not in (None, "progressive", "unknown"):
        return True
    if result["video"]["scan_type"] not in (None, "Progressive"):
        return True
    fps = result["video"]["fps"] or 0
    # 29.97/59.94/25 fps H.264 can hide telecine or interlacing
    return any(abs(fps - f) < 0.05 for f in (29.97, 59.94, 25.0, 50.0)) and (video.get("height") or 0) <= 1080


def probe_file(path: Path, kind: str = "file", deep: bool = True) -> dict:
    ff = ffprobe(path)
    mi = mediainfo(path)
    vstreams = [s for s in ff["streams"] if s["codec_type"] == "video"
                and not s.get("disposition", {}).get("attached_pic")]
    if not vstreams:
        raise ValueError(f"no video stream in {path}")
    video = vstreams[0]
    mi_video = next(iter(_mi_tracks(mi, "Video")), None)
    side = first_frames_side_data(path)
    result = {
        "path": str(path),
        "container": ff["format"].get("format_name"),
        "duration": float(ff["format"].get("duration", 0) or 0),
        "size": int(ff["format"].get("size", 0) or 0),
        "video": video_summary(video, mi_video),
        "extra_video_streams": len(vstreams) - 1,
        "hdr": hdr_info(video, side),
        "audio": audio_tracks(ff, mi),
        "subtitles": subtitle_tracks(ff),
        "chapters": len(ff.get("chapters", [])),
        "attachments": [s.get("tags", {}).get("filename") for s in ff["streams"]
                        if s["codec_type"] == "attachment"],
        "reencode": reencode_assessment(video, mi_video, kind),
    }
    if result["hdr"]["hdr10"]:
        result["hdr"]["static"] = mastering_metadata(path)
    if deep and needs_interlace_check(video, result):
        result["interlace"] = interlace_check(path, start=min(120.0, result["duration"] / 4))
    return result


def probe_bdmv(path: Path) -> dict:
    root = bluray.find_bdmv_root(path if path.is_dir() else path.parent.parent.parent)
    playlists = bluray.scan_playlists(root)
    groups = bluray.group_playlists(playlists)
    bluray.classify_groups(groups)
    useful = [g for g in groups if g["role"] in ("feature-candidate", "episode-candidate")]
    main = useful[0] if useful else (groups[0] if groups else None)
    detail = None
    tracks = None
    if main:
        # Use the longest clip of the main playlist for stream properties.
        clips = sorted(set(main["clips"]),
                       key=lambda c: (root / "BDMV" / "STREAM" / f"{c}.m2ts").stat().st_size,
                       reverse=True)
        clip0 = root / "BDMV" / "STREAM" / f"{clips[0]}.m2ts"
        detail = probe_file(clip0, kind="bdmv")
        if not detail["reencode"]["bitrate"] and detail["duration"]:
            detail["reencode"]["bitrate"] = int(detail["size"] * 8 / detail["duration"])
            detail["reencode"]["bitrate_note"] = "whole-clip mux rate (video + all audio)"
        mpls = root / "BDMV" / "PLAYLIST" / f"{main['playlist']}.mpls"
        mk = run_json("mkvmerge", "-J", mpls)
        tracks = [{"id": t["id"], "type": t["type"], "codec": t["codec"],
                   "language": t["properties"].get("language"),
                   "channels": t["properties"].get("audio_channels"),
                   "core_of": t["properties"].get("multiplexed_tracks")}
                  for t in mk.get("tracks", [])]
        # mkvmerge reads languages from the playlist; ids follow the ffprobe order.
        by_id = {t["id"]: t for t in tracks}
        for item in (detail or {}).get("audio", []) + (detail or {}).get("subtitles", []):
            t = by_id.get(item["index"])
            if t and not item.get("language"):
                item["language"] = t["language"]
        if any(t["type"] == "video" for t in tracks[1:]) and detail:
            detail["hdr"]["dolby_vision_el_track"] = True
    return {
        "bdmv_root": str(root),
        "playlist_count": len(playlists),
        "playlist_groups": groups,
        "main_playlist": main["playlist"] if main else None,
        "main_tracks": tracks,
        "main_stream": detail,
    }


def probe(path: Path) -> dict:
    path = Path(path)
    kind = detect_kind(path)
    result: dict = {"source": str(path), "kind": kind}
    if kind == "bdmv":
        result.update(probe_bdmv(path))
    elif kind == "file":
        result["main_stream"] = probe_file(path)
        result["sidecars"] = find_sidecars(path)
    elif kind == "folder":
        vids = sorted(p for p in path.iterdir() if p.suffix.lower() in VIDEO_EXTS)
        result["files"] = [str(v) for v in vids]
        result["main_stream"] = probe_file(vids[0])
        result["sidecars"] = find_sidecars(vids[0])
    elif kind in ("iso", "dvd"):
        result["note"] = f"{kind} sources are opened via `vcook mount`/VOB demux; not yet probed"
    result["flags"] = flags(result)
    return result


def flags(result: dict) -> list[str]:
    """Headline issues the interrogation must address."""
    out: list[str] = []
    ms = result.get("main_stream")
    if not ms:
        return out
    hdr = ms["hdr"]
    if hdr.get("dolby_vision") or hdr.get("dolby_vision_el_track"):
        out.append("DOLBY_VISION: default plan drops DV and keeps the HDR10 base layer")
    if hdr.get("hdr10plus"):
        out.append("HDR10PLUS: dynamic metadata present; will be passed to x265")
    if hdr.get("hdr10"):
        out.append("HDR10: static metadata must be passed to x265 (outside the VCB-S guides)")
    if hdr.get("hlg"):
        out.append("HLG source")
    if not hdr.get("primaries") and not hdr.get("transfer"):
        h = ms["video"].get("height") or 0
        guess = "BT.709" if h >= 720 else "BT.601 (check NTSC 170m vs PAL 470bg)"
        out.append(f"COLOUR_UNTAGGED: no colour metadata; assume {guess}")
    if ms["reencode"]["is_lossy_reencode"]:
        out.append("LOSSY_SOURCE: already an encode; default recommendation is video passthrough")
    il = ms.get("interlace", {})
    if il.get("verdict") and il["verdict"] != "progressive":
        out.append(f"INTERLACE: {il['verdict']}")
    if ms["video"].get("frame_rate_mode") == "VFR":
        out.append("VFR source: timecodes must be preserved")
    if any(a["object_audio"] for a in ms["audio"]):
        out.append("OBJECT_AUDIO: Atmos/DTS:X tracks are passed through")
    if result.get("kind") == "bdmv":
        feats = [g for g in result["playlist_groups"] if g["role"] == "feature-candidate"]
        if len(feats) > 1:
            out.append(f"MULTIPLE_CUTS: {len(feats)} feature-length playlists; ask which cut")
        if any(g["role"] == "suspicious-loop" for g in result["playlist_groups"]):
            out.append("OBFUSCATION: looping decoy playlists present; ignored")
    if any(sc.get("long_path") for sc in result.get("sidecars", [])) or fsutil.too_long(ms["path"]):
        out.append("LONG_PATH: paths exceed 260 chars; sidecars are copied into the job dir")
    if result.get("sidecars"):
        out.append(f"SIDECARS: {len(result['sidecars'])} same-name external files found")
    return out


def to_markdown(result: dict) -> str:
    """Compact human/Claude-readable summary of a probe result."""
    lines = [f"# Probe: `{result['source']}`", "", f"- kind: **{result['kind']}**"]
    for f in result.get("flags", []):
        lines.append(f"- ⚑ {f}")
    if result["kind"] == "bdmv":
        lines += ["", "## Playlists", "",
                  "| playlist | role | duration | items | chapters | duplicates |", "|---|---|---|---|---|---|"]
        for g in result["playlist_groups"][:15]:
            m, s = divmod(int(g["duration"]), 60)
            lines.append(f"| {g['playlist']} | {g['role']} | {m // 60}:{m % 60:02d}:{s:02d} | "
                         f"{g['items']} | {g['chapters']} | {len(g['duplicates'])} |")
    ms = result.get("main_stream")
    if ms:
        v = ms["video"]
        lines += ["", "## Video", "",
                  f"- {v['codec']} {v['profile']} {v['width']}x{v['height']} {v['pix_fmt']} "
                  f"@ {v['fps']} fps ({v['frame_rate_mode']}), field order {v['field_order']}, "
                  f"SAR {v['sar']}",
                  f"- colour: primaries {ms['hdr']['primaries']}, transfer {ms['hdr']['transfer']}, "
                  f"matrix {ms['hdr']['matrix']}, range {ms['hdr']['range']}"]
        if ms["hdr"].get("static"):
            lines.append(f"- HDR10 static: {ms['hdr']['static']}")
        r = ms["reencode"]
        lines.append(f"- bitrate {r['bitrate'] and round(r['bitrate'] / 1e6, 2)} Mbps, "
                     f"{r['bits_per_pixel']} bits/pixel; lossy re-encode: {r['is_lossy_reencode']} "
                     f"{'; '.join(r['reasons'])}")
        if ms.get("interlace"):
            lines.append(f"- interlace check: {ms['interlace']}")
        lines += ["", "## Audio", "", "| # | codec | profile | ch | lang | title | plan |",
                  "|---|---|---|---|---|---|---|"]
        for a in ms["audio"]:
            lines.append(f"| {a['index']} | {a['codec']} | {a['profile'] or ''} | {a['layout'] or a['channels']} "
                         f"| {a['language'] or ''} | {a['title'] or ''} | {a['default_plan']} |")
        if ms["subtitles"]:
            lines += ["", "## Subtitles", ""]
            lines.append(", ".join(f"{s['index']}:{s['codec']}/{s['language']}"
                                   f"{' forced' if s['forced'] else ''}" for s in ms["subtitles"]))
    if result.get("main_tracks"):
        lines += ["", "## Playlist tracks (mkvmerge)", ""]
        lines.append(", ".join(f"{t['id']}:{t['codec']}/{t['language']}"
                               f"{' (core of ' + str(t['core_of']) + ')' if t['core_of'] else ''}"
                               for t in result["main_tracks"] if t["type"] != "video"))
    if result.get("sidecars"):
        lines += ["", "## Sidecar files", ""]
        for s in result["sidecars"]:
            lines.append(f"- `{Path(s['path']).name}` ({s['ext']}, tags {s['tags']})")
    return "\n".join(lines) + "\n"


def save(result: dict, job_dir: Path) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "probe.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (job_dir / "probe.md").write_text(to_markdown(result), encoding="utf-8")
