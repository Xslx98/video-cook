"""Stage 1 — intake: probe a source and create the job with a first draft of job.toml."""

from __future__ import annotations

import re
from pathlib import Path

from videocook import fsutil, probe
from videocook.job import Job, deep_merge, slugify

EP_PATTERN = re.compile(r"(?:S(\d{1,2})E(\d{1,3}))|(?:\[(\d{1,3})(?:v\d)?\])|(?:\s-\s(\d{1,3})(?:v\d)?\s)|(?:E[Pp]?(\d{1,3}))")


def episode_id(name: str, fallback: int) -> str:
    m = EP_PATTERN.search(name)
    if m:
        if m.group(1):
            return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
        num = next(g for g in m.groups()[2:] if g)
        return f"S01E{int(num):02d}"
    return f"S01E{fallback:02d}"


def default_audio_plan(audio: list[dict]) -> list[dict]:
    plan = []
    first_kept = True
    for a in audio:
        action = {"FLAC": "flac", "passthrough (object audio)": "copy",
                  "passthrough (lossy)": "copy"}.get(a["default_plan"], "drop")
        plan.append({"index": a["index"], "action": action, "language": a.get("language") or "und",
                     "title": "", "default": action != "drop" and first_kept,
                     "codec": a["codec"], "channels": a.get("channels")})
        if action != "drop":
            first_kept = False
    return plan


def default_subtitle_plan(subs: list[dict]) -> list[dict]:
    return [{"index": s["index"], "action": "copy", "language": s.get("language") or "und",
             "title": s.get("title") or "", "default": False, "forced": s.get("forced", False)}
            for s in subs]


def sidecar_plan(sidecars: list[dict], job: Job) -> tuple[list[dict], list[dict], list[str]]:
    """External audio/subtitle files are copied into the job (short paths) and offered."""
    audio, subs, fonts = [], [], []
    ext_dir = job.sub("work", "external")
    for sc in sidecars:
        src = Path(sc["path"])
        if sc["ext"] == "fonts-dir":
            fonts.append(str(src))
            continue
        local = ext_dir / f"{len(list(ext_dir.iterdir())):02d}_{'.'.join(sc['tags']) or 'main'}{sc['ext']}"
        fsutil.copy(src, local)
        lang, title = guess_language(sc["tags"])
        entry = {"file": str(local), "original": str(src), "language": lang, "title": title,
                 "default": False, "action": "copy"}
        if sc["ext"] in (".ass", ".ssa", ".srt", ".sup", ".vtt", ".idx"):
            subs.append(entry)
        else:
            audio.append(entry)
    return audio, subs, fonts


LANG_TAGS = {
    "sc": ("chi", "简体中文"), "chs": ("chi", "简体中文"), "gb": ("chi", "简体中文"),
    "tc": ("chi", "繁體中文"), "cht": ("chi", "繁體中文"), "big5": ("chi", "繁體中文"),
    "jp": ("jpn", "日本語"), "jpn": ("jpn", "日本語"), "ja": ("jpn", "日本語"),
    "en": ("eng", "English"), "eng": ("eng", "English"),
}


def guess_language(tags: list[str]) -> tuple[str, str]:
    """VCB-S style tags: 'sc_jp' → Chinese (simplified) + Japanese bilingual."""
    parts = [p for t in tags for p in re.split(r"[_\-&]", t.lower()) if p]
    found = [LANG_TAGS[p] for p in parts if p in LANG_TAGS]
    if not found:
        return "und", ""
    lang = found[0][0]
    title = " & ".join(dict.fromkeys(n for _, n in found))
    return lang, title


def new_job(source: Path, name: str | None = None, trim: list[list[int]] | None = None) -> Job:
    result = probe.probe(source)
    kind = result["kind"]
    initial: dict = {"job": {"title": "", "type": "film"}, "run": {"trim": trim or []}}
    if kind == "bdmv":
        from videocook.vsource import resolve_bdmv_items

        root = Path(result["bdmv_root"])
        feats = [g for g in result["playlist_groups"] if g["role"] == "feature-candidate"]
        eps = [g for g in result["playlist_groups"] if g["role"] == "episode-candidate"]
        if len(eps) >= 3 and (not feats or len(eps) * 20 * 60 > feats[0]["duration"]):
            initial["job"]["type"] = "series"
            initial["episodes"] = [
                {"id": f"S01E{i:02d}", "kind": "main",
                 "source": {"kind": "bdmv", "root": str(root), "playlist": g["playlist"],
                            "items": resolve_bdmv_items(root, g["playlist"])}}
                for i, g in enumerate(sorted(eps, key=lambda g: g["playlist"]), 1)]
        else:
            pl = result["main_playlist"]
            initial["source"] = {"kind": "bdmv", "root": str(root), "playlist": pl,
                                 "items": resolve_bdmv_items(root, pl)}
        ms = result["main_stream"]
    elif kind == "folder":
        initial["job"]["type"] = "series"
        initial["episodes"] = [
            {"id": episode_id(Path(f).name, i), "kind": "main", "source": {"kind": "file", "path": f}}
            for i, f in enumerate(result["files"], 1)]
        ms = result["main_stream"]
    elif kind == "file":
        initial["source"] = {"kind": "file", "path": str(source)}
        ms = result["main_stream"]
    else:
        raise NotImplementedError(f"{kind} sources are not supported by intake yet")

    if ms["reencode"]["is_lossy_reencode"]:
        initial["video"] = {"mode": "passthrough"}
    initial["audio"] = {"tracks": default_audio_plan(ms["audio"])}
    initial["subtitles"] = {"tracks": default_subtitle_plan(ms["subtitles"])}

    job = Job.create(name or slugify(source.stem if source.is_file() else source.name), initial)
    probe.save(result, job.dir)
    if result.get("sidecars"):
        audio, subs, fonts = sidecar_plan(result["sidecars"], job)
        job.data["audio"]["external"] = audio
        job.data["subtitles"]["external"] = subs
        job.data["subtitles"]["font_dirs"] = fonts
        job.save()
    return job


def derive_job(ref: Job, source: Path, name: str, trim: list[list[int]] | None = None) -> Job:
    """Fast track: new job seeded from a finished job's decisions (design §18)."""
    job = new_job(source, name=name, trim=trim)
    keep = {k: ref.data[k] for k in ("video", "chapters") if k in ref.data}
    keep["job"] = {"title": ref.data["job"]["title"], "year": ref.data["job"]["year"],
                   "type": job.data["job"]["type"], "derived_from": str(ref.dir)}
    keep["output"] = {k: v for k, v in ref.data["output"].items() if k != "nas_dir"}
    job.data = deep_merge(job.data, keep)
    # Track plans are re-derived from the new probe but inherit languages/titles by position.
    for section in ("audio", "subtitles"):
        old = ref.data[section].get("tracks", [])
        for i, t in enumerate(job.data[section].get("tracks", [])):
            if i < len(old):
                for key in ("action", "title", "default", "language"):
                    t[key] = old[i].get(key, t.get(key))
    job.save()
    return job
