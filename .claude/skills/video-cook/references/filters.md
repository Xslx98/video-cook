# Filters: `[video.filters]` in job.toml

Implementation: `src/videocook/filters.py` (`standard_chain`). We inherit the
guides' *thinking*, not their exact tools: the chain follows chapter 9 §4 —
**field handling → crop → 16-bit → denoise → (deband ‖ AA, both from the
denoised clip) → LimitFilter merge → 10-bit** — implemented with VapourSynth
R80, API 4 plugin wheels and vs-jetpack. Frontier options are marked; use them
when the trial results justify it.

```toml
[video.filters]
crop = { left = 0, top = 140, right = 0, bottom = 140 }   # even numbers for 4:2:0
field = "ivtc"            # or "deint" (QTGMC single rate); omit for progressive
tff = true
denoise = { preset = "light" }                       # nlm h 1.5 / 3 / 5
# denoise = { method = "dfttest", sigma = 8 }
# denoise = { method = "bm3d", sigma = 3, tr = 1 }   # GPU: CUDA or Vulkan
# denoise = { method = "mc_degrain", tr = 2 }        # frontier: motion-compensated temporal
# denoise = { method = "dpir", strength = 10 }       # frontier: AI deblock (dpir_denoise for noise)
deband = { preset = "medium" }                       # light / medium / strong
# deband = { preset = "medium", method = "placebo" } # frontier: libplacebo deband
aa = { method = "nnedi3" }                           # nnedi3 / eedi3; merge_thr 1.0, merge_elast 1.5
```

No filters at all (and 10-bit source) → the source passes through untouched
apart from crop; 8-bit sources are converted to 10-bit.

## Methods, their origin and backends

| Key | Method | Implementation | Origin |
|---|---|---|---|
| deband | f3k presets | `vszip.Deband` (API 4 port of f3kdb), thresholds kept in classic f3kdb units: light (12,48/32/32)+(24,32/24/24), medium (12,64/48/48)+(24,48/32/32), strong (12,96/48/48)+(24,72/32/32); grain 0 | guides ch. 9 §2 (light is ours) |
| deband | limit | `vszip.LimitFilter` thr 0.55 elast 1.5 vs. input | guides ch. 8 §4 |
| deband | placebo | libplacebo deband via vs-jetpack | frontier |
| denoise | nlm | NLMeans d=0 wmode=3; CUDA → OpenCL (Arc/NVIDIA) → CPU ISPC | guides ch. 9 §1 |
| denoise | dfttest | dfttest2 via vs-jetpack | guides ch. 9 §1 |
| denoise | bm3d | BM3D CUDA (NVIDIA) or Vulkan (`bm3dvk`, Arc), float 4:4:4 | guides ch. 9 / modern backends |
| denoise | mc_degrain | MVTools degrain via vs-jetpack | frontier (temporal) |
| denoise | dpir | DPIR deblock/denoise network via vs-mlrt (TensorRT on NVIDIA, OpenVINO elsewhere) | frontier (AI) |
| aa | nnedi3 | znedi3 double-height both directions + zsmooth Repair 2 | guides ch. 9 §3 |
| aa | eedi3 | vs-jetpack EEDI3 (Vulkan) + Repair 2 | guides ch. 9 §3 |
| aa merge | — | LimitFilter(deband_Y, aa_Y, thr 1.0, elast 1.5) | guides ch. 9 §4 |
| field | ivtc | vivtc VFM + QTGMC (vs-jetpack) for leftover combed frames + VDecimate | guides ch. 10 |
| field | deint | QTGMC (vs-jetpack), single rate | guides ch. 10 |

## Choosing (results first)

- Live action with natural grain: usually **no filters**; at most `deband light`
  where CAMBI/boosted crops show banding. Never denoise artistic grain.
- Anime from BD: `deband medium` is the common baseline; `aa` only when the
  high-detail crops show aliasing; denoise only for visible noise/mosquito.
- Lossy sources being re-encoded (WebRip, re-encodes): `dpir` deblock
  (strength 5–15) removes blocking/mosquito noise far better than spatial
  denoisers, then `deband light|medium`. Check detail loss in the triptychs.
- Temporal noise on static scenes: `mc_degrain` keeps more detail than
  spatial denoisers.
- HDR: avoid filters except crop unless there is an obvious defect.
- Judge with the trial metrics: CAMBI out ≫ in means banding was introduced;
  SSIMULACRA2 p5 shows the worst frames.

## Per-episode and per-range overrides

```toml
[[episodes]]
id = "S01E07"
filters = { deband = { preset = "strong" } }     # whole episode
zones = [ { range = [12000, 13500], filters = { deband = { preset = "strong" } } } ]
```

`video.script_locked = true` keeps hand-edited scripts in `scripts/` from being
regenerated (for advanced manual work).
