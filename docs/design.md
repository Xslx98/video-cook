# video-cook design decisions

This document records the decisions agreed in the initial design interview
(2026-10-01). It is the reference for *why* the code and the skill behave the
way they do. Change it when a decision changes.

Guiding principle (2026-10-02): inherit the guides' *thinking* — assess first,
fix defects in the right order, never trade detail for a fix the viewer won't
notice — without being bound to their exact versions, tools or plugins. Prefer
current tools and frontier techniques when results justify them.

Knowledge base: the public VCB-Studio guides (<https://guides.vcb-s.com/>,
vendored as a submodule under `third_party/vcb-s-guides`). The guides carry no
license, so this repository never copies their text, images or sample scripts;
the skill references are written from scratch and link back to the chapters.

## 1. Toolchain

Full VCB-S route, minus OKEGui:

| Role | Tool |
|---|---|
| Frame server / filtering | VapourSynth **R80** (PyPI wheel inside the uv `.venv`) |
| VS plugins | API 4 plugin wheels from PyPI, pinned in `uv.lock`; filter library vs-jetpack |
| GPU / AI | CUDA + TensorRT plugins via the `nvidia` extra; OpenCL / Vulkan / OpenVINO otherwise |
| Encoder | **x265, 10-bit only** (Patman mod, `x86-64-v3` build) |
| Probing | ffprobe, MediaInfo CLI, mkvmerge `-J` |
| Audio | ffmpeg (FLAC, Opus) |
| Muxing / BD playlists | MKVToolNix (`mkvmerge` reads `.mpls` directly) |
| Fonts | assfonts (subset + embed) |
| HDR metadata | dovi_tool, hdr10plus_tool |
| Orchestration | `vcook` Python CLI (this repo) — replaces OKEGui |

Why R80 + wheels: R80 dropped API 3 plugins and vsrepo still ships API 3
builds, but the PyPI wheel ecosystem (the one vs-jetpack depends on) has API 4
builds of almost everything, and uv pins their versions — reproducible across
machines, unlike vsrepo's "latest". Replacements: f3kdb → vszip.Deband (same
algorithm), mvsfunc.LimitFilter → vszip.LimitFilter, misc.SCDetect → own
motion-spike detector, havsfunc QTGMC → vs-jetpack QTGMC, BM3D CPU → BM3D
CUDA/Vulkan. Verified 2026-10-02 (the first evaluation wrongly concluded R79
because it only tested vsrepo builds).

## 2. Installation

`bootstrap.ps1` → `uv sync` (`--extra nvidia` when an NVIDIA GPU is present) →
`vcook bootstrap` (portable tools + DPIR models into the shared vsscale cache).
Portable binaries pinned in
`tools.lock.json` (URL + SHA256) are downloaded into the git-ignored `tools/`
directory. Nothing is installed system-wide; no admin rights needed. The
bootstrap ends with a short x265 benchmark that calibrates the default number
of parallel encodes.

## 3. Encoder scope

Only x265 10-bit. No x264, no NVENC, no AV1. Preview/test encodes also use
x265 (fast preset on short segments) so previews behave like the final encode.
Video **passthrough** (no re-encode) is allowed — see §20.

## 4. Source types

Supported: BDMV folder / BD ISO, MKV/MP4 (remux, WEB-DL, other encodes), DVD
(VOB/ISO; interlaced, IVTC, VFR), UHD HDR10.
Dolby Vision: detected and flagged; default plan is to drop the DV layer and
keep the HDR10 base layer. HDR10+ dynamic metadata: detected; passed through to
x265 when present. HDR handling is *outside* the guides and is labelled as such.

## 5–6. Assessment

1. Metadata probe (containers, tracks, colour, HDR, interlacing flags, BD
   playlists, crop).
2. Cheap full scan in VapourSynth at ~540p (combing check at native res):
   luma mean/min/max, out-of-range ratio, flatness (low-gradient ratio),
   edge density, motion (frame diff), scene changes, black borders.
3. Shots are grouped into risk buckets: dark-flat (banding), bright gradient,
   high-detail lines (aliasing/ringing), high motion (blocking/combing),
   dark-noisy, static/credits, normal.
4. Stratified sampling: weight × duration share, ≥1 frame per non-empty
   bucket, ≤1 frame per shot, representative + extreme frame per bucket,
   dark-flat weighted up. Budget: ~24 frames for a film, 8 per episode for a
   series plus a season summary.
5. CAMBI (Netflix banding metric) per frame; shots with CAMBI peaks ≥ 3 at
   scan resolution are always sampled ("cambi-hotspot"), even when the bucket
   heuristics put them elsewhere.
6. A frame-composition report (bucket shares, timelines) plus full frames and
   zoomed crops. Claude reads the images and writes a defect report using the
   guides' chapter 4 taxonomy; the user confirms.
7. getnative-style native resolution detection is informational only, never a
   default action.

All parameters live in config and can be overridden per job.

## 7. Verification before the full run

- Filter check: source / filtered / amplified-difference triptychs on the
  sampled frames.
- Encode check: 3–5 ~10 s clips from the riskiest shots encoded with the final
  parameters; source-vs-encode screenshots; size extrapolated to the full run.
- Reference metrics on test clips (never gates): SSIMULACRA2 (mean and 5th
  percentile), XPSNR, and CAMBI of encoder input vs. output (banding added by
  x265). Replaced SSIM on 2026-10-02. No VMAF.

## 8. Audio

| Source | Action |
|---|---|
| LPCM / DTS-HD MA / TrueHD without objects | FLAC |
| TrueHD Atmos / DTS:X | passthrough |
| Lossy main tracks (AC3, DTS core, AAC…) | passthrough |
| Secondary tracks (commentary) from lossless | Opus |
| Empty / duplicate / fake multichannel | reported, user decides |

## 9. Repository boundary (public repo)

Repo contains tools, skill, templates and self-written references only.
Job data (paths, screenshots, reports, scripts, logs) lives outside the repo.

## 10. Storage layout

| Data | Location |
|---|---|
| Sources | read-only, typically from the NAS; never write next to a source |
| `.lwi` / index caches | local job directory |
| Job directory (reports, shots, scripts, logs, intermediates) | local SSD workspace (`config.local.toml`) |
| Deliverables | local first; copied to the NAS output dir (asked per job) after QA, CRC re-verified |
| After completion | intermediates removed; `job.toml`, scripts and reports kept |

Optional per-job switch: copy the source to local disk first.

## 11. Subtitles, fonts, chapters

Soft subtitles only (no hardsubs). PGS/SRT/ASS/VobSub passed through; forced
tracks flagged. External ASS: assfonts subsets and embeds the fonts; missing
fonts or glyphs **block muxing** and the user is asked. No fixed font library:
fonts come from the system plus per-job font folders. Same-basename sidecar
files (`.mka`, `.ass`, `.sup`, …) are discovered automatically and offered.
Chapters: extracted from the BD playlist, renamed `Chapter 01…`, language
tagged, keyframes forced at chapter points.

## 12. x265 parameters

Three layers:
1. Base template derived from the guides' general BDRip settings; colour
   flags and HDR10 mastering metadata filled in from the probe, never by hand.
2. Content-type overlay: anime / live-action grain / WebRip-clean.
3. Rate control: CRF quality tier (archival / standard / compact). Size is
   estimated from test clips; no 2-pass. No VBV (playback is mpv on PC).

## 13. Series

One `.vpy` template per season plus per-episode overrides (including frame
ranges) in `job.toml`. Extras: main episodes + NCOP/NCED only. Episodes are
queued; default parallelism 2 for ≤1080p and 1 for 4K, calibrated by the
bootstrap benchmark. Resumable; failed episodes can be re-run alone. Season
scan runs on every episode; Claude reviews flagged episodes and samples.

## 14. QA (all must pass before copying to the NAS)

1. Full decode with zero errors.
2. Frame count equals the `.vpy` output (timecodes for VFR).
3. Track durations in sync (< 1 frame).
4. Metadata: colour, HDR, track language/name/default flags, chapters.
5. Source-vs-output screenshots on the sampled frames (report only, no gate).
6. CRC32 recorded in the report (not in the filename) and re-verified on NAS.

## 15. Naming (Jellyfin)

- Film: `Title (Year)/Title (Year) [1080p x265 10bit FLAC].mkv`
- Series: `Title (Year)/Season 01/Title - S01E01 [1080p x265 10bit FLAC].mkv`
- NCOP/NCED: `Season 01/Extras/`

Track metadata: MKV title = title; video track name empty; audio names like
`FLAC 5.1`, `Commentary - Opus 2.0`; ISO 639-2 language codes.

## 16. Execution

Detached background queue independent of the Claude session; progress in the
job's `state.json` and logs; idempotent steps; Windows toast on finish/failure;
system sleep blocked while running; encoders at below-normal priority.

## 17. Skill workflow

1. **Intake** — `vcook probe` → job directory.
2. **Assess** — `vcook scan` / `vcook sample` → reports + images.
3. **Interrogate** — one question at a time; decisions go into `job.toml`
   (single source of truth).
4. **Trial** — `.vpy` + triptychs + test clips; loop back to 3 if rejected.
5. **Run** — `vcook run` in the background.
6. **QA & deliver** — QA, copy to NAS, CRC, cleanup.

The orchestrator never decides; it only executes `job.toml`.

## 18. Fast track

`vcook new --from <past job>` derives a job. Assessment still runs (scan +
probe only). Any mismatch against the reference job (resolution, frame rate,
colour, interlacing, HDR, audio layout, dark share differing by >10 points)
drops out of the fast track. QA is never skipped. One 10 s test clip is still
encoded and attached to the QA report without blocking. Job configs are backed
up to `<NAS output>/.videocook/` so jobs can be derived on another machine.

## 19–20. Acceptance and re-encode policy

Acceptance cases (run with `--trim`, all six stages):
- Black Hawk Down UHD BDMV — playlists, DV + HDR10, Atmos passthrough.
- House MD S01E01 (existing HEVC encode) — re-encode detection → remux.
- JoJo DU ep 01 (VCB-S release) — anime, sidecar `.mka` + ASS, missing fonts.
- Synthetic telecine / interlaced clips — DVD path (marked synthetic-only).

Lossy-source policy: when the source is already a lossy encode, the report
warns about generation loss and estimates the size; the default recommendation
is **video passthrough** (remux, tracks, subs, naming), re-encoding only on
explicit request.

## Implementation notes (found during acceptance, 2026-10-02)

- **VapourSynth R80 + PyPI wheels** (see §1). bm3dcpu is excluded via a uv
  override (still API 3); jetpack's `nvidia` extra pulls Linux-only CUDA
  wheels, so our own `nvidia` extra lists the Windows plugins explicitly.
- **Colour tagging**: generated scripts set `_Matrix/_Transfer/_Primaries/
  _ColorRange` from the probe (HD/SD defaults for untagged sources) so RGB-based
  filters (DPIR) and resizers never guess.
- **Track ids**: ffprobe stream indexes and mkvmerge track ids differ (MPEG-PS,
  BD). The probe pairs them by order within each type (`mkv_id`); track
  selection always uses `mkv_id`.
- **Inconsistent BD tracks**: DIY discs can change a track's format between
  the playlist's clips (e.g. AC-3 5.1 → 2.0); mkvmerge cannot join it. The run
  stops with a readable error and the user drops/replaces the track.
- **Fonts**: assfonts aborts the whole subset when any font is missing, so
  missing fonts block the run. `subtitles.allow_missing_fonts = true` is an
  explicit, user-approved escape hatch (no fonts are attached then). Multiple
  ASS files are subset together (`-c`) so each font is attached once.
- **Test runs (`--trim`)**: audio/subtitles are cut while demuxing (before
  FLAC conversion — mkvmerge cannot split FLAC); external audio is cut with
  ffmpeg. Cutting TrueHD at arbitrary points produces decoder errors at the
  joins, so in trimmed runs the audio-decode QA check is non-blocking.
  Chapters and chapter keyframes are skipped.
- **QA** additionally checks the video track's real duration against
  frames/fps (catches a wrong `--default-duration`).
- **Scan speed** (laptop, Core Ultra 7 255H): 1080p HEVC ≈ 600 fps, UHD HEVC
  ≈ 150 fps with hardware decoding. Encodes use software decoding.
- **Encode speed reference** (laptop): UHD grainy film at `slower` ≈ 0.3–0.6
  fps; 1080p ≈ 3 fps. Plan full runs on the desktop.
