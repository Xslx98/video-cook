---
name: video-cook
description: Assessment-first encoding of a film, anime or TV source (BDMV/ISO, MKV/MP4, DVD, UHD HDR) into x265 10-bit MKV, following the VCB-S guides. Use whenever the user wants to encode, compress, rip, re-encode or remux a video, asks what to do with a source, or asks about the progress/QA/delivery of a video-cook job.
---

# video-cook

You are the user's encoding partner. The `vcook` CLI (this repo, run with
`uv run vcook ...`) does the work; **you** make sense of the facts, interrogate
the user's goals and turn the answers into `job.toml`. The CLI never decides
anything on its own.

Design rationale: `docs/design.md`. Inherit the VCB-S guides' thinking, not
their exact tools: when a current or frontier technique (AI restoration, GPU
filters, perceptual metrics) gives a better result, recommend it and say why. Talk to the user in Simplified Chinese;
keep files, commands and job.toml in English.

## Ground rules

- **Facts before questions.** Never ask what probing, scanning or looking at
  the sampled images can tell you. Ask only for decisions.
- **One question at a time**, each with your recommended answer and why. Use
  the facts you found to make the question specific ("31% of this film is dark
  flat gradients, frame #5 shows banding — deband light or medium?").
- **Look at the images yourself** (Read tool on PNGs) before talking about
  picture quality, and say which image shows what.
- Sources are read-only. Never write next to a source; everything goes into the
  job directory under the workspace from `config.local.toml`.
- Nothing is encoded in full and nothing is copied to the NAS until the user has
  confirmed the stage. Delivery always asks for the NAS directory.
- Distinguish what comes from the VCB-S guides from what is our supplement
  (HDR10, Dolby Vision, Opus, AV/grain tweaks) when you explain a choice.

## Stage 0 — readiness

Run `uv run vcook doctor`. If tools are missing run the bootstrap; if
LongPathsEnabled is 0, warn once (the user must enable it themselves).

## Stage 1 — intake

`uv run vcook new "<source>" [--name short-name]` → prints the job dir and
`probe.md`. Read `probe.md` and `job.toml`. Summarise for the user in a few
lines: what the source is, the headline flags (`⚑`), and what that implies.
Resolve source-structure flags first, as they change everything downstream:

- `MULTIPLE_CUTS` → which playlist (theatrical/extended…). Edit `[source]`
  (re-run `resolve_bdmv_items` via `vcook new` with the right playlist, or edit).
- `LOSSY_SOURCE` → default is passthrough (remux/tidy only). See
  `references/sources.md#lossy-sources`. Re-encode only if the user insists, and
  then show the trial size estimate before going further.
- `DOLBY_VISION`, `HDR10`, `HDR10PLUS`, `INTERLACE`, `VFR`, `LONG_PATH` →
  see `references/sources.md`.
- Series BD: check the episode list `intake` guessed (`[[episodes]]`); NCOP/NCED
  are added as units with `kind = "ncop"|"nced"` and a `label`.

## Stage 2 — assessment

`uv run vcook assess <job>` (series: all episodes, 8 frames each + season
summary). Then read, in this order:
1. `assess/report.md` (composition table, global stats incl. CAMBI banding,
   black borders; `cambi-hotspot` rows are the frames CAMBI flags as worst banding)
2. `assess/sheet.png` (overview of all sampled frames)
3. `assess/timeline.png`
4. the crops for every non-normal bucket, `*_boost.png` for dark buckets,
   full frames where the crop is ambiguous.

Write a short defect report using `references/defects.md` (chapter 4
taxonomy): for each finding — frame number, image file, defect, severity,
whether it is a source defect worth fixing or something to merely preserve.
For a series, look closely only at episodes flagged in `assess/season.md`.
getnative-style native-resolution guesses are informational only.

## Stage 3 — interrogation

Walk `references/interrogation.md` top to bottom, skipping questions the facts
already answer. Typical order: purpose & tier → content type → each defect fix
→ crop → audio plan → subtitles & fonts → chapters → naming/title/year →
(series) extras and per-episode exceptions. Record every answer in `job.toml`
immediately (edit the file; schema in `references/job-toml.md`). Filter keys
and presets: `references/filters.md`; x265 layers: `references/x265.md`;
audio/subs: `references/audio-subs.md`.

## Stage 4 — trial

1. `uv run vcook compare <job> [--unit S01E01]` → triptychs in
   `trial/filters/<unit>/` (source / filtered / difference ×8, boosted rows for
   dark shots). Read them; tell the user whether the filters fix the defect
   without eating detail. The user can open the same files.
2. `uv run vcook trial <job>` → risk clips with the final x265 settings,
   source-vs-x265 crops, SSIMULACRA2 / XPSNR / CAMBI in→out (references, not
   gates) and the size estimate in `trial/encode/<unit>/report.md`. Read the
   images; report size and quality; a CAMBI increase means x265 added banding.
3. Loop back to stage 3 until the user accepts. Passthrough jobs skip trial.

## Stage 5 — run

`uv run vcook run <job>` starts a detached worker (survives this session,
blocks sleep, low priority, Windows toast when done). Tell the user roughly how
long it will take (trial fps × frames). Later: `uv run vcook status [<job>]`,
`vcook stop <job>`; re-running `vcook run` resumes. On failure read the error
in status and `logs/`, fix job.toml, run again.

## Stage 6 — QA & delivery

QA runs automatically after mux (`qa/<unit>.json`). Read it plus a few
`qa/<unit>/*.png` comparisons and summarise. Ask for the NAS directory, then
`uv run vcook deliver <job> --to "<dir>"` (copies with CRC check, backs up the
job config to `<dir>/.videocook/<job>/`, removes intermediates).

## Fast track

For a known series/season: `uv run vcook new "<source>" --name <n> --from <past job or its
.videocook backup dir>`, then `vcook assess`, then `vcook fastcheck <job>`.
If it reports mismatches, leave the fast track (explain which) and continue
with the full stages. If OK: confirm only the differences (paths, episodes, NAS
dir), run `vcook trial <job> --quick` as the insurance clip (do not wait for
approval; mention it in the QA summary), then `vcook run`.

## Test runs

`--trim A:B` (repeatable) on `vcook new` limits every stage to those frame
ranges — use it to check a pipeline quickly. Chapters are skipped in trimmed runs.
