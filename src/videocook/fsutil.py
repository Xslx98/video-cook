"""Filesystem helpers that survive Windows MAX_PATH (260 chars)."""

from __future__ import annotations

import os
import shutil
import winreg
from pathlib import Path


def lp(path: str | os.PathLike) -> Path:
    """Extended-length form of an absolute path (\\\\?\\ prefix) for Python-side I/O."""
    s = os.path.abspath(os.fspath(path))
    if s.startswith("\\\\?\\"):
        return Path(s)
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def plain(path: str | os.PathLike) -> Path:
    """Strip the extended-length prefix again (for display and for external tools)."""
    s = os.fspath(path)
    if s.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + s[8:])
    if s.startswith("\\\\?\\"):
        return Path(s[4:])
    return Path(s)


def iterdir(path: str | os.PathLike) -> list[Path]:
    return [plain(p) for p in lp(path).iterdir()]


def is_file(path: str | os.PathLike) -> bool:
    return lp(path).is_file()


def size(path: str | os.PathLike) -> int:
    return lp(path).stat().st_size


def copy(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    lp(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(lp(src), lp(dst))


def long_paths_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def too_long(path: str | os.PathLike) -> bool:
    return len(os.path.abspath(os.fspath(path))) >= 260
