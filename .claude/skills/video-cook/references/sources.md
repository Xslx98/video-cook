# Source types and special cases

## Blu-ray (BDMV folder / ISO) — guides ch. 2–3

- `probe.md` lists playlist groups: `feature-candidate`, `episode-candidate`,
  `extra`, `short`, `suspicious-loop` (obfuscation decoys: many play items
  looping 1–2 clips — ignore). Identical playlists are collapsed (duplicates).
- **Multiple cuts** (`MULTIPLE_CUTS`): several feature-length playlists sharing
  clips via seamless branching (theatrical / extended / director's cut). Ask
  which cut; durations usually identify them. Switch with
  `uv run vcook playlist <job> <playlist>`.
- mkvmerge reads `.mpls` directly (concatenation, seamless branching, chapters,
  languages); VapourSynth opens the m2ts clips trimmed to the playlist's in/out
  points, so no full demux of the video is ever written to disk.
- TrueHD tracks carry an AC-3 compatibility core in the same PID: the plan
  marks it `drop (AC-3 compatibility core...)`.
- ISO: `vcook new` mounts it read-only with Windows' built-in mounter (or ask
  the user to mount it and pass the drive letter).
- Series discs: episodes are separate playlists or one long playlist with
  chapters per episode; check `intake`'s guess against durations. NCOP/NCED are
  usually 1:30 extras: `uv run vcook unit-add <job> --playlist 00012 --kind ncop --label NCOP1`.

## Files (MKV/MP4/M2TS)

- Same-basename sidecars (`name.sc_jp.ass`, `name.mka`) are copied into
  `work/external/` and listed under `audio.external` / `subtitles.external`
  with guessed language/title (VCB-S tags: sc = simplified Chinese, tc =
  traditional, jp = Japanese). Confirm titles with the user.
- `LONG_PATH`: paths > 260 chars. Sidecars are copied to short paths; the
  video itself still needs LongPathsEnabled for some tools.

## Lossy sources

`LOSSY_SOURCE` = the video is already a consumer encode (encoder tag x264/x265,
HEVC at ≤1080p, low bits/pixel). Re-encoding stacks generation loss and often
*grows* the file (House MD test: 3.9 Mbps source → 7.3 Mbps at CRF 19).
Default: `video.mode = "passthrough"` — the video track is copied, everything
else (audio plan, subs, fonts, chapters, naming) still applies. Re-encode only
on explicit request, after showing the trial estimate vs. the source size.

## DVD — guides ch. 10

- `VIDEO_TS`: the longest title set is remuxed to `work/dvd_title.mpg`
  (stream copy) and treated as a file source.
- Probe runs ffmpeg `idet`: `telecined` (≈20% repeated fields, 3:2 pulldown)
  → `field = "ivtc"`; `interlaced` → `field = "deint"` (QTGMC single rate);
  `mixed / hybrid` → look at combing in `high_motion` crops and ask.
- Soft pulldown / VFR: chapter 10 covers timecodes; our pipeline currently
  outputs CFR — flag VFR sources to the user before proceeding.
- Colour: SD is BT.601 (`smpte170m` NTSC, `bt470bg` PAL). Untagged SD sources
  are encoded with smpte170m flags; ask if PAL.
- DVD support has only been validated with synthetic telecine clips.

## HDR (our supplement — not in the guides)

- **HDR10**: static metadata (mastering display, MaxCLL/MaxFALL) is read from
  the first frame and passed to x265 (`--hdr10 --hdr10-opt --master-display
  --max-cll`). Sampled images are tone-mapped (Hable) to SDR for viewing only.
- **HDR10+**: dynamic metadata detected → extract with `hdr10plus_tool` and
  pass `--dhdr10-info` (manual step for now; tell the user).
- **Dolby Vision**: detected via DOVI config record / RPU side data or an
  enhancement-layer video track. Default: encode the HDR10 base layer only (the
  DV layer is dropped automatically because the RPU is not passed to x265).
  Profile 5 (no HDR10 base, IPTPQc2) cannot be handled this way — stop and
  tell the user. For passthrough jobs DV metadata would survive; strip it with
  dovi_tool only if the user asks.
- HDR filters: avoid unless clearly needed; QA checks HDR10 metadata survived.

## Frame-rate and colour flags

- `VFR` from MediaInfo: preserve timestamps (passthrough) or ask before
  converting to CFR.
- `COLOUR_UNTAGGED`: no colour metadata; we assume BT.709 for HD and write the
  flags explicitly.
