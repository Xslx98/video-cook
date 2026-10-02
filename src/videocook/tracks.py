"""Audio, subtitle, font and chapter preparation for one unit (design §8, §11)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from videocook import bluray
from videocook.toolchain import run, run_json, tool

OPUS_KBPS = {1: 96, 2: 128, 6: 256, 8: 320}


def unit_media(unit: dict) -> Path:
    """The file mkvmerge reads tracks from: the playlist (.mpls) or the source file."""
    src = unit["source"]
    if src["kind"] == "bdmv":
        return Path(src["root"]) / "BDMV" / "PLAYLIST" / f"{src['playlist']}.mpls"
    return Path(src["path"])


def split_parts(trim: list, fps: float) -> str:
    """mkvmerge `--split parts:` spec that keeps and joins the trim segments."""
    def ts(sec: float) -> str:
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"

    return "parts:" + ",+".join(f"{ts(a / fps)}-{ts(b / fps)}" for a, b in trim)


def _single_output(base: Path) -> None:
    """mkvmerge numbers split output files (name-001.mka); normalise to `base`."""
    if base.exists():
        return
    hits = sorted(base.parent.glob(f"{base.stem}-*{base.suffix}"))
    if len(hits) != 1:
        raise RuntimeError(f"expected one split output for {base.name}, found {[h.name for h in hits]}")
    os.replace(hits[0], base)


def demux(unit: dict, plan: dict, work: Path, trim: list | None = None, fps: float | None = None) -> Path:
    """Copy the planned audio/subtitle tracks (no video) into work/tracks.mka.

    With `trim`, only the trimmed segments are kept (joined), using the source frame rate.
    Splitting happens here, before any FLAC conversion (mkvmerge cannot split FLAC).
    """
    out = work / "tracks.mka"
    if out.exists():
        return out
    a_ids = [str(t.get("mkv_id", t["index"])) for t in plan["audio"] if t["action"] != "drop" and "index" in t]
    s_ids = [str(t.get("mkv_id", t["index"])) for t in plan["subs"] if t["action"] != "drop" and "index" in t]
    part = work / "tracks.part.mka"
    args = ["-o", str(part), "-D", "--no-attachments"]
    args += ["--audio-tracks", ",".join(a_ids)] if a_ids else ["-A"]
    args += ["--subtitle-tracks", ",".join(s_ids)] if s_ids else ["-S"]
    if trim:
        args += ["--split", split_parts(trim, fps)]
    args.append(str(unit_media(unit)))
    res = run("mkvmerge", *args, check=False)
    if res.returncode >= 2:  # 1 = warnings
        m = re.search(r"The track number (\d+) from the file .*? cannot be appended.*?: (.*)", res.stdout)
        if m:
            raise RuntimeError(f"track {m.group(1)} changes format between the playlist's clips "
                               f"({m.group(2).strip()}); it cannot be joined — drop it or pick another track")
        raise RuntimeError(f"mkvmerge demux failed: {res.stdout[-2000:]}")
    _single_output(part)
    os.replace(part, out)
    return out


def trim_external_audio(src: Path, trim: list, fps: float, out: Path) -> Path:
    """Cut and join trim segments of an external audio file (re-encoded to FLAC; test runs only)."""
    if out.exists():
        return out
    chains = "".join(f"[0:a]atrim=start={a / fps}:end={b / fps},asetpts=PTS-STARTPTS[a{i}];"
                     for i, (a, b) in enumerate(trim))
    joined = "".join(f"[a{i}]" for i in range(len(trim))) + f"concat=n={len(trim)}:v=0:a=1[out]"
    res = run("ffmpeg", "-hide_banner", "-nostats", "-y", "-i", src, "-filter_complex", chains + joined,
              "-map", "[out]", "-c:a", "flac", out, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"trimming external audio failed: {res.stderr[-1000:]}")
    return out


def mka_tracks(mka: Path) -> list[dict]:
    """mkvmerge identification of tracks.mka; 'source_id' maps back to the plan index."""
    info = run_json("mkvmerge", "-J", mka)
    return info["tracks"]


def _source_ids(unit: dict, plan: dict) -> list[int]:
    """Order in which kept tracks appear in tracks.mka (audio first, then subtitles)."""
    a = [t["index"] for t in plan["audio"] if t["action"] != "drop" and "index" in t]
    s = [t["index"] for t in plan["subs"] if t["action"] != "drop" and "index" in t]
    return a + s


def stream_starts(mka: Path) -> dict[int, float]:
    info = run_json("ffprobe", "-v", "error", "-show_entries", "stream=index,start_time", "-of", "json", mka)
    return {s["index"]: float(s.get("start_time") or 0) for s in info["streams"]}


def convert_audio(plan: dict, mka: Path, work: Path, unit: dict) -> list[dict]:
    """FLAC/Opus conversion of kept tracks; returns mux entries for every kept audio track."""
    order = _source_ids(unit, plan)
    starts = stream_starts(mka)
    entries = []
    for t in plan["audio"]:
        if t["action"] == "drop" or "index" not in t:
            continue
        mka_id = order.index(t["index"])
        entry = {**t, "mka_id": mka_id}
        if t["action"] in ("flac", "opus"):
            ext = "flac" if t["action"] == "flac" else "opus"
            out = work / f"audio_{t['index']}.{ext}"
            if not out.exists():
                info = run_json("ffprobe", "-v", "error", "-select_streams", str(mka_id),
                                "-show_entries", "stream=bits_per_raw_sample,sample_fmt,channels",
                                "-of", "json", mka)["streams"][0]
                cmd = ["-hide_banner", "-nostats", "-y", "-i", str(mka), "-map", f"0:{mka_id}",
                       "-map_metadata", "-1"]
                if ext == "flac":
                    bits = int(info.get("bits_per_raw_sample") or 0)
                    cmd += ["-c:a", "flac", "-compression_level", "8"]
                    if bits > 16 or info.get("sample_fmt", "").startswith("s32"):
                        cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
                else:
                    ch = int(info.get("channels") or 2)
                    kbps = t.get("bitrate") or OPUS_KBPS.get(ch, 64 * ch)
                    cmd += ["-c:a", "libopus", "-b:a", f"{kbps}k", "-mapping_family", "1" if ch > 2 else "0"]
                tmp = out.with_suffix(out.suffix + ".part")
                res = run("ffmpeg", *cmd, "-f", "flac" if ext == "flac" else "ogg", str(tmp), check=False)
                if res.returncode != 0:
                    raise RuntimeError(f"audio conversion failed for track {t['index']}: {res.stderr[-1500:]}")
                os.replace(tmp, out)
            entry["file"] = str(out)
            entry["delay_ms"] = round(starts.get(mka_id, 0.0) * 1000)
        entries.append(entry)
    return entries


# --- fonts -------------------------------------------------------------------------

def system_font_dirs() -> list[str]:
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    user = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
    if os.path.isdir(user):
        dirs.append(user)
    return dirs


def subset_fonts(ass_files: list[Path], font_dirs: list[str], out_dir: Path, db_dir: Path) -> dict:
    """assfonts subset; returns {"fonts": [paths], "missing": [...], "log": str}."""
    import shutil

    shutil.rmtree(out_dir, ignore_errors=True)  # assfonts never overwrites (adds _1, _2 copies)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_dir.mkdir(parents=True, exist_ok=True)
    dirs = [*font_dirs, *system_font_dirs()]
    run("assfonts", "-f", *dirs, "-d", db_dir, "-b", check=False)
    # -c: one combined subset per font across all subtitle files (sc/tc share fonts)
    combine = ["-c"] if len(ass_files) > 1 else []
    res = run("assfonts", "-f", *dirs, "-d", db_dir, "-o", out_dir, "-s", *combine, "-v", "2",
              "-i", *map(str, ass_files), check=False)
    log = res.stdout + res.stderr
    missing = sorted(set(re.findall(r'Missing the font: "([^"]+)"', log)))
    missing_glyphs = sorted(set(re.findall(r"\[WARN\][^\n]*(?:glyph|codepoint)[^\n]*", log, re.I)))
    warnings = sorted(set(re.findall(r"\[WARN\] (Style [^\n]*not found[^\n]*)", log)))
    fonts = sorted(str(p) for p in out_dir.rglob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
    return {"fonts": fonts, "missing": missing, "missing_glyphs": missing_glyphs, "warnings": warnings,
            "log": log, "ok": not missing and not missing_glyphs}


# --- chapters -----------------------------------------------------------------------

def chapter_times(unit: dict) -> list[float]:
    src = unit["source"]
    if src["kind"] == "bdmv":
        pl = bluray.parse_mpls(Path(src["root"]) / "BDMV" / "PLAYLIST" / f"{src['playlist']}.mpls")
        return pl.chapters
    info = run_json("ffprobe", "-v", "error", "-show_chapters", "-of", "json", src["path"])
    return [float(c["start_time"]) for c in info.get("chapters", [])]


def chapters_xml(times: list[float], language: str, path: Path) -> Path | None:
    times = [t for t in times if t >= 0]
    if not times:
        return None
    if times[0] > 0.5:
        times = [0.0, *times]
    atoms = []
    for i, t in enumerate(times, 1):
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        stamp = f"{int(h):02d}:{int(m):02d}:{s:012.9f}"
        atoms.append(f"""    <ChapterAtom>
      <ChapterTimeStart>{stamp}</ChapterTimeStart>
      <ChapterDisplay><ChapterString>{escape(f'Chapter {i:02d}')}</ChapterString>
        <ChapterLanguage>{language}</ChapterLanguage></ChapterDisplay>
    </ChapterAtom>""")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<Chapters>\n  <EditionEntry>\n'
                    + "\n".join(atoms) + "\n  </EditionEntry>\n</Chapters>\n", encoding="utf-8")
    return path


def qpfile(times: list[float], fps: float, frames: int, path: Path) -> Path | None:
    keys = sorted({round(t * fps) for t in times if 0 < round(t * fps) < frames})
    if not keys:
        return None
    path.write_text("".join(f"{k} I\n" for k in keys), encoding="ascii")
    return path


def ffprobe_json(path: Path) -> dict:
    return json.loads(subprocess.run([str(tool("ffprobe")), "-v", "error", "-show_streams", "-show_format",
                                      "-show_chapters", "-of", "json", str(path)],
                                     capture_output=True, text=True, encoding="utf-8").stdout)
