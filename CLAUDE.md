# video-cook — notes for Claude

- Design decisions and their reasons: `docs/design.md`. Read it before changing behaviour.
- The encoding workflow lives in the `video-cook` skill (`.claude/skills/video-cook/`).
- Python is managed by uv only: `uv run ...`, `uv add ...`. Never pip.
- VapourSynth is pinned to R79 on purpose (R80 dropped API 3 plugins). Do not upgrade it.
- Toolchain binaries are resolved via `videocook.toolchain.tool()`; never rely on PATH.
- Job data (paths, screenshots, reports) lives in the workspace from `config.local.toml`, never in the repo — the repo is public.
- Never write next to source files (sources are read-only, often on the NAS).
- Do not copy text, images or scripts from `third_party/vcb-s-guides` into the repo; summarise and link.
- Code, comments and docs in English; talk to the user in Simplified Chinese.
