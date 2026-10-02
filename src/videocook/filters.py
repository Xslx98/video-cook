"""Filter building blocks used by generated .vpy scripts.

Philosophy from the VCB-S guides (chapter 9 §4), implemented with current
tools (VapourSynth R80, API 4 plugins from PyPI, vs-jetpack): denoise first,
then deband and anti-aliasing are both computed from the denoised clip and
merged with LimitFilter (deband owns flat areas, AA owns lines).

The guides' parameter values are kept where an equivalent exists:
f3kdb thresholds keep their classic 14-bit meaning (64 ≈ one 8-bit code value)
and are converted for vszip.Deband, the API 4 port of the same algorithm.
Additional, non-guide options are labelled "frontier" below.
Guides: https://guides.vcb-s.com/ (chapters 6, 8, 9, 10).
"""

from __future__ import annotations

import functools
import os

import vapoursynth as vs

core = vs.core

# AI model storage is shared by all jobs (not the per-cwd default of vs-jetpack).
os.environ.setdefault("VSSCALE_GLOBAL", "1")


def _has(ns: str) -> bool:
    return hasattr(core, ns)


# --- precision ---------------------------------------------------------------

def to16(clip: vs.VideoNode) -> vs.VideoNode:
    if clip.format.bits_per_sample == 16 and clip.format.sample_type == vs.INTEGER:
        return clip
    return core.fmtc.bitdepth(clip, bits=16)


def to10(clip: vs.VideoNode) -> vs.VideoNode:
    """Final output for x265 10-bit (fmtc default error-diffusion dither)."""
    return core.fmtc.bitdepth(clip, bits=10)


# --- geometry ----------------------------------------------------------------

def crop(clip: vs.VideoNode, left: int = 0, top: int = 0, right: int = 0, bottom: int = 0) -> vs.VideoNode:
    if not any((left, top, right, bottom)):
        return clip
    sub_w = 1 << clip.format.subsampling_w
    sub_h = 1 << clip.format.subsampling_h
    if any(v % sub_w for v in (left, right)) or any(v % sub_h for v in (top, bottom)):
        raise ValueError("crop values must respect chroma subsampling (even numbers for 4:2:0)")
    return core.std.Crop(clip, left, right, top, bottom)


# --- LimitFilter (chapter 8 §4) -----------------------------------------------

def limit(flt: vs.VideoNode, src: vs.VideoNode, thr: float = 0.55, elast: float = 1.5,
          planes: list[int] | None = None) -> vs.VideoNode:
    """mvsfunc.LimitFilter semantics (thr in 8-bit units) via vszip's API 4 implementation."""
    return core.vszip.LimitFilter(flt, src, dark_thr=thr, bright_thr=thr, elast=elast,
                                  planes=planes if planes is not None else list(range(flt.format.num_planes)))


# --- denoise (chapter 9 §1 + frontier) ----------------------------------------

DENOISE_PRESETS = {
    "light": {"method": "nlm", "h": 1.5},
    "medium": {"method": "nlm", "h": 3.0},
    "strong": {"method": "nlm", "h": 5.0},
}


def _nlm(clip: vs.VideoNode, h: float) -> vs.VideoNode:
    """NLMeans on the best available device: CUDA → OpenCL (Intel/NVIDIA/AMD) → CPU (ISPC)."""
    for ns in ("nlm_cuda", "vszipcl", "nlm_ispc"):
        if _has(ns):
            return getattr(core, ns).NLMeans(clip, d=0, wmode=3, h=h)
    raise RuntimeError("no NLMeans plugin available")


def denoise(clip: vs.VideoNode, preset: str | None = None, method: str | None = None,
            h: float | None = None, sigma: float | None = None, strength: float | None = None,
            tr: int | None = None) -> vs.VideoNode:
    """Denoise. Guide methods: nlm (NLMeans, h), dfttest (sigma), bm3d (sigma).
    Frontier: mc_degrain (motion-compensated temporal, MVTools via vs-jetpack),
    dpir (AI deblock/denoise for compressed sources; strength in 8-bit units).
    GPU builds (CUDA on NVIDIA, Vulkan elsewhere) are picked automatically."""
    if preset in (None, "none", "off") and method is None:
        return clip
    cfg = dict(DENOISE_PRESETS.get(preset or "", {}))
    method = method or cfg.get("method", "nlm")
    if method == "nlm":
        return _nlm(clip, h if h is not None else cfg.get("h", 3.0))
    if method == "dfttest":
        import vsdenoise

        return vsdenoise.DFTTest().denoise(clip, sigma=sigma if sigma is not None else 8.0)
    if method == "bm3d":
        import vsdenoise

        sig = sigma if sigma is not None else 3.0
        f = core.resize.Bicubic(clip, format=vs.YUV444PS)  # GPU backends want float 4:4:4
        if _has("bm3dcuda"):
            out = vsdenoise.bm3d(f, sigma=sig, tr=tr)        # jetpack picks the CUDA backend
        else:
            out = core.bm3dvk.BM3Dv2(f, sigma=[sig] * 3, radius=tr or 0)  # Vulkan (Arc etc.)
        return core.resize.Bicubic(out, format=clip.format.id)
    if method == "mc_degrain":
        import vsdenoise

        return vsdenoise.mc_degrain(clip, tr=tr or 2)
    if method in ("dpir", "dpir_deblock", "dpir_denoise"):
        import vsdenoise

        mode = vsdenoise.dpir.DENOISE if method == "dpir_denoise" else vsdenoise.dpir.DEBLOCK
        out = mode(core.resize.Bicubic(clip, format=vs.YUV444PS), strength=strength or 10)
        return core.resize.Bicubic(out, format=clip.format.id)
    raise ValueError(f"unknown denoise method {method!r}")


# --- deband (chapter 9 §2) -----------------------------------------------------

# Two f3kdb passes: (range, y, cb, cr) in classic 14-bit threshold units.
DEBAND_PRESETS = {
    "light": ((12, 48, 32, 32), (24, 32, 24, 24)),
    "medium": ((12, 64, 48, 48), (24, 48, 32, 32)),
    "strong": ((12, 96, 48, 48), (24, 72, 32, 32)),
}


def _f3k(clip: vs.VideoNode, rng: int, y: float, cb: float, cr: float) -> vs.VideoNode:
    thr = [t * 255 / 16383 for t in (y, cb, cr)]  # 14-bit f3kdb units → 8-bit units
    return core.vszip.Deband(clip, range=rng, thr=thr, grain=[0, 0])


def deband(clip: vs.VideoNode, preset: str = "medium", limit_thr: float = 0.55,
           limit_elast: float = 1.5, passes: tuple | None = None, method: str = "f3k",
           placebo_thr: float | None = None) -> vs.VideoNode:
    """Guides: two-pass f3kdb without grain, limited against the input.
    Frontier: method="placebo" uses libplacebo's deband (GPU) instead."""
    if preset in (None, "none", "off"):
        return clip
    if method == "placebo":
        import vsdeband

        db = vsdeband.placebo_deband(clip, thr=placebo_thr or {"light": 2.0, "medium": 3.0,
                                                               "strong": 4.5}[preset])
    else:
        p1, p2 = passes or DEBAND_PRESETS[preset]
        db = _f3k(_f3k(clip, *p1), *p2)
    return limit(db, clip, thr=limit_thr, elast=limit_elast)


# --- anti-aliasing (chapter 9 §3) -----------------------------------------------

def _double_aa(y: vs.VideoNode, interp) -> vs.VideoNode:
    w, h = y.width, y.height
    aa = interp(y)
    aa = core.fmtc.resample(aa, w, h, 0, -0.5).std.Transpose()
    aa = interp(aa)
    aa = core.fmtc.resample(aa, h, w, 0, -0.5).std.Transpose()
    return core.zsmooth.Repair(aa, y, 2)


def aa_luma(clip: vs.VideoNode, method: str = "nnedi3") -> vs.VideoNode:
    """Anti-aliased luma (GRAY) via double-height interpolation in both directions."""
    y = core.std.ShufflePlanes(clip, 0, vs.GRAY)
    if method == "nnedi3":
        interp = functools.partial(core.znedi3.nnedi3, field=1, dh=True, nsize=3, nns=2, qual=2)
        return _double_aa(y, interp)
    if method == "eedi3":
        import vsaa

        return core.zsmooth.Repair(vsaa.EEDI3(alpha=0.5, beta=0.2, gamma=20.0, nrad=3, mdis=30)
                                   .antialias(y), y, 2)
    raise ValueError(f"unknown AA method {method!r} (nnedi3 | eedi3)")


def merge_aa(base: vs.VideoNode, aa_y: vs.VideoNode, thr: float = 1.0, elast: float = 1.5) -> vs.VideoNode:
    """Fold AA'd lines into the (debanded) base: LimitFilter keeps AA where it differs little."""
    base_y = core.std.ShufflePlanes(base, 0, vs.GRAY)
    merged_y = limit(base_y, aa_y, thr=thr, elast=elast, planes=[0])
    return core.std.ShufflePlanes([merged_y, base], [0, 1, 2], vs.YUV)


# --- 30 fps / interlaced sources (chapter 10) ---------------------------------

def deinterlace(clip: vs.VideoNode, tff: bool = True, double_rate: bool = False) -> vs.VideoNode:
    from vsdeinterlace import QTempGaussMC

    bobbed = QTempGaussMC().bob(clip, tff=tff)  # double rate
    return bobbed if double_rate else bobbed[::2]


def ivtc(clip: vs.VideoNode, tff: bool = True) -> vs.VideoNode:
    """Field matching + decimation; combed leftovers are deinterlaced with QTGMC."""
    from vsdeinterlace import QTempGaussMC

    matched = core.vivtc.VFM(clip, 1 if tff else 0)
    deint = QTempGaussMC().bob(matched, tff=tff)[::2]  # single rate, aligned with `matched`

    def pick(n, f, clip, deint):
        return deint if f.props["_Combed"] > 0 else clip

    fixed = core.std.FrameEval(matched, functools.partial(pick, clip=matched, deint=deint),
                               prop_src=matched)
    return core.vivtc.VDecimate(fixed)


# --- inspection helpers --------------------------------------------------------

def amplified_diff(a: vs.VideoNode, b: vs.VideoNode, gain: int = 10) -> vs.VideoNode:
    return core.std.Expr([core.std.MakeDiff(a, b)], [f"x 32768 - {gain} * 32768 +", "32768"])


# --- the standard chain ----------------------------------------------------------

def standard_chain(src: vs.VideoNode, cfg: dict) -> vs.VideoNode:
    """Apply the job's [video.filters] table in the guides' order.

    cfg keys (all optional): crop {left,top,right,bottom}, field ("ivtc"|"deint"),
    tff, denoise {preset|method,h,sigma,strength,tr}, deband {preset,method,limit_thr,
    limit_elast}, aa {method, merge_thr, merge_elast}.
    """
    clip = src
    field = cfg.get("field")
    if field == "ivtc":
        clip = ivtc(clip, tff=cfg.get("tff", True))
    elif field == "deint":
        clip = deinterlace(clip, tff=cfg.get("tff", True))
    if cfg.get("crop"):
        clip = crop(clip, **cfg["crop"])
    clip = to16(clip)
    nr = denoise(clip, **cfg["denoise"]) if cfg.get("denoise") else clip
    out = deband(nr, **cfg["deband"]) if cfg.get("deband") else nr
    if cfg.get("aa"):
        aa_cfg = dict(cfg["aa"])
        aa_y = aa_luma(nr, aa_cfg.pop("method", "nnedi3"))
        out = merge_aa(out, aa_y, thr=aa_cfg.get("merge_thr", 1.0), elast=aa_cfg.get("merge_elast", 1.5))
    return out


# --- colour tagging ----------------------------------------------------------------

_MATRIX = {"bt709": 1, "bt470bg": 5, "smpte170m": 6, "bt2020nc": 9}
_TRANSFER = {"bt709": 1, "bt470bg": 5, "smpte170m": 6, "smpte2084": 16, "arib-std-b67": 18}
_PRIMARIES = {"bt709": 1, "bt470bg": 5, "smpte170m": 6, "bt2020": 9}


def tag_colour(clip: vs.VideoNode, colour: dict) -> vs.VideoNode:
    """Set _Matrix/_Transfer/_Primaries/_ColorRange from the probe (or HD/SD defaults)
    so colour-aware filters (RGB models, resizers) never guess."""
    hd = clip.height >= 720
    m = _MATRIX.get(colour.get("matrix") or "", 1 if hd else 6)
    t = _TRANSFER.get(colour.get("transfer") or "", 1 if hd else 6)
    p = _PRIMARIES.get(colour.get("primaries") or "", 1 if hd else 6)
    rng = 0 if colour.get("range") == "pc" else 1  # VS: 0 = full, 1 = limited
    return core.std.SetFrameProps(clip, _Matrix=m, _Transfer=t, _Primaries=p, _ColorRange=rng)
