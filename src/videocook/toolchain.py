"""Locate pinned binaries and run them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

from videocook.config import LOCK_FILE, TOOLS_DIR


class ToolMissing(RuntimeError):
    pass


@cache
def lock() -> dict:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


@cache
def _bin_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for name, spec in lock()["tools"].items():
        for exe, rel in spec["bin"].items():
            index[exe] = TOOLS_DIR / name / rel
    return index


def venv_scripts() -> Path:
    return Path(sys.executable).parent


def tool(exe: str) -> Path:
    """Absolute path of a pinned executable (e.g. 'ffprobe', 'x265', 'vspipe')."""
    if exe in ("vspipe", "vsrepo"):
        path = venv_scripts() / f"{exe}.exe"
    else:
        path = _bin_index().get(exe)
        if path is None:
            raise KeyError(f"unknown tool {exe!r}")
    if not path.exists():
        raise ToolMissing(f"{exe} not found at {path}; run `vcook bootstrap`")
    return path


def run(
    exe: str,
    *args: str | os.PathLike,
    check: bool = True,
    capture: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    cmd = [str(tool(exe)), *map(str, args)]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=capture,
        encoding="utf-8" if capture else None,
        errors="replace" if capture else None,
        **kwargs,
    )


def run_json(exe: str, *args: str | os.PathLike) -> dict:
    return json.loads(run(exe, *args).stdout)
