# video-cook — notes for Claude

- Design decisions and their reasons: `docs/design.md`. Read it before changing behaviour.
- The encoding workflow lives in the `video-cook` skill (`.claude/skills/video-cook/`).
- Python is managed by uv only: `uv run ...`, `uv add ...`. Never pip.
- VapourSynth R80; plugins are PyPI wheels pinned in uv.lock (`uv add vapoursynth-<name>`), never vsrepo. Check API 4 compatibility before adding a plugin.
- Follow the guides' thinking, not their exact tools; prefer current/frontier tools when results justify it.
- Toolchain binaries are resolved via `videocook.toolchain.tool()`; never rely on PATH.
- Job data (paths, screenshots, reports) lives in the workspace from `config.local.toml`, never in the repo — the repo is public.
- Never write next to source files (sources are read-only, often on the NAS).
- Do not copy text, images or scripts from `third_party/vcb-s-guides` into the repo; summarise and link.
- Code, comments and docs in English; talk to the user in Simplified Chinese.
