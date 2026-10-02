"""Repository paths and machine-local configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
TEMPLATES_DIR = REPO_ROOT / "templates"
LOCK_FILE = REPO_ROOT / "tools.lock.json"
LOCAL_CONFIG = REPO_ROOT / "config.local.toml"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.toml"


@dataclass
class Settings:
    workspace: Path
    parallel_1080p: int | str = "auto"
    parallel_2160p: int | str = "auto"

    @property
    def jobs_dir(self) -> Path:
        return self.workspace / "jobs"

    @property
    def bench_file(self) -> Path:
        return TOOLS_DIR / "benchmark.json"


def load_settings() -> Settings:
    path = LOCAL_CONFIG if LOCAL_CONFIG.exists() else EXAMPLE_CONFIG
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    paths = data.get("paths", {})
    encode = data.get("encode", {})
    return Settings(
        workspace=Path(paths.get("workspace", "D:/VideoCook")),
        parallel_1080p=encode.get("parallel_1080p", "auto"),
        parallel_2160p=encode.get("parallel_2160p", "auto"),
    )
