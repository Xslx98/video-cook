# Defect taxonomy → evidence → remedy

Summarised from the VCB-S guides, chapter 4 (认识瑕疵) and the repair chapters
8–10. Original text: `third_party/vcb-s-guides/Basics/[04] 认识瑕疵/README.md`.
Read the original when unsure — it has example images for every defect.

Picture elements (chapter 4 §1): **flat areas** (low frequency), **lines**
(edges), **texture**, **noise/grain**; and static vs moving content. Every fix
trades against one of these: deband/denoise eat texture, AA softens lines.

| Defect | What it looks like | Where the scan finds it | Typical remedy | Notes |
|---|---|---|---|---|
| Banding (色带) | contour steps in gradients — sky, walls, dark fades | `dark_flat`, `bright_gradient` crops; dark ones in `*_boost.png` | `deband` light/medium/strong | Most visible defect; 10-bit output keeps the fix. Strong deband on grainy film smears grain. |
| Aliasing (锯齿) | jagged/stair-stepped lines, broken thin lines | `high_detail` crops (zoomed 2×) | `aa` nnedi3 (eedi2/eedi3 for harder cases) | Mostly anime/CG. Upscaled ("fake 1080p") sources alias along every line. Live action rarely needs AA. |
| Ringing / haloing (振铃/晕轮) | light/dark echo hugging strong edges | `high_detail` | none in our presets (dering is advanced) — note it, usually preserve | Often from sharpening or bad upscales. |
| Noise / grain (噪点) | random fine texture; dynamic | `dark_noisy`, `noise` timeline | film: **preserve** (x265 live_action layer); anime/digital noise: `denoise` light | Decide: is it artistic grain or compression/sensor noise? |
| Blocking (色块) | square blocks, mostly in dark/moving areas | `dark_flat` boosted crops, `high_motion` | light deband can soften; mostly a source limit | Very common in lossy (re-encoded) sources. |
| DCT ringing / mosquito noise (烂边/蚊噪) | buzzing specks around edges | `high_detail`, `dark_noisy` | light denoise | Compression artefact of the source. |
| Luma overflow/underflow (亮度越界) | crushed blacks / clipped whites outside 16–235 | report "frames with >1% out-of-range luma" | usually preserve (limited range in, limited range out) | Only matters if large areas are clipped. |
| Combing (拉丝/横纹) | horizontal comb teeth on moving edges | probe interlace verdict, `high_motion` | `field = "ivtc"` (telecine) or `"deint"` | DVD/TV sources; chapter 10. |
| 缟缟 (orphan fields, wrongly processed interlacing) | combing that survived a bad deinterlace | as above | case by case; often unfixable | Chapter 10 last section. |
| Duplicate fields / frames (重复场) | repeated fields → judder | probe `repeated_fields_ratio`, report "near-duplicate frames" | IVTC | 3:2 pulldown signature ≈ 20% repeated fields. |
| Blending / ghosting (鬼影) | two frames blended | `high_motion` | unfixable in our scope; never deinterlace blended content again | Field-blended conversions (PAL↔NTSC). |
| Chroma banding / aliasing / shift / bleeding | colour steps, jaggy colour edges, colour offset, colour leaking | crops of saturated areas | deband touches chroma (cb/cr); shift/bleeding: advanced, usually preserve | Point it out; rarely worth risky fixes. |
| Global motion / pan judder (晃动) | shaky pans | `high_motion` | none | Informational. |
| Rainbow / dot-crawl (彩虹/点状斑纹) | composite-video artefacts | old DVD/TV sources | out of scope | Informational. |

## How to judge from the images

- Always compare full frame and crop; crops are chosen automatically and may
  miss the worst area — check the full frame when the crop looks clean.
- Dark buckets: banding/blocking only become visible in `*_boost.png`
  (levels stretched ×3). State that the user will see it less on a normal
  display; the decision is about how far to go, not whether it exists.
- Judge source defects (fix or preserve) separately from what the encoder may
  introduce (verified later in trial images).
- When unsure between two strengths, run `vcook compare` with each and show both.

## Fix-or-leave heuristics (guides ch. 9 closing remarks)

Every fix has side effects. Prefer leaving a mild defect untouched over a fix
that visibly removes detail. The goal is "the encode must not make it worse";
improving the source is optional and must be weighed against detail loss.
