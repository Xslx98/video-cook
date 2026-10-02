"""Risk buckets and stratified frame sampling (design §5–6).

Shots are classified into one risk bucket each, using thresholds that mix
absolute limits with percentiles of the title itself (anime and grainy film
have very different absolute statistics). Frames are then allocated to buckets
by risk weight × duration share, with at least one frame per non-empty bucket
and at most one frame per shot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from videocook.scan import ScanResult, shots

BLACK_N, WHITE_N = 4096 / 65535, 60160 / 65535

BUCKETS = {
    # name: (weight, chapter-4 defects it exposes)
    "dark_flat": (3.0, "banding, blocking in dark gradients"),
    "dark_noisy": (2.0, "noise/grain handling, DCT noise"),
    "bright_gradient": (2.0, "banding in sky/walls, chroma banding"),
    "high_detail": (2.0, "aliasing, ringing/haloing, line quality"),
    "high_motion": (1.5, "blocking, ghosting, combing"),
    "static": (0.5, "credits/text, title cards"),
    "normal": (1.0, "general reference"),
    "blank": (0.0, "black/blank frames (fades, gaps); never sampled"),
}


@dataclass
class Shot:
    start: int
    end: int
    luma: float        # 0..1 within limited range
    dark_flat: float
    flat: float
    edge: float
    noise: float
    motion: float
    oor: float
    luma_range: float = 1.0
    cambi: float = 0.0
    bucket: str = "normal"

    @property
    def length(self) -> int:
        return self.end - self.start


def _norm_luma(v: np.ndarray | float) -> np.ndarray | float:
    return np.clip((v - BLACK_N) / (WHITE_N - BLACK_N), 0, 1)


def summarise_shots(scan: ScanResult) -> list[Shot]:
    d = scan.data
    out = []
    for a, b in shots(scan):
        # ignore transition frames at both ends of longer shots
        lo, hi = (a + 2, b - 2) if b - a > 8 else (a, b)
        sl = slice(lo, hi)
        out.append(Shot(a, b,
                        float(_norm_luma(d["luma"][sl].mean())),
                        float(d["dark_flat"][sl].mean()), float(d["flat"][sl].mean()),
                        float(d["edge"][sl].mean()), float(d["noise"][sl].mean()),
                        float(np.median(d["motion"][sl])), float(d["oor"][sl].mean()),
                        float(np.median(d["luma_max"][sl] - d["luma_min"][sl])),
                        float(d["cambi"][sl].mean())))
    return out


def classify(shots_: list[Shot]) -> dict:
    """Assign buckets in priority order; return the thresholds used."""
    edge = np.array([s.edge for s in shots_])
    noise = np.array([s.noise for s in shots_])
    motion = np.array([s.motion for s in shots_])
    th = {
        "dark_luma": 0.22,
        "dark_flat_share": 0.30,
        "noise_p75": float(np.percentile(noise, 75)),
        "motion_p90": float(np.percentile(motion, 90)),
        "motion_p10": float(np.percentile(motion, 10)),
        "edge_p80": float(np.percentile(edge, 80)),
        "bright_flat_share": 0.55,
    }
    for s in shots_:
        dark = s.luma < th["dark_luma"]
        if s.luma < 0.04 or s.luma_range < 0.05:
            s.bucket = "blank"
        elif dark and s.dark_flat > th["dark_flat_share"]:
            s.bucket = "dark_flat"
        elif dark and s.noise > th["noise_p75"]:
            s.bucket = "dark_noisy"
        elif s.motion > th["motion_p90"]:
            s.bucket = "high_motion"
        elif s.edge > th["edge_p80"]:
            s.bucket = "high_detail"
        elif not dark and s.flat > th["bright_flat_share"]:
            s.bucket = "bright_gradient"
        elif s.motion < th["motion_p10"]:
            s.bucket = "static"
        else:
            s.bucket = "normal"
    return th


def composition(shots_: list[Shot], total_frames: int) -> dict:
    comp = {}
    for b in BUCKETS:
        mem = [s for s in shots_ if s.bucket == b]
        frames = sum(s.length for s in mem)
        comp[b] = {"shots": len(mem), "frames": frames, "share": frames / max(1, total_frames)}
    return comp


def allocate(comp: dict, budget: int) -> dict[str, int]:
    """Budget split by weight × share, ≥1 per non-empty bucket, ≤ shots per bucket."""
    present = {b: c for b, c in comp.items() if c["shots"] and BUCKETS[b][0] > 0}
    alloc = {b: 1 for b in present}
    remaining = budget - len(alloc)
    if remaining <= 0:
        return alloc
    score = {b: BUCKETS[b][0] * c["share"] for b, c in present.items()}
    total = sum(score.values()) or 1
    # largest remainder method
    raw = {b: remaining * s / total for b, s in score.items()}
    for b in raw:
        alloc[b] += int(raw[b])
    left = budget - sum(alloc.values())
    for b in sorted(raw, key=lambda k: raw[k] - int(raw[k]), reverse=True)[:left]:
        alloc[b] += 1
    for b in alloc:
        alloc[b] = min(alloc[b], present[b]["shots"])
    return alloc


KEY_METRIC = {
    "dark_flat": ("cambi", max),
    "dark_noisy": ("noise", max),
    "bright_gradient": ("cambi", max),
    "high_detail": ("edge", max),
    "high_motion": ("motion", max),
    "static": ("edge", max),
    "normal": ("luma", None),
    "blank": ("luma", None),
}


def pick_frames(shots_: list[Shot], alloc: dict[str, int], scan: ScanResult) -> list[dict]:
    """Per bucket: the extreme shot, then shots spread over the timeline."""
    picks: list[dict] = []
    for bucket, k in alloc.items():
        mem = [s for s in shots_ if s.bucket == bucket and s.length >= 12] or \
              [s for s in shots_ if s.bucket == bucket]
        metric, fn = KEY_METRIC[bucket]
        chosen: list[tuple[Shot, str]] = []
        if fn is not None:
            ext = fn(mem, key=lambda s: getattr(s, metric))
            chosen.append((ext, "extreme"))
        rest = [s for s in mem if s not in [c[0] for c in chosen]]
        need = k - len(chosen)
        if need > 0 and rest:
            # representatives spread across the timeline
            pos = np.linspace(0, len(rest) - 1, need + 2)[1:-1] if need < len(rest) else range(len(rest))
            for p in pos:
                chosen.append((rest[int(round(p))], "representative"))
        for s, role in chosen[:k]:
            frame = _best_frame(s, metric if role == "extreme" else None, scan)
            picks.append({"frame": frame, "bucket": bucket, "role": role,
                          "time": frame / scan.fps, "shot": [s.start, s.end],
                          "metrics": {m: round(getattr(s, m), 4) for m in
                                      ("luma", "dark_flat", "flat", "edge", "noise", "motion", "oor", "cambi")}})
    # Banding hotspots: CAMBI finds banding the bucket heuristics miss; make sure the
    # worst shots (scan-resolution CAMBI ≥ 2.5) are always looked at.
    picked = {tuple(p["shot"]) for p in picks}
    cam = scan.data["cambi"]
    peaks: list[tuple[float, Shot]] = []
    for s in shots_:
        if s.bucket == "blank" or (s.start, s.end) in picked or s.length < 6:
            continue
        seg = cam[s.start:s.end]
        if len(seg) and float(seg.max()) >= 3.0:
            peaks.append((float(seg.max()), s))
    for _, s in sorted(peaks, key=lambda t: -t[0])[:2]:
        frame = _best_frame(s, "cambi", scan)
        picks.append({"frame": frame, "bucket": s.bucket, "role": "cambi-hotspot",
                      "time": frame / scan.fps, "shot": [s.start, s.end],
                      "metrics": {m: round(getattr(s, m), 4) for m in
                                  ("luma", "dark_flat", "flat", "edge", "noise", "motion", "oor", "cambi")}})
    picks.sort(key=lambda p: p["frame"])
    return picks


def _best_frame(s: Shot, metric: str | None, scan: ScanResult) -> int:
    lo, hi = (s.start + 2, s.end - 2) if s.length > 8 else (s.start, s.end)
    if metric and metric in scan.data and metric != "luma":
        seg = scan.data[metric][lo:hi]
        if len(seg):
            return int(lo + int(np.argmax(seg)))
    return int((lo + hi) // 2)


def assess(scan: ScanResult, budget: int) -> dict:
    shots_ = summarise_shots(scan)
    th = classify(shots_)
    comp = composition(shots_, scan.num_frames)
    alloc = allocate(comp, budget)
    picks = pick_frames(shots_, alloc, scan)
    d = scan.data
    lum = _norm_luma(d["luma"])
    return {
        "frames": scan.num_frames,
        "fps": scan.fps,
        "duration": scan.num_frames / scan.fps,
        "shots": len(shots_),
        "thresholds": th,
        "composition": comp,
        "allocation": alloc,
        "picks": picks,
        "global": {
            "dark_share": float((lum < th["dark_luma"]).mean()),
            "mean_luma": float(lum.mean()),
            "median_noise": float(np.median(d["noise"])),
            "median_edge": float(np.median(d["edge"])),
            "median_motion": float(np.median(d["motion"])),
            "oor_frames_share": float((d["oor"] > 0.01).mean()),
            "near_duplicate_frames_share": float((d["motion"][1:] < 1e-4).mean()),
            "cambi_median": float(np.median(d["cambi"])),
            "cambi_p95": float(np.percentile(d["cambi"], 95)),
            "cambi_max": float(d["cambi"].max()),
        },
        "shot_list": [asdict(s) for s in shots_],
    }
