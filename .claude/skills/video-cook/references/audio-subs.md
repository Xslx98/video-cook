# Audio, subtitles, fonts, chapters

Guides: ch. 3 (demux/mux tools), ch. 11 (OKEGui's track handling: empty and
duplicate tracks, optional tracks, ordering, lossy compression, default flags,
chapter checks).

## Audio plan (`[[audio.tracks]]`, one entry per source track)

| Source | `action` | Why |
|---|---|---|
| LPCM / DTS-HD MA / TrueHD without objects | `flac` | lossless, smaller |
| TrueHD Atmos / DTS:X | `copy` | keeps object metadata (FLAC would lose it) |
| Lossy main (AC3, DTS core, AAC, E-AC3) | `copy` | no second lossy generation |
| Secondary lossless (commentary) | `opus` | 2.0 128k, 5.1 256k, 7.1 320k (`bitrate` overrides) |
| AC-3 core of TrueHD, duplicates, unwanted languages | `drop` | |

Fields: `index` (source stream index), `action`, `language` (ISO 639-2),
`title` (e.g. `"Commentary - Director"`; leave empty for the main track),
`default` (exactly one audio track), `bitrate` (opus only).
Interrogate: which languages to keep, which track is default, which tracks are
commentary (titles are rarely present on discs — ask), keep or drop lossy
dubs. Point out empty/duplicate/fake-multichannel suspicions; never drop them
silently. External audio (`audio.external`, e.g. VCB-S `.mka`): `action =
"copy"|"drop"`, with language/title.

## Subtitles (`[[subtitles.tracks]]` + `subtitles.external`)

Soft subtitles only. PGS/SRT/ASS/VobSub are copied; set `forced = true` on
forced-only tracks, one `default` at most (usually none for a native-language
audio track; ask). External ASS: `file`, `language`, `title` (e.g.
`"简日双语"`), `default`.

## Fonts

External ASS fonts are subset with assfonts and attached to the MKV. Fonts
are looked up in `subtitles.font_dirs` (folders next to the release are added
automatically) and the system fonts. **Missing fonts block the run** — ask the
user for a font pack folder and add it to `font_dirs`. Only if the user
explicitly accepts shipping without them, set `subtitles.allow_missing_fonts =
true` (assfonts then attaches nothing at all — say so). Mention the guides' tip:
releases usually ship a font pack; FontLoaderSub/ListAssFonts are the GUI
equivalents.

## Chapters

`[chapters] mode = "source" | "none"`, renamed `Chapter 01…`, `language`
(ISO 639-2), `keyframes = true` puts I-frames at chapter starts (qpfile).
BD chapters come from the playlist marks; files from their own chapters.
