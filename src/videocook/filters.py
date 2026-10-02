"""Filter building blocks used by generated .vpy scripts.

The processing order follows the VCB-S guides (chapter 9 §4): denoise first,
then deband and anti-aliasing are both computed from the denoised clip and
merged with LimitFilter (deband owns flat areas, AA owns lines). Parameter
presets are starting points taken from the guides' examples; every function
accepts explicit overrides so the job can fine-tune them.
Guides: https://guides.vcb-s.com/ (chapters 6, 8, 9, 10).
"""

from __future__ import annotations

import functools

import mvsfunc as mvf
import vapoursynth as vs

core = vs.core


# --- precision ---------------------------------------------------------------

def to16(clip: vs.VideoNode) -> vs.VideoNode:
    return clip if clip.format.bits_per_sample == 16 else core.fmtc.bitdepth(clip, bits=16)


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


# --- denoise (chapter 9 §1) ----------------------------------------------------

DENOISE_PRESETS = {
    "light": {"method": "nlm", "h": 1.5},
    "medium": {"method": "nlm", "h": 3.0},
    "strong": {"method": "nlm", "h": 5.0},
}


def denoise(clip: vs.VideoNode, preset: str | None = None, method: str | None = None,
            h: float | None = None, sigma: float | None = None) -> vs.VideoNode:
    """Spatial denoise. nlm = nlm_ispc NLMeans (guides' default), dfttest, bm3d."""
    if preset in (None, "none", "off") and method is None:
        return clip
    cfg = dict(DENOISE_PRESETS.get(preset or "", {}))
    method = method or cfg.get("method", "nlm")
    if method == "nlm":
        return core.nlm_ispc.NLMeans(clip, d=0, wmode=3, h=h if h is not None else cfg.get("h", 3.0))
    if method == "dfttest":
        return core.dfttest.DFTTest(clip, sigma=sigma if sigma is not None else 8.0, tbsize=1)
    if method == "bm3d":
        return mvf.BM3D(clip, sigma=sigma if sigma is not None else 3.0)
    raise ValueError(f"unknown denoise method {method!r}")


# --- deband (chapter 9 §2, chapter 8 LimitFilter) -------------------------------

# Two passes of neo_f3kdb: (range, y, cb, cr) for pass 1 and pass 2.
DEBAND_PRESETS = {
    "light": ((12, 48, 32, 32), (24, 32, 24, 24)),
    "medium": ((12, 64, 48, 48), (24, 48, 32, 32)),
    "strong": ((12, 96, 48, 48), (24, 72, 32, 32)),
}


def deband(clip: vs.VideoNode, preset: str = "medium", limit_thr: float = 0.55,
           limit_elast: float = 1.5, passes: tuple | None = None) -> vs.VideoNode:
    """Two-pass f3kdb without grain, limited against the input by LimitFilter."""
    if preset in (None, "none", "off"):
        return clip
    p1, p2 = passes or DEBAND_PRESETS[preset]
    db = core.neo_f3kdb.Deband(clip, range=p1[0], y=p1[1], cb=p1[2], cr=p1[3],
                               grainy=0, grainc=0, output_depth=16)
    db = core.neo_f3kdb.Deband(db, range=p2[0], y=p2[1], cb=p2[2], cr=p2[3],
                               grainy=0, grainc=0, output_depth=16)
    return mvf.LimitFilter(db, clip, thr=limit_thr, elast=limit_elast, planes=[0, 1, 2])


# --- anti-aliasing (chapter 9 §3) ---------------------------------------------

def _double_aa(y: vs.VideoNode, interp) -> vs.VideoNode:
    w, h = y.width, y.height
    aa = interp(y)
    aa = core.fmtc.resample(aa, w, h, 0, -0.5).std.Transpose()
    aa = interp(aa)
    aa = core.fmtc.resample(aa, h, w, 0, -0.5).std.Transpose()
    return core.rgvs.Repair(aa, y, 2)


def aa_luma(clip: vs.VideoNode, method: str = "nnedi3") -> vs.VideoNode:
    """Anti-aliased luma (GRAY) via double-height interpolation in both directions."""
    y = core.std.ShufflePlanes(clip, 0, vs.GRAY)
    if method == "nnedi3":
        interp = functools.partial(core.znedi3.nnedi3, field=1, dh=True, nsize=3, nns=2, qual=2)
    elif method == "eedi2":
        interp = functools.partial(core.eedi2.EEDI2, field=1, mthresh=10, lthresh=20,
                                   vthresh=20, maxd=24, nt=50)
    elif method == "eedi3":
        interp = functools.partial(core.eedi3m.EEDI3, field=1, dh=True, alpha=0.5, beta=0.2,
                                   gamma=20.0, nrad=3, mdis=30)
    else:
        raise ValueError(f"unknown AA method {method!r}")
    return _double_aa(y, interp)


def merge_aa(base: vs.VideoNode, aa_y: vs.VideoNode, thr: float = 1.0, elast: float = 1.5) -> vs.VideoNode:
    """Fold AA'd lines into the (debanded) base: LimitFilter keeps AA where it differs little."""
    base_y = core.std.ShufflePlanes(base, 0, vs.GRAY)
    merged_y = mvf.LimitFilter(base_y, aa_y, thr=thr, elast=elast)
    return core.std.ShufflePlanes([merged_y, base], [0, 1, 2], vs.YUV)


# --- 30 fps / interlaced sources (chapter 10) ---------------------------------

def deinterlace(clip: vs.VideoNode, tff: bool = True, preset: str = "Slower",
                double_rate: bool = False) -> vs.VideoNode:
    import havsfunc as haf

    return haf.QTGMC(clip, TFF=tff, Preset=preset, FPSDivisor=1 if double_rate else 2)


def ivtc(clip: vs.VideoNode, tff: bool = True, qtgmc_preset: str = "Slower") -> vs.VideoNode:
    """Field matching + decimation; combed leftovers are deinterlaced with QTGMC."""
    import havsfunc as haf

    matched = core.vivtc.VFM(clip, 1 if tff else 0)
    deint = haf.QTGMC(matched, TFF=tff, Preset=qtgmc_preset, FPSDivisor=2)

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
    tff, denoise {preset|method,h,sigma}, deband {preset,limit_thr,limit_elast},
    aa {method, merge_thr, merge_elast}.
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
