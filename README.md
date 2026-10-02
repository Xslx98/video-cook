# video-cook

Assessment-first video encoding pipeline for Windows, built on the
[VCB-Studio public guides](https://guides.vcb-s.com/) and driven by a Claude
Code skill.

Every encode follows the same loop: **probe and scan the source → interrogate
the goals → trial → run → QA → deliver**. The `vcook` CLI executes; all
decisions are captured in a per-job `job.toml`, so jobs are reproducible and
can be re-run or derived later.

## Setup

Requirements: Windows x64, [uv](https://docs.astral.sh/uv/), git.

```powershell
git clone --recursive https://github.com/Xslx98/video-cook
cd video-cook
powershell -ExecutionPolicy Bypass -File bootstrap.ps1
```

The bootstrap downloads the pinned portable toolchain (`tools.lock.json`) into
`tools/`, installs VapourSynth R79 and its plugins into the uv `.venv`, checks
everything loads and benchmarks x265 to pick the default parallelism. Nothing
is installed system-wide. Edit `config.local.toml` to set the local workspace.

## Toolchain

VapourSynth R79 · x265 (10-bit) · ffmpeg · MKVToolNix · MediaInfo · assfonts ·
dovi_tool · hdr10plus_tool. See [docs/design.md](docs/design.md) for the
reasoning behind every choice.

## Usage

Open the repo in Claude Code and ask to encode something; the `video-cook`
skill drives the workflow. The CLI can also be used directly — run
`uv run vcook --help`.

## Credits

The encoding knowledge comes from the VCB-Studio guides, vendored as a
submodule in `third_party/vcb-s-guides`. Their content is not redistributed
here; the skill references are original summaries that link back to the
relevant chapters.
