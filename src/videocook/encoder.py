"""x265 10-bit parameters in three layers (design §12).

1. base     — from the guides' general BDRip settings (chapter 7), VBV removed.
2. content  — anime / live_action / webrip overlay.
3. tier     — CRF by quality tier, per content type.
Colour/HDR flags come from the probe, never from hand edits. `extra` in the job
can append or override any flag.
"""

from __future__ import annotations

import shlex

BASE = [
    "--preset", "slower", "--output-depth", "10", "--profile", "main10",
    "--deblock", "-1:-1", "--ctu", "32", "--qg-size", "16", "--limit-tu", "0", "--rskip", "0",
    "--max-tu-size", "16", "--me", "3", "--subme", "5", "--merange", "32", "--b-intra",
    "--no-amp", "--ref", "5", "--weightb", "--keyint", "240", "--min-keyint", "1",
    "--bframes", "8", "--aq-mode", "3", "--rd", "5", "--rdoq-level", "1", "--rc-lookahead", "80",
    "--scenecut", "40", "--no-open-gop", "--no-sao", "--no-strong-intra-smoothing",
    "--pbratio", "1.2", "--cbqpoffs", "-3", "--crqpoffs", "-3",
]

CONTENT = {
    # Guides: general BDRip example (anime is the guides' home turf).
    "anime": ["--aq-strength", "0.8", "--psy-rd", "1.7", "--psy-rdoq", "0.8", "--qcomp", "0.65"],
    # Guides: "film-like, keep the grain" example; psy-rdoq 1.0 and deblock -2 are our supplement.
    "live_action": ["--aq-strength", "0.7", "--psy-rd", "2.0", "--psy-rdoq", "1.0", "--qcomp", "0.72",
                    "--ipratio", "1.2", "--pbratio", "1.3", "--deblock", "-2:-2"],
    # Guides: WebRip example (source pre-cleaned, compression-oriented).
    "webrip": ["--aq-mode", "1", "--aq-strength", "0.75", "--psy-rd", "1.5", "--psy-rdoq", "0.5",
               "--rdoq-level", "2", "--cbqpoffs", "-2", "--crqpoffs", "-2", "--bframes", "10",
               "--no-rect", "--limit-tu", "3", "--tu-intra-depth", "3", "--tu-inter-depth", "3",
               "--qcomp", "0.65"],
}

CRF = {
    "anime": {"archival": 15.0, "standard": 18.0, "compact": 21.0},
    "live_action": {"archival": 16.0, "standard": 19.0, "compact": 22.0},
    "webrip": {"archival": 17.0, "standard": 19.5, "compact": 22.0},
}

COLOURPRIM = {"bt709": "bt709", "bt2020": "bt2020", "smpte170m": "smpte170m", "bt470bg": "bt470bg"}
TRANSFER = {"bt709": "bt709", "smpte2084": "smpte2084", "arib-std-b67": "arib-std-b67",
            "smpte170m": "smpte170m", "bt470bg": "bt470bg"}
MATRIX = {"bt709": "bt709", "bt2020nc": "bt2020nc", "smpte170m": "smpte170m", "bt470bg": "bt470bg"}
CHROMALOC = {"left": "0", "center": "1", "topleft": "2"}


def colour_args(colour: dict, height: int) -> list[str]:
    hd = height >= 720
    prim = COLOURPRIM.get(colour.get("primaries") or "", "bt709" if hd else "smpte170m")
    trc = TRANSFER.get(colour.get("transfer") or "", "bt709" if hd else "smpte170m")
    mat = MATRIX.get(colour.get("matrix") or "", "bt709" if hd else "smpte170m")
    args = ["--colorprim", prim, "--transfer", trc, "--colormatrix", mat, "--range", "limited"]
    loc = CHROMALOC.get(colour.get("chroma_location") or "")
    if loc is not None:
        args += ["--chromaloc", loc]
    return args


def hdr_args(hdr: dict, hdr10plus_json: str | None = None) -> list[str]:
    if not hdr.get("hdr10"):
        return []
    args = ["--hdr10", "--hdr10-opt", "--repeat-headers"]
    static = hdr.get("static") or {}
    if static.get("master_display"):
        args += ["--master-display", static["master_display"]]
    if static.get("max_cll"):
        args += ["--max-cll", static["max_cll"]]
    if hdr10plus_json:
        args += ["--dhdr10-info", hdr10plus_json]
    return args


def merge(args: list[str], override: list[str]) -> list[str]:
    """Later flags win: drop earlier occurrences of any flag present in `override`."""
    def flags(seq: list[str]) -> dict[str, int]:
        return {t: i for i, t in enumerate(seq) if t.startswith("--")}

    over = flags(override)
    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        has_val = i + 1 < len(args) and not args[i + 1].startswith("--")
        neg = "--no-" + tok[2:] if tok.startswith("--") else None
        pos = "--" + tok[5:] if tok.startswith("--no-") else None
        if tok in over or neg in over or pos in over:
            i += 2 if has_val else 1
            continue
        out.append(tok)
        if has_val:
            out.append(args[i + 1])
        i += 2 if has_val else 1
    return out + override


def build(video_cfg: dict, colour: dict, hdr: dict, height: int,
          qpfile: str | None = None, hdr10plus_json: str | None = None) -> list[str]:
    content = video_cfg.get("content", "live_action")
    tier = video_cfg.get("tier", "standard")
    crf = video_cfg.get("crf") or CRF[content][tier]
    args = merge(BASE, CONTENT[content])
    args = merge(args, ["--crf", f"{crf:g}"])
    args = merge(args, colour_args(colour, height))
    args = merge(args, hdr_args(hdr, hdr10plus_json))
    if qpfile:
        args = merge(args, ["--qpfile", qpfile])
    extra = video_cfg.get("x265_extra")
    if extra:
        args = merge(args, shlex.split(extra))
    return args


def preview(video_cfg: dict, colour: dict, hdr: dict, height: int) -> list[str]:
    """Final parameters (test clips must use the real settings)."""
    return build(video_cfg, colour, hdr, height)
