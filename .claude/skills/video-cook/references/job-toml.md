# job.toml schema

Defaults live in `src/videocook/job.py` (`DEFAULTS`). Edit the file directly;
`vcook` re-reads it on every command.

```toml
[job]
name = "black-hawk-down"       # directory name under <workspace>/jobs
title = "Black Hawk Down"      # used for the MKV title and Jellyfin naming
year = 2001
type = "film"                  # film | series
derived_from = ""              # fast track reference
status = "new"

[source]                       # film only (series: per episode)
kind = "bdmv"                  # bdmv | file
root = "X:/..."                # bdmv: disc root; file: path = "..."
playlist = "00503"
items = [ { clip = "00337", first = 0, last = 15923 }, ... ]   # resolved by vcook

[[episodes]]                   # series only
id = "S01E01"
kind = "main"                  # main | ncop | nced | extra
label = ""                     # for extras: "NCOP1"
source = { kind = "file", path = "..." }
filters = { ... }              # optional per-episode override
zones = [ { range = [a, b], filters = { ... } } ]
skip = false

[video]
mode = "encode"                # encode | passthrough
content = "live_action"        # anime | live_action | webrip
tier = "standard"              # archival | standard | compact
crf = 0.0                      # non-zero overrides the tier
x265_extra = ""                # e.g. "--psy-rd 2.2 --aq-strength 0.6"
script_locked = false
[video.filters]                # see filters.md

[audio]
tracks = [ { index = 1, action = "copy", language = "eng", title = "", default = true } ]
external = [ { file = "...", language = "jpn", title = "2.1ch", action = "copy", default = false } ]

[subtitles]
tracks = [ { index = 18, action = "copy", language = "eng", title = "", default = false, forced = false } ]
external = [ { file = "...", language = "chi", title = "简日双语", action = "copy", default = true } ]
font_dirs = []

[chapters]
mode = "source"                # source | none
language = "eng"
keyframes = true

[output]
nas_dir = ""                   # asked at delivery
resolution_label = ""          # override "1080p" etc.
audio_label = ""               # override "FLAC" etc.

[run]
parallel = 0                   # 0 = benchmark/config default
trim = []                      # [[a, b], ...] test runs only

[trial]
clips = 4
clip_seconds = 10
```
