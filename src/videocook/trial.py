"""Stage 4b — encode check: short clips with the final x265 settings (design §7).

* risk clips: ~10 s around the riskiest sampled frames → visual comparison + metrics
* uniform clips: short segments spread over the title → bitrate → size estimate
Metrics (reference only, never a gate): SSIMULACRA2 (perceptual, 0–100; ≥90 is
visually lossless territory, 80–90 very good), XPSNR (psychovisual PSNR, dB) and
CAMBI of encoder input vs output (positive delta = banding added by x265).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from videocook import encoder, images
from videocook.compare import picks_for
from videocook.job import Job
from videocook.sampling import BUCKETS
from videocook.script import load_outputs, script_path
from videocook.toolchain import tool


def _encode_range(vpy: Path, start: int, end: int, x265_args: list[str], out: Path, log: Path) -> None:
    """vspipe [start, end] (inclusive) | x265 → out (.hevc)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as lf:
        pipe = subprocess.Popen([str(tool("vspipe")), "-c", "y4m", "-s", str(start), "-e", str(end),
                                 str(vpy), "-"], stdout=subprocess.PIPE, stderr=lf)
        enc = subprocess.run([str(tool("x265")), "--y4m", "--input", "-", *x265_args,
                              "--output", str(out)], stdin=pipe.stdout, stderr=lf, stdout=lf)
        pipe.stdout.close()
        pipe.wait()
    if enc.returncode != 0 or pipe.returncode != 0:
        raise RuntimeError(f"test encode failed, see {log}")


def _metrics(res, start: int, end: int, encoded: Path, colour: dict) -> dict:
    """SSIMULACRA2 / XPSNR / CAMBI between the encoder input and the encoded clip."""
    import vapoursynth as vs

    from videocook import images

    core = vs.core
    ref = res[start:end + 1]
    enc = core.lsmas.LWLibavSource(str(encoded), cachefile=str(encoded.with_suffix(".lwi")))
    if enc.num_frames != ref.num_frames:
        return {"error": f"frame mismatch {enc.num_frames} vs {ref.num_frames}"}
    enc = core.resize.Point(enc, format=ref.format.id)
    xp = core.vszip.XPSNR(ref, enc)
    to_rgbs = lambda c: core.resize.Bicubic(images.to_rgb(c, colour), format=vs.RGBS)  # noqa: E731
    s2 = core.vszip.SSIMULACRA2(to_rgbs(ref), to_rgbs(enc))
    gray = lambda c: core.resize.Point(c, format=vs.GRAY10)  # noqa: E731
    cam_ref = core.akarin.Cambi(gray(ref))
    cam_enc = core.akarin.Cambi(gray(enc))
    vals: dict[str, list[float]] = {"ssimu2": [], "xpsnr_y": [], "cambi_in": [], "cambi_out": []}
    for fx, fs, fa, fb in zip(xp.frames(), s2.frames(), cam_ref.frames(), cam_enc.frames()):
        vals["xpsnr_y"].append(fx.props["XPSNR_Y"])
        vals["ssimu2"].append(fs.props["SSIMULACRA2"])
        vals["cambi_in"].append(fa.props["CAMBI"])
        vals["cambi_out"].append(fb.props["CAMBI"])
    import numpy as np

    s2v = np.array(vals["ssimu2"])
    return {"ssimu2_mean": round(float(s2v.mean()), 2), "ssimu2_p5": round(float(np.percentile(s2v, 5)), 2),
            "xpsnr_y": round(float(np.mean([v for v in vals["xpsnr_y"] if np.isfinite(v)] or [0])), 2),
            "cambi_in": round(float(np.mean(vals["cambi_in"])), 2),
            "cambi_out": round(float(np.mean(vals["cambi_out"])), 2)}


def choose_risk_clips(picks: list[dict], count: int, length: int, total: int) -> list[dict]:
    order = sorted(picks, key=lambda p: (-BUCKETS[p["bucket"]][0], p["role"] != "extreme"))
    chosen: list[dict] = []
    seen_buckets: set[str] = set()
    for p in order:  # one per bucket first, then fill
        if p["bucket"] not in seen_buckets and len(chosen) < count:
            chosen.append(p)
            seen_buckets.add(p["bucket"])
    for p in order:
        if len(chosen) >= count:
            break
        if p not in chosen:
            chosen.append(p)
    clips = []
    for p in chosen:
        a = max(0, min(p["frame"] - length // 2, total - length))
        clips.append({"start": a, "end": min(total, a + length) - 1, "pick": p})
    return sorted(clips, key=lambda c: c["start"])


def run(job: Job, unit_id: str | None = None, quick: bool = False) -> dict:
    unit_id = unit_id or job.units()[0]["id"]
    vpy = script_path(job, unit_id)
    res, src = load_outputs(vpy)
    facts = job.stream_facts()
    fps = float(res.fps) if res.fps.denominator else facts["fps"]
    args = encoder.build(job.data["video"], facts["colour"], facts["hdr"], res.height)
    tcfg = job.data["trial"]
    out = job.sub("trial", "encode", unit_id)
    length = int(tcfg.get("clip_seconds", 10) * fps)
    picks = picks_for(job, unit_id)
    scale = res.num_frames / src.num_frames  # IVTC changes frame counts
    for p in picks:
        p["frame"] = min(res.num_frames - 1, round(p["frame"] * scale))
    risk = choose_risk_clips(picks, 1 if quick else tcfg.get("clips", 4), length, res.num_frames)

    uniform = []
    if not quick:
        n_uni = 10
        seg = int(3 * fps)
        for k, c in enumerate(np.linspace(0.05, 0.95, n_uni)):
            a = int(c * (res.num_frames - seg))
            uniform.append({"start": a, "end": a + seg - 1})

    report: dict = {"unit": unit_id, "x265": args, "risk": [], "uniform": []}
    rgb_src = images.to_rgb(src, facts["colour"])
    for i, c in enumerate(risk, 1):
        hevc = out / f"risk{i:02d}_{c['start']}-{c['end']}.hevc"
        _encode_range(vpy, c["start"], c["end"], args, hevc, out / f"risk{i:02d}.log")
        size = hevc.stat().st_size
        frames = c["end"] - c["start"] + 1
        entry = {**{k: c[k] for k in ("start", "end")}, "bucket": c["pick"]["bucket"],
                 "kbps": round(size * 8 / (frames / fps) / 1000),
                 "metrics": _metrics(res, c["start"], c["end"], hevc, facts["colour"])}
        entry["image"] = str(_compare_image(hevc, c, rgb_src, facts, out / f"risk{i:02d}.png", job))
        report["risk"].append(entry)
    for i, c in enumerate(uniform, 1):
        hevc = out / f"uniform{i:02d}.hevc"
        _encode_range(vpy, c["start"], c["end"], args, hevc, out / f"uniform{i:02d}.log")
        frames = c["end"] - c["start"] + 1
        report["uniform"].append({**c, "kbps": round(hevc.stat().st_size * 8 / (frames / fps) / 1000)})
    duration = res.num_frames / fps
    if report["uniform"]:
        kbps = float(np.mean([u["kbps"] for u in report["uniform"]]))
        report["estimate"] = {"video_kbps": round(kbps),
                              "video_gib": round(kbps * 1000 / 8 * duration / 2**30, 2),
                              "duration_min": round(duration / 60, 1),
                              "note": "mean of uniform 3 s clips; keyframe overhead makes short clips run slightly high"}
    (out / "trial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "report.md").write_text(report_md(report), encoding="utf-8")
    return report


def _compare_image(hevc: Path, clip: dict, rgb_src, facts: dict, path: Path, job: Job) -> Path:
    import vapoursynth as vs

    enc = vs.core.lsmas.LWLibavSource(str(hevc), cachefile=str(hevc.with_suffix(".lwi")))
    p = clip["pick"]
    n_src = p["frame"]
    n_enc = n_src - clip["start"]
    a = images.frame_array(rgb_src, n_src)
    b = images.frame_array(images.to_rgb(enc, facts["colour"]), n_enc)
    x, y, cw, ch = p["crop_box"]
    crop = job.data["video"].get("filters", {}).get("crop") or {}
    bx, by = max(0, x - crop.get("left", 0)), max(0, y - crop.get("top", 0))
    zoom = p["zoom"]
    sa = images.crop_zoom(a, x, y, cw, ch, zoom)
    sb = images.crop_zoom(b, bx, by, cw, ch, zoom)
    if sa.shape != sb.shape:
        sb = np.zeros_like(sa)
    row = images.hstack(images.label(sa, f"source f{n_src}"), images.label(sb, "x265"),
                        images.label(images.amplified_diff(sb, sa), "difference x8"))
    if p["bucket"] in ("dark_flat", "dark_noisy"):
        row = images.vstack(row, images.hstack(images.label(images.boost_dark(sa), "source (boosted)"),
                                               images.label(images.boost_dark(sb), "x265 (boosted)")))
    images.save_png(row, path)
    return path


def report_md(r: dict) -> str:
    lines = [f"# Trial encode — {r['unit']}", "", "x265 " + " ".join(r["x265"]), "",
             "| clip | frames | bucket | kbps | SSIMULACRA2 mean / p5 | XPSNR-Y dB | CAMBI in → out | image |",
             "|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(r["risk"], 1):
        m = c.get("metrics") or {}
        lines.append(f"| risk{i:02d} | {c['start']}-{c['end']} | {c['bucket']} | {c['kbps']} | "
                     f"{m.get('ssimu2_mean', '')} / {m.get('ssimu2_p5', '')} | {m.get('xpsnr_y', '')} | "
                     f"{m.get('cambi_in', '')} → {m.get('cambi_out', '')} | `{Path(c['image']).name}` |")
    lines += ["", "Metrics are references, not gates: SSIMULACRA2 ≥ 90 ≈ visually lossless, "
              "80–90 very good; a CAMBI increase means x265 added banding (lower CRF / raise aq-strength)."]
    if r.get("estimate"):
        e = r["estimate"]
        lines += ["", f"**Estimated video size: {e['video_gib']} GiB** "
                      f"({e['video_kbps']} kbps over {e['duration_min']} min; {e['note']})"]
    return "\n".join(lines) + "\n"
