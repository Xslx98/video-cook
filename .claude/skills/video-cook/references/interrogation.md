# Interrogation tree

Ask one question at a time, with a recommendation built from the facts. Skip
any question the facts already settle, and say so briefly ("音轨只有一条日语
FLAC，直接保留"). Record each answer in job.toml before asking the next.

1. **Purpose & tier** — archival / standard / compact (`video.tier`). Mention
   the source size and, later, the trial estimate. Lossy source → passthrough
   recommended instead (see sources.md).
2. **Which content?** (only if ambiguous) — playlist/cut, episodes, extras
   (main + NCOP/NCED by default for series).
3. **Content type** — anime / live_action / webrip (`video.content`). Usually
   obvious from the sheet; confirm only when mixed (e.g. CG-heavy film).
4. **Defects, one by one, from the defect report** — for each: show the
   image(s), state severity, recommend fix-or-preserve with strength.
   Order: field handling (interlace/telecine) → banding → aliasing → noise →
   everything else.
5. **Crop** — if black borders were detected: crop to the active area (even
   values) or keep (some prefer letterboxed). Never crop HDR/UHD without asking.
6. **Audio** — languages to keep, default track, commentary identification and
   titles, lossy dubs, object audio passthrough (inform only).
7. **Subtitles** — which PGS/ASS to keep, default/forced flags, external ASS
   titles; fonts problems.
8. **Chapters** — keep and rename (default) or none; language.
9. **Metadata & naming** — title, year (Jellyfin), any label overrides.
10. **Series specifics** — per-episode exceptions found in the season summary
    (zones/overrides), episode numbering.
11. **Ready for trial** — summarise all decisions in a compact table and ask
    to proceed to stage 4.

After trial: ask accept / adjust (which knob) / change tier. After QA: ask the
NAS output directory, then deliver.
