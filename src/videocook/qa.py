"""Stage 6a — QA of a finished unit (design §14). Any failure blocks delivery."""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

from videocook import images
from videocook.toolchain import run, run_json


def crc32(path: Path) -> str:
    crc = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08X}"


def full_decode(path: Path) -> list[str]:
    res = run("ffmpeg", "-hide_banner", "-nostats", "-v", "error", "-i", path,
              "-map", "0:v", "-map", "0:a?", "-f", "null", "-", check=False)
    return [ln for ln in res.stderr.splitlines() if ln.strip()]


def count_frames(path: Path) -> int:
    info = run_json("ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                    "-show_entries", "stream=nb_read_packets", "-of", "json", path)
    return int(info["streams"][0]["nb_read_packets"])


def check(unit_id: str, mkv: Path, expect: dict, out_dir: Path) -> dict:
    """expect: frames, fps, colour flags, audio/sub counts, chapters, hdr."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    def add(name: str, ok: bool, detail: str, blocking: bool = True) -> None:
        results.append({"check": name, "ok": ok, "detail": detail, "blocking": blocking})

    errors = full_decode(mkv)
    add("full decode", not errors, f"{len(errors)} error line(s)" + (f": {errors[:3]}" if errors else ""))

    frames = count_frames(mkv)
    add("frame count", frames == expect["frames"], f"{frames} in file, {expect['frames']} expected")

    info = run_json("ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_chapters",
                    "-of", "json", mkv)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    frame = 1 / expect["fps"]
    vdur = frames / expect["fps"]
    drift = []
    for s in info["streams"]:
        if s["codec_type"] != "audio":
            continue
        d = s.get("tags", {}).get("DURATION") or s.get("duration")
        if d:
            secs = _dur(d)
            drift.append(abs(secs - vdur))
    worst = max(drift, default=0.0)
    add("duration sync", worst < 0.5,
        f"max |audio - video| = {worst * 1000:.0f} ms ({worst / frame:.1f} frames)"
        + ("; over 1 frame (non-blocking)" if worst > frame else ""))

    colour_ok = True
    detail = []
    for key, want in (("color_primaries", expect.get("primaries")), ("color_transfer", expect.get("transfer")),
                      ("color_space", expect.get("matrix"))):
        if want and v.get(key) != want:
            colour_ok = False
        detail.append(f"{key}={v.get(key)}")
    if expect.get("encoded"):
        add("colour metadata", colour_ok, ", ".join(detail))
    if expect.get("hdr10"):
        side = run_json("ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                        "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", mkv)
        kinds = {sd.get("side_data_type") for f in side.get("frames", []) for sd in f.get("side_data_list", [])}
        add("HDR10 metadata", "Mastering display metadata" in kinds, ", ".join(sorted(k for k in kinds if k)))

    n_audio = sum(1 for s in info["streams"] if s["codec_type"] == "audio")
    n_subs = sum(1 for s in info["streams"] if s["codec_type"] == "subtitle")
    add("track layout", n_audio == expect["audio"] and n_subs == expect["subs"],
        f"audio {n_audio}/{expect['audio']}, subtitles {n_subs}/{expect['subs']}")
    langs = [s.get("tags", {}).get("language", "und") for s in info["streams"] if s["codec_type"] in ("audio", "subtitle")]
    defaults = [s["index"] for s in info["streams"] if s.get("disposition", {}).get("default")
                and s["codec_type"] == "audio"]
    add("track metadata", "und" not in langs[: expect["audio"]] and len(defaults) <= 1,
        f"languages {langs}, default audio streams {defaults}", blocking=False)
    if expect.get("chapters") is not None:
        n_ch = len(info.get("chapters", []))
        add("chapters", n_ch == expect["chapters"], f"{n_ch} in file, {expect['chapters']} expected")

    crc = crc32(mkv)
    blocking_fail = [r for r in results if not r["ok"] and r["blocking"]]
    report = {"unit": unit_id, "file": str(mkv), "size": mkv.stat().st_size, "crc32": crc,
              "passed": not blocking_fail, "checks": results}
    (out_dir / f"{unit_id}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def screenshots(unit_id: str, mkv: Path, src_clip, picks: list[dict], colour: dict, crop: dict,
                out_dir: Path) -> list[str]:
    """Source vs final output on the assessment frames (report only, no gate)."""
    import vapoursynth as vs

    enc = vs.core.lsmas.LWLibavSource(str(mkv), cachefile=str(out_dir / f"{unit_id}.lwi"))
    a_rgb = images.to_rgb(src_clip, colour)
    b_rgb = images.to_rgb(enc, colour)
    scale = enc.num_frames / src_clip.num_frames
    paths = []
    for i, p in enumerate(picks, 1):
        n = p["frame"]
        m = min(enc.num_frames - 1, round(n * scale))
        a = images.frame_array(a_rgb, n)
        b = images.frame_array(b_rgb, m)
        x, y, cw, ch = p["crop_box"]
        bx, by = max(0, x - crop.get("left", 0)), max(0, y - crop.get("top", 0))
        sa = images.crop_zoom(a, x, y, cw, ch, p["zoom"])
        sb = images.crop_zoom(b, bx, by, cw, ch, p["zoom"])
        if sa.shape != sb.shape:
            continue
        row = images.hstack(images.label(sa, f"source f{n}"), images.label(sb, "final"),
                            images.label(images.amplified_diff(sb, sa), "difference x8"))
        path = out_dir / unit_id / f"{i:02d}_{p['bucket']}_f{n}.png"
        images.save_png(row, path)
        paths.append(str(path))
    return paths


def _dur(text: str) -> float:
    m = re.match(r"(\d+):(\d+):([\d.]+)", str(text))
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return float(text)
