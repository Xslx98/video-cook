"""vspipe → x265 encodes with retries and failure forensics.

On Windows vspipe intermittently fails to write to the x265 pipe (`fwrite() call
failed when writing video plane 0, errno: 22`); x265 then sees EOF and exits 0
with a short output (design.md, "vspipe → x265 pipe investigation"). Every
attempt therefore checks the encoded frame count, a failed attempt is retried
with its output discarded, and each failure appends one JSON line to
`<log dir>/encode_failures.jsonl` with what is needed to analyse it later:
exit codes, exit order and timing, stderr tails and system memory state.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from videocook.toolchain import tool

FAILURE_LOG = "encode_failures.jsonl"
_ENCODED = re.compile(r"encoded (\d+) frames")


class EncodeError(RuntimeError):
    pass


def encoded_frames(x265_output: str) -> int | None:
    """Frame count from x265's final summary line, None when it never got there."""
    hits = _ENCODED.findall(x265_output)
    return int(hits[-1]) if hits else None


def error_line(stderr: str) -> str:
    """Most telling line of a tool's stderr: the last one mentioning an error, else the last one.

    vspipe ends with an `Output N frames` summary even after an fwrite failure."""
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    hits = [ln for ln in lines if re.search(r"error|failed|exception", ln, re.IGNORECASE)]
    return (hits or lines or [""])[-1].strip()


def classify(vspipe_rc: int | None, x265_rc: int | None, encoded: int | None, expected: int) -> str | None:
    """Failure kind of one attempt, None when it succeeded."""
    if x265_rc != 0:
        return "x265_error"
    if vspipe_rc != 0:
        return "vspipe_error"
    if encoded != expected:
        return "short_encode"
    return None


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory_status() -> dict:
    """System memory snapshot (GiB); the pipe failure may be a resource error mapped to EINVAL."""
    try:
        st = _MemoryStatus()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {}
    except (AttributeError, OSError):
        return {}
    gib = 2**30
    return {"load_pct": st.dwMemoryLoad,
            "phys_avail_gib": round(st.ullAvailPhys / gib, 2), "phys_total_gib": round(st.ullTotalPhys / gib, 2),
            "commit_avail_gib": round(st.ullAvailPageFile / gib, 2),
            "commit_total_gib": round(st.ullTotalPageFile / gib, 2)}


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _waiter(proc: subprocess.Popen, t0: float, exits: dict, key: str) -> threading.Thread:
    def wait() -> None:
        proc.wait()
        exits[key] = round(time.perf_counter() - t0, 3)

    th = threading.Thread(target=wait, daemon=True)
    th.start()
    return th


def _attempt(vpy: Path, x265_args: list[str], out: Path, log: Path, expected: int,
             frame_range: tuple[int, int] | None, priority: int,
             on_progress: Callable[[str], None] | None,
             on_pids: Callable[[list[int]], None] | None) -> dict:
    vspipe_cmd = [str(tool("vspipe")), "-c", "y4m"]
    if frame_range:
        vspipe_cmd += ["-s", str(frame_range[0]), "-e", str(frame_range[1])]  # inclusive
    vspipe_cmd += [str(vpy), "-"]
    x265_cmd = [str(tool("x265")), "--y4m", "--input", "-", *x265_args, "--output", str(out)]
    vs_log = log.with_suffix(".vspipe.log")
    x265_lines: list[str] = []
    exits: dict = {}
    started = dt.datetime.now().isoformat(timespec="seconds")
    with log.open("w", encoding="utf-8") as lf, vs_log.open("w", encoding="utf-8") as vf:
        lf.write(f"started {started}\nvspipe: {' '.join(vspipe_cmd)}\nx265: {' '.join(x265_cmd)}\n"
                 f"expected frames: {expected}\n\n")
        lf.flush()
        t0 = time.perf_counter()
        pipe = subprocess.Popen(vspipe_cmd, stdout=subprocess.PIPE, stderr=vf, creationflags=priority)
        enc = subprocess.Popen(x265_cmd, stdin=pipe.stdout, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               creationflags=priority)
        pipe.stdout.close()
        waiters = [_waiter(pipe, t0, exits, "vspipe"), _waiter(enc, t0, exits, "x265")]
        if on_pids:
            on_pids([pipe.pid, enc.pid])
        buf = b""
        last = 0.0
        while True:
            ch = enc.stderr.read(256)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts[-1]
            for p in parts[:-1]:
                line = p.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("["):  # progress line
                    if on_progress and time.time() - last > 5:
                        on_progress(line)
                        last = time.time()
                else:
                    x265_lines.append(line)
                    lf.write(line + "\n")
        if buf.strip():
            x265_lines.append(buf.decode("utf-8", "replace").strip())
            lf.write(x265_lines[-1] + "\n")
        for th in waiters:
            th.join()
        x265_text = "\n".join(x265_lines)
        encoded = encoded_frames(x265_text)
        failure = classify(pipe.returncode, enc.returncode, encoded, expected)
        lf.write(f"\nvspipe exit {pipe.returncode} at {exits.get('vspipe')} s, "
                 f"x265 exit {enc.returncode} at {exits.get('x265')} s\n"
                 f"encoded {encoded} of {expected} frames -> {failure or 'ok'}\n")
    result = {"time": started, "vpy": str(vpy), "range": list(frame_range) if frame_range else None,
              "expected": expected, "encoded": encoded, "failure": failure,
              "vspipe_rc": pipe.returncode, "x265_rc": enc.returncode,
              "vspipe_exit_s": exits.get("vspipe"), "x265_exit_s": exits.get("x265"),
              "log": str(log), "vspipe_log": str(vs_log)}
    if failure:
        result.update({"vspipe_stderr": _tail(vs_log.read_text(encoding="utf-8", errors="replace")),
                       "x265_stderr": _tail(x265_text), "memory": memory_status(),
                       "output_bytes": out.stat().st_size if out.exists() else 0})
    return result


def encode(vpy: Path, x265_args: list[str], out: Path, log: Path, expected: int, *,
           frame_range: tuple[int, int] | None = None, attempts: int = 3, priority: int = 0,
           on_progress: Callable[[str], None] | None = None,
           on_pids: Callable[[list[int]], None] | None = None,
           on_retry: Callable[[dict], None] | None = None,
           failure_log: Path | None = None) -> dict:
    """Encode `vpy` (optionally frames [start, end], inclusive) to `out`, retrying broken attempts.

    `log` is the first attempt's log; retries write `<stem>_try<N>.log`. Returns the
    successful attempt's record plus `attempts`; raises EncodeError after the last one.
    Failure records go to `failure_log` (default `<log dir>/encode_failures.jsonl`).
    """
    failure_log = failure_log or log.parent / FAILURE_LOG
    out.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    failures: list[dict] = []
    for attempt in range(1, attempts + 1):
        alog = log if attempt == 1 else log.with_name(f"{log.stem}_try{attempt}{log.suffix}")
        rec = _attempt(vpy, x265_args, out, alog, expected, frame_range, priority, on_progress, on_pids)
        rec["attempt"] = attempt
        if not rec["failure"]:
            return {**rec, "attempts": attempt}
        out.unlink(missing_ok=True)
        failures.append(rec)
        with failure_log.open("a", encoding="utf-8") as ff:
            ff.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if attempt < attempts and on_retry:
            on_retry(rec)
    summary = "; ".join(f"try {r['attempt']}: {r['failure']} ({r['encoded']}/{r['expected']} frames, "
                        f"vspipe {r['vspipe_rc']}, x265 {r['x265_rc']})" for r in failures)
    last = failures[-1]
    detail = error_line(last["vspipe_stderr"] if last["failure"] != "x265_error" else last["x265_stderr"])
    raise EncodeError(f"encode failed after {attempts} attempts: {summary}"
                      + (f"; last error: {detail}" if detail else "")
                      + f"; see {last['log']} and {failure_log}")
