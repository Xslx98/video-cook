# Filters: `[video.filters]` in job.toml

Implementation: `src/videocook/filters.py` (`standard_chain`). Order follows
the guides' chapter 9 §4: **field handling → crop → 16-bit → denoise → (deband
‖ AA, both from the denoised clip) → LimitFilter merge → 10-bit**.

```toml
[video.filters]
crop = { left = 0, top = 140, right = 0, bottom = 140 }   # even numbers for 4:2:0
field = "ivtc"            # or "deint"; omit for progressive sources
tff = true
denoise = { preset = "light" }          # light/medium/strong (nlm h 1.5/3/5)
# denoise = { method = "dfttest", sigma = 8 }  /  { method = "bm3d", sigma = 3 }
deband = { preset = "medium" }          # light / medium / strong
# deband = { preset = "medium", limit_thr = 0.55, limit_elast = 1.5 }
aa = { method = "nnedi3" }              # nnedi3 / eedi2 / eedi3, merge_thr 1.0, merge_elast 1.5
```

No filters at all (and 10-bit source) → the script passes the source through
untouched apart from crop; 8-bit sources are converted to 10-bit.

## Presets and their origin

| Key | Preset | Values | Source |
|---|---|---|---|
| deband | light | f3kdb (12, 48/32/32) + (24, 32/24/24) | our softer variant |
| deband | medium | (12, 64/48/48) + (24, 48/32/32) | guides ch. 9 §2 example |
| deband | strong | (12, 96/48/48) + (24, 72/32/32) | guides ch. 9 §2 example |
| deband | all | grain 0, output 16-bit, LimitFilter thr 0.55 elast 1.5 vs input | guides ch. 8 §4, ch. 9 |
| denoise | nlm | nlm_ispc NLMeans d=0 wmode=3 h | guides ch. 9 §1 (h=3 example) |
| aa | nnedi3/eedi2/eedi3 | double-height interpolation in both directions + Repair mode 2 | guides ch. 9 §3 |
| aa merge | — | LimitFilter(deband_Y, aa_Y, thr 1.0, elast 1.5) | guides ch. 9 §4 |
| field | ivtc | VFM + QTGMC for leftover combed frames + VDecimate | guides ch. 10 |
| field | deint | QTGMC Slower, single rate | guides ch. 10 |

## Choosing

- Live action with natural grain: usually **no filters**; at most `deband light`
  for visible banding in dark gradients. Never denoise artistic grain.
- Anime from BD: `deband medium` is the common baseline; add `aa` only when the
  high-detail crops show aliasing; denoise only for visible noise/mosquito.
- WebRip/lossy sources being re-encoded: `denoise light` + `deband medium`
  (guides' WebRip example cleans before encoding).
- HDR: avoid filters except crop unless there is an obvious defect; thresholds
  were designed for SDR.

## Per-episode and per-range overrides

```toml
[[episodes]]
id = "S01E07"
filters = { deband = { preset = "strong" } }     # whole episode
zones = [ { range = [12000, 13500], filters = { deband = { preset = "strong" } } } ]
```

`video.script_locked = true` keeps hand-edited scripts in `scripts/` from being
regenerated (for advanced manual work).
