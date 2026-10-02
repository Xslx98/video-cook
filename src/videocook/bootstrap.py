"""Download, verify and extract the pinned toolchain; install VapourSynth plugins."""

from __future__ import annotations

import hashlib
import os
import json
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

from videocook.config import TOOLS_DIR, load_settings
from videocook.toolchain import lock, tool, venv_scripts

DOWNLOADS = TOOLS_DIR / "_downloads"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "video-cook-bootstrap"})
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    tmp.replace(dest)


def _install_tool(name: str, spec: dict) -> None:
    target = TOOLS_DIR / name
    marker = target / ".sha256"
    if marker.exists() and marker.read_text().strip() == spec["sha256"]:
        print(f"  {name} {spec['version']}: up to date")
        return

    archive = DOWNLOADS / spec["url"].rsplit("/", 1)[-1].replace("%2B", "+")
    if not archive.exists() or _sha256(archive) != spec["sha256"]:
        print(f"  {name}: downloading {spec['url']}")
        _download(spec["url"], archive)
    digest = _sha256(archive)
    if digest != spec["sha256"]:
        raise RuntimeError(f"{name}: SHA256 mismatch ({digest} != {spec['sha256']})")

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    kind = spec["archive"]
    if kind == "none":
        shutil.copy2(archive, target / next(iter(spec["bin"].values())))
    elif kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target)
    elif kind == "7z":
        subprocess.run(
            [str(tool("7zr")), "x", "-y", f"-o{target}", str(archive)],
            check=True,
            capture_output=True,
        )
    else:
        raise ValueError(f"{name}: unknown archive kind {kind!r}")
    marker.write_text(spec["sha256"])
    print(f"  {name} {spec['version']}: installed")


def install_tools() -> None:
    tools = lock()["tools"]
    # 7zr first: it extracts the other .7z archives.
    for name in sorted(tools, key=lambda n: n != "7zr"):
        _install_tool(name, tools[name])


def install_models() -> None:
    """AI models used by vs-jetpack (DPIR) go to the shared vsscale cache."""
    os.environ["VSSCALE_GLOBAL"] = "1"
    exe = str(venv_scripts() / "vsscale.exe")
    res = subprocess.run([exe, "onnx", "download", "DPIR", "--latest", "-y", "--global"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    print("  DPIR models:", "ok" if res.returncode == 0 else f"failed ({res.stderr.strip()[-200:]})")


# Plugins come from PyPI wheels pinned in uv.lock (see pyproject.toml); this is
# the set the pipeline relies on. GPU-only namespaces are checked separately.
REQUIRED_NAMESPACES = [
    "lsmas", "bs", "ffms2", "fmtc", "akarin", "vszip", "zsmooth", "znedi3", "nlm_ispc",
    "dfttest2_cpu", "bm3dvk", "mvu", "vivtc", "bwdif", "descale", "resize2", "placebo", "ov",
]
GPU_NAMESPACES = {"nvidia": ["nlm_cuda", "bm3dcuda", "trt", "vszipcu"], "opencl": ["vszipcl"]}


def verify() -> list[str]:
    problems: list[str] = []
    for exe in ("ffmpeg", "ffprobe", "x265", "mkvmerge", "mediainfo", "assfonts",
                "dovi_tool", "hdr10plus_tool", "vspipe"):
        try:
            tool(exe)
        except Exception as exc:  # noqa: BLE001
            problems.append(str(exc))
    import vapoursynth as vs

    loaded = {p.namespace for p in vs.core.plugins()}
    missing = [ns for ns in REQUIRED_NAMESPACES if ns not in loaded]
    if missing:
        problems.append(f"VapourSynth plugins not loaded: {', '.join(missing)}")
    for module in ("vstools", "vsdenoise", "vsdeband", "vsaa", "vsdeinterlace", "vsscale"):
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"python module {module}: {exc}")
    return problems


BENCH_VPY = """
import vapoursynth as vs
core = vs.core
clip = core.std.BlankClip(width={w}, height={h}, format=vs.YUV420P16, length={n},
                          fpsnum=24000, fpsden=1001, color=[20000, 32768, 32768])
clip = core.grain.Add(clip, var=40, uvar=10, constant=False)
clip = core.resize.Point(clip, format=vs.YUV420P10)
clip.set_output()
"""


def _bench_once(vpy: Path, parallel: int, frames: int) -> float:
    """Aggregate fps of `parallel` concurrent x265 encodes."""
    procs = []
    start = time.perf_counter()
    for _ in range(parallel):
        pipe = subprocess.Popen(
            [str(tool("vspipe")), "-c", "y4m", str(vpy), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        enc = subprocess.Popen(
            [str(tool("x265")), "--y4m", "--input", "-", "--output-depth", "10",
             "--preset", "slower", "--crf", "18", "--output", "NUL"],
            stdin=pipe.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pipe.stdout.close()
        procs.append((pipe, enc))
    for pipe, enc in procs:
        enc.wait()
        pipe.wait()
    return parallel * frames / (time.perf_counter() - start)


def benchmark() -> dict:
    settings = load_settings()
    work = TOOLS_DIR / "_bench"
    work.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    for label, (w, h, n) in {"1080p": (1920, 1080, 240), "2160p": (3840, 2160, 72)}.items():
        vpy = work / f"bench_{label}.vpy"
        vpy.write_text(BENCH_VPY.format(w=w, h=h, n=n), encoding="utf-8")
        fps = {p: _bench_once(vpy, p, n) for p in (1, 2)}
        best = 2 if fps[2] > fps[1] * 1.08 else 1
        result[label] = {"fps": fps, "parallel": best}
        print(f"  {label}: 1 job {fps[1]:.2f} fps, 2 jobs {fps[2]:.2f} fps -> parallel={best}")
    settings.bench_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(skip_bench: bool = False) -> int:
    print("[1/4] portable tools")
    install_tools()
    print("[2/4] AI models")
    install_models()
    print("[3/4] verification")
    problems = verify()
    for p in problems:
        print(f"  PROBLEM: {p}")
    if problems:
        return 1
    print("  all tools and plugins OK")
    if skip_bench:
        print("[4/4] benchmark skipped")
    else:
        print("[4/4] x265 parallelism benchmark (takes a few minutes)")
        benchmark()
    return 0
