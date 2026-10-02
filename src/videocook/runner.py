"""Stage 5–6 — background execution, mux, QA and delivery (design §10, §14–16).

`vcook run <job>` detaches a worker process that survives the Claude session.
Progress lives in <job>/state.json; every step is idempotent, so re-running
resumes where it stopped. The worker blocks system sleep, runs encoders at
below-normal priority and sends a Windows toast when it finishes.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from videocook import encoder, naming, qa, script, tracks
from videocook.config import load_settings
from videocook.job import Job
from videocook.toolchain import run, tool

BELOW_NORMAL = 0x00004000
DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
STEPS = ("demux", "audio", "fonts", "video", "mux", "qa")


# --- state ------------------------------------------------------------------------

class State:
    def __init__(self, job: Job):
        self.path = job.dir / "state.json"
        self.lock = threading.Lock()
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.data.setdefault("units", {})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def unit(self, uid: str) -> dict:
        return self.data["units"].setdefault(uid, {"steps": {}, "progress": ""})

    def set(self, uid: str, **kw) -> None:
        with self.lock:
            self.unit(uid).update(kw)
            self.save()

    def step(self, uid: str, step: str, status: str) -> None:
        with self.lock:
            self.unit(uid)["steps"][step] = status
            self.save()

    def done(self, uid: str, step: str) -> bool:
        return self.unit(uid)["steps"].get(step) == "done"


# --- environment helpers --------------------------------------------------------------

def keep_awake(on: bool) -> None:
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except (AttributeError, OSError):
        pass


def toast(title: str, body: str) -> None:
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode({json.dumps(title)})) > $null
$x.Item(1).AppendChild($t.CreateTextNode({json.dumps(body)})) > $null
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, creationflags=0x08000000)


def parallelism(job: Job, height: int) -> int:
    if job.data["run"].get("parallel"):
        return int(job.data["run"]["parallel"])
    settings = load_settings()
    key = "2160p" if height > 1100 else "1080p"
    conf = settings.parallel_2160p if key == "2160p" else settings.parallel_1080p
    if conf != "auto":
        return int(conf)
    if settings.bench_file.exists():
        return int(json.loads(settings.bench_file.read_text())[key]["parallel"])
    return 1 if key == "2160p" else 2


def vpy_info(vpy: Path) -> dict:
    res = run("vspipe", "--info", vpy, "-", check=False)
    out = {}
    for line in res.stdout.splitlines():
        if not line.strip() and out:
            break  # only output 0 (output 1 is the untouched source)
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    if "frames" not in out:
        raise RuntimeError(f"vspipe --info failed for {vpy}: {res.stderr[-1500:]}")
    num, den = out["fps"].split(" ")[0].split("/")
    return {"frames": int(out["frames"]), "width": int(out["width"]), "height": int(out["height"]),
            "fps": int(num) / int(den), "fps_frac": f"{num}/{den}"}


# --- unit pipeline ------------------------------------------------------------------

class UnitRunner:
    def __init__(self, job: Job, unit: dict, state: State):
        self.job, self.unit, self.state = job, unit, state
        self.uid = unit["id"]
        self.work = job.sub("work", self.uid)
        self.logs = job.sub("logs")
        self.vpy = script.script_path(job, self.uid)
        self.trim = job.data["run"].get("trim") or []
        self.encode = job.data["video"]["mode"] == "encode"

    def plan(self) -> dict:
        ep = self.unit
        return {"audio": ep.get("audio", self.job.data["audio"]["tracks"]),
                "subs": ep.get("subtitles", self.job.data["subtitles"]["tracks"]),
                "ext_audio": ep.get("external_audio", self.job.data["audio"].get("external", [])),
                "ext_subs": ep.get("external_subs", self.job.data["subtitles"].get("external", []))}

    def do(self, step: str, fn) -> None:
        if self.state.done(self.uid, step):
            return
        self.state.step(self.uid, step, "running")
        fn()
        self.state.step(self.uid, step, "done")

    def run(self) -> None:
        try:
            self.info = vpy_info(self.vpy)
            self.state.set(self.uid, frames=self.info["frames"], error="", status="running")
            self.do("demux", self.step_demux)
            self.do("audio", self.step_audio)
            self.do("fonts", self.step_fonts)
            self.do("video", self.step_video)
            self.do("mux", self.step_mux)
            self.do("qa", self.step_qa)
            self.state.set(self.uid, status="done")
        except Exception as exc:  # noqa: BLE001
            self.state.set(self.uid, status="failed", error=f"{exc}\n{traceback.format_exc()[-3000:]}")
            raise

    # -- steps ----------------------------------------------------------------------
    def source_fps(self) -> float:
        return float(self.job.stream_facts()["fps"])

    def step_demux(self) -> None:
        tracks.demux(self.unit, self.plan(), self.work, self.trim, self.source_fps())

    def step_audio(self) -> None:
        entries = tracks.convert_audio(self.plan(), self.work / "tracks.mka", self.work, self.unit)
        (self.work / "audio.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def step_fonts(self) -> None:
        ass = [Path(s["file"]) for s in self.plan()["ext_subs"]
               if s["action"] != "drop" and s["file"].lower().endswith((".ass", ".ssa"))]
        result = {"fonts": [], "missing": [], "ok": True}
        if ass:
            dirs = self.job.data["subtitles"].get("font_dirs", [])
            result = tracks.subset_fonts(ass, dirs, self.work / "fonts", self.job.sub("cache", "fontdb"))
            (self.logs / f"{self.uid}_assfonts.log").write_text(result.pop("log"), encoding="utf-8")
        (self.work / "fonts.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if not result["ok"] and not self.job.data["subtitles"].get("allow_missing_fonts"):
            raise RuntimeError(f"fonts missing for subtitles: {result['missing']} "
                               f"{result.get('missing_glyphs', [])} — add a font folder to "
                               "subtitles.font_dirs (or, with the user's consent, set "
                               "subtitles.allow_missing_fonts = true) and re-run")

    def step_video(self) -> None:
        if not self.encode:
            return
        facts = self.job.stream_facts()
        qp = None
        if self.job.data["chapters"].get("keyframes") and not self.trim \
                and not self.job.data["video"].get("filters", {}).get("field"):
            qp = tracks.qpfile(tracks.chapter_times(self.unit), self.info["fps"], self.info["frames"],
                               self.work / "chapters.qp")
        args = encoder.build(self.job.data["video"], facts["colour"], facts["hdr"], self.info["height"],
                             qpfile=str(qp) if qp else None)
        out = self.work / "video.hevc"
        log = self.logs / f"{self.uid}_x265.log"
        with log.open("w", encoding="utf-8") as lf:
            lf.write("x265 " + " ".join(args) + "\n")
            lf.flush()
            pipe = subprocess.Popen([str(tool("vspipe")), "-c", "y4m", str(self.vpy), "-"],
                                    stdout=subprocess.PIPE, stderr=lf, creationflags=BELOW_NORMAL)
            enc = subprocess.Popen([str(tool("x265")), "--y4m", "--input", "-", *args, "--output", str(out)],
                                   stdin=pipe.stdout, stderr=subprocess.PIPE, stdout=lf,
                                   creationflags=BELOW_NORMAL)
            pipe.stdout.close()
            self.state.set(self.uid, pids=[pipe.pid, enc.pid])
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
                        if time.time() - last > 5:
                            self.state.set(self.uid, progress=line)
                            last = time.time()
                    else:
                        lf.write(line + "\n")
            enc.wait()
            pipe.wait()
        if enc.returncode != 0 or pipe.returncode != 0:
            raise RuntimeError(f"encode failed (x265 {enc.returncode}, vspipe {pipe.returncode}); see {log}")

    def _av_mka(self) -> Path:
        """All final audio + subtitle tracks with metadata, trimmed if --trim is set."""
        plan = self.plan()
        entries = json.loads((self.work / "audio.json").read_text(encoding="utf-8"))
        mka = self.work / "tracks.mka"
        out = self.work / "av.mka"
        args = ["-o", str(out)]
        inputs: list[list[str]] = []
        order: list[str] = []
        mka_input = None

        def meta(tid: str, t: dict) -> list[str]:
            m = ["--language", f"{tid}:{t.get('language') or 'und'}",
                 "--track-name", f"{tid}:{t.get('title') or ''}",
                 "--default-track-flag", f"{tid}:{'yes' if t.get('default') else 'no'}"]
            if t.get("forced"):
                m += ["--forced-display-flag", f"{tid}:yes"]
            return m

        mka_meta: list[str] = []
        mka_audio, mka_subs = [], []
        for e in entries:
            if "file" in e:
                inputs.append([*meta("0", e), *(["--sync", f"0:{e['delay_ms']}"] if e.get("delay_ms") else []),
                               e["file"]])
                order.append(f"{len(inputs) - 1}:0")
            else:
                if mka_input is None:
                    mka_input = len(inputs)
                    inputs.append([])  # placeholder
                mka_audio.append(str(e["mka_id"]))
                mka_meta += meta(str(e["mka_id"]), e)
                order.append(f"{mka_input}:{e['mka_id']}")
        for k, e in enumerate(plan["ext_audio"]):
            if e["action"] != "drop":
                f = e["file"]
                if self.trim:
                    f = str(tracks.trim_external_audio(Path(f), self.trim, self.source_fps(),
                                                       self.work / f"ext_audio_{k}_trim.flac"))
                inputs.append([*meta("-1", e), f])
                order.append(f"{len(inputs) - 1}:0")
        n_audio_kept = len([t for t in plan["audio"] if t["action"] != "drop" and "index" in t])
        for k, s in enumerate([t for t in plan["subs"] if t["action"] != "drop" and "index" in t]):
            mka_id = n_audio_kept + k
            if mka_input is None:
                mka_input = len(inputs)
                inputs.append([])
            mka_subs.append(str(mka_id))
            mka_meta += meta(str(mka_id), s)
            order.append(f"{mka_input}:{mka_id}")
        for k, s in enumerate(plan["ext_subs"]):
            if s["action"] != "drop":
                f = s["file"]
                if self.trim:
                    f = str(self._trim_text_sub(Path(f), k))
                inputs.append([*meta("-1", s), "--sub-charset", "-1:UTF-8", f])
                order.append(f"{len(inputs) - 1}:0")
        if mka_input is not None:
            sel = (["--audio-tracks", ",".join(mka_audio)] if mka_audio else ["-A"]) + \
                  (["--subtitle-tracks", ",".join(mka_subs)] if mka_subs else ["-S"])
            inputs[mka_input] = [*sel, *mka_meta, "--no-chapters", str(mka)]
        for inp in inputs:
            args += ["--no-chapters", *inp] if inp and inp[-1] != str(mka) else inp
        if order:
            args += ["--track-order", ",".join(order)]
        res = run("mkvmerge", *args, check=False)
        if res.returncode >= 2:
            raise RuntimeError(f"mkvmerge (audio/subs) failed: {res.stdout[-2000:]}")
        return out

    def _trim_text_sub(self, sub: Path, k: int) -> Path:
        """Trim an external text subtitle for test runs (mkvmerge can split text tracks)."""
        out = self.work / f"ext_sub_{k}_trim.mks"
        if not out.exists():
            part = self.work / f"ext_sub_{k}_trim.part.mks"
            res = run("mkvmerge", "-o", part, "--split", tracks.split_parts(self.trim, self.source_fps()),
                      "--sub-charset", "0:UTF-8", sub, check=False)
            if res.returncode >= 2:
                raise RuntimeError(f"trimming subtitle {sub.name} failed: {res.stdout[-1000:]}")
            tracks._single_output(part)
            os.replace(part, out)
        return out

    def output_path(self) -> Path:
        rel = naming.relative_path(self.job, self.unit, self.info["height"], self.info["width"])
        return self.job.sub("out") / rel

    def step_mux(self) -> None:
        av = self._av_mka()
        out = self.output_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        j = self.job.data["job"]
        title = j["title"] or j["name"]
        args = ["-o", str(out), "--title", f"{title} ({j['year']})" if j.get("year") else title]
        if self.encode:
            args += ["--language", "0:und", "--track-name", "0:", "--default-duration",
                     f"0:{self.info['fps_frac']}p", str(self.work / "video.hevc")]
        else:
            args += ["-A", "-S", "--no-chapters", "--no-attachments", "--no-global-tags",
                     "--track-name", "0:", str(self.unit["source"]["path"])]
        args += [str(av)]
        fonts = json.loads((self.work / "fonts.json").read_text(encoding="utf-8"))["fonts"]
        for f in fonts:
            args += ["--attachment-mime-type", "font/otf" if f.lower().endswith(".otf") else "font/ttf",
                     "--attach-file", f]
        self.chapters = None
        if not self.trim and self.job.data["chapters"].get("mode") != "none":
            xml = tracks.chapters_xml(tracks.chapter_times(self.unit),
                                      self.job.data["chapters"].get("language", "eng"),
                                      self.work / "chapters.xml")
            if xml:
                args += ["--chapters", str(xml)]
        res = run("mkvmerge", *args, check=False)
        if res.returncode >= 2:
            raise RuntimeError(f"final mux failed: {res.stdout[-2000:]}")
        self.state.set(self.uid, output=str(out))

    def step_qa(self) -> None:
        out = Path(self.state.unit(self.uid)["output"])
        facts = self.job.stream_facts()
        plan = self.plan()
        n_audio = len([t for t in plan["audio"] if t["action"] != "drop" and "index" in t]) + \
            len([t for t in plan["ext_audio"] if t["action"] != "drop"])
        n_subs = len([t for t in plan["subs"] if t["action"] != "drop" and "index" in t]) + \
            len([t for t in plan["ext_subs"] if t["action"] != "drop"])
        chapters = None
        xml = self.work / "chapters.xml"
        if xml.exists():
            chapters = xml.read_text(encoding="utf-8").count("<ChapterAtom>")
        expect = {"frames": self.info["frames"], "fps": self.info["fps"], "audio": n_audio, "subs": n_subs,
                  "chapters": chapters, "encoded": self.encode, "trimmed": bool(self.trim), "hdr10": facts["hdr"].get("hdr10"),
                  "primaries": facts["colour"].get("primaries") if self.encode else None,
                  "transfer": facts["colour"].get("transfer") if self.encode else None,
                  "matrix": facts["colour"].get("matrix") if self.encode else None}
        qa_dir = self.job.sub("qa")
        report = qa.check(self.uid, out, expect, qa_dir)
        try:
            from videocook.compare import picks_for
            from videocook.vsource import open_trimmed

            src = open_trimmed(self.unit["source"], self.job.cache, self.trim)
            crop = self.job.data["video"].get("filters", {}).get("crop") or {}
            report["screenshots"] = qa.screenshots(self.uid, out, src, picks_for(self.job, self.uid),
                                                   facts["colour"], crop, qa_dir)
        except Exception as exc:  # noqa: BLE001 — screenshots never block
            report["screenshots_error"] = str(exc)
        (qa_dir / f"{self.uid}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.state.set(self.uid, qa_passed=report["passed"], crc32=report["crc32"])
        if not report["passed"]:
            bad = [c["check"] for c in report["checks"] if not c["ok"] and c["blocking"]]
            raise RuntimeError(f"QA failed: {bad}")


# --- job level ------------------------------------------------------------------------

def run_job(job: Job, units: list[str] | None = None, retry_failed: bool = True) -> int:
    state = State(job)
    state.data.update({"status": "running", "pid": os.getpid(),
                       "started": dt.datetime.now().isoformat(timespec="seconds"), "finished": ""})
    state.save()
    script.generate(job)
    todo = [u for u in job.units() if not units or u["id"] in units]
    for u in todo:
        st = state.unit(u["id"])
        if retry_failed and st.get("status") == "failed":
            for k, v in list(st["steps"].items()):
                if v != "done":
                    st["steps"].pop(k)
    state.save()
    keep_awake(True)
    failures = 0
    try:
        height = job.stream_facts()["height"]
        with ThreadPoolExecutor(max_workers=max(1, parallelism(job, height))) as pool:
            futures = {pool.submit(UnitRunner(job, u, state).run): u["id"] for u in todo}
            for fut, uid in futures.items():
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    failures += 1
    finally:
        keep_awake(False)
    state.data.update({"status": "failed" if failures else "done",
                       "finished": dt.datetime.now().isoformat(timespec="seconds")})
    state.save()
    name = job.data["job"]["title"] or job.data["job"]["name"]
    toast("video-cook", f"{name}: {'%d unit(s) failed' % failures if failures else 'finished, QA passed'}")
    return 1 if failures else 0


def detach(job: Job, extra: list[str]) -> int:
    log = job.sub("logs") / "run.log"
    with log.open("a", encoding="utf-8") as lf:
        proc = subprocess.Popen([sys.executable, "-m", "videocook.cli", "run", str(job.dir), "--foreground", *extra],
                                stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=DETACHED, close_fds=True)
    print(f"started worker pid {proc.pid}; progress: vcook status {job.data['job']['name']}")
    return 0


def status_text(job: Job) -> str:
    path = job.dir / "state.json"
    if not path.exists():
        return f"{job.data['job']['name']}: not started"
    st = json.loads(path.read_text(encoding="utf-8"))
    alive = _pid_alive(st.get("pid"))
    lines = [f"{job.data['job']['name']}: {st.get('status')} (worker {'alive' if alive else 'not running'}), "
             f"started {st.get('started')}, finished {st.get('finished') or '-'}"]
    for uid, u in st["units"].items():
        steps = " ".join(f"{k}:{'✓' if v == 'done' else v}" for k, v in u["steps"].items())
        lines.append(f"  {uid}: {u.get('status', 'pending')} | {steps} | {u.get('progress', '')}")
        if u.get("error"):
            lines.append("    error: " + u["error"].splitlines()[0])
    return "\n".join(lines)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    return str(pid) in res.stdout


# --- delivery --------------------------------------------------------------------------

def deliver(job: Job, nas_dir: str | None = None, keep_work: bool = False) -> int:
    from videocook import fsutil

    st = State(job)
    target = Path(nas_dir or job.data["output"].get("nas_dir") or "")
    if not str(target) or str(target) == ".":
        print("output.nas_dir is not set — ask the user where to deliver")
        return 2
    not_ready = [uid for uid, u in st.data["units"].items() if not u.get("qa_passed")]
    if not_ready:
        print(f"QA has not passed for: {not_ready}")
        return 1
    out_root = job.dir / "out"
    for uid, u in st.data["units"].items():
        src = Path(u["output"])
        rel = src.relative_to(out_root)
        dst = target / rel
        print(f"copy {rel} → {dst}")
        fsutil.copy(src, dst)
        crc = qa.crc32(fsutil.lp(dst))
        if crc != u["crc32"]:
            print(f"CRC mismatch after copy for {uid}: {crc} != {u['crc32']}")
            return 1
        st.set(uid, delivered=str(dst))
    backup = target / ".videocook" / job.data["job"]["name"]
    for name in ("job.toml", "probe.json", "probe.md", "state.json"):
        if (job.dir / name).exists():
            fsutil.copy(job.dir / name, backup / name)
    for sub in ("scripts", "qa"):
        for f in (job.dir / sub).rglob("*.vpy" if sub == "scripts" else "*.json"):
            fsutil.copy(f, backup / sub / f.name)
    if not keep_work:
        shutil.rmtree(job.dir / "work", ignore_errors=True)
        shutil.rmtree(job.dir / "trial" / "encode", ignore_errors=True)
    job.data["output"]["nas_dir"] = str(target)
    job.data["job"]["status"] = "delivered"
    job.save()
    print("delivered; job config backed up to", backup)
    return 0


# --- CLI ---------------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    job = Job.resolve(args.job)
    if args.foreground:
        return run_job(job, args.unit)
    extra = sum((["--unit", u] for u in args.unit or []), [])
    return detach(job, extra)


def _cmd_status(args: argparse.Namespace) -> int:
    if args.job:
        print(status_text(Job.resolve(args.job)))
        return 0
    jobs_dir = load_settings().jobs_dir
    for d in sorted(jobs_dir.iterdir()) if jobs_dir.exists() else []:
        if (d / "job.toml").exists():
            print(status_text(Job(d)).splitlines()[0])
    return 0


def _cmd_deliver(args: argparse.Namespace) -> int:
    return deliver(Job.resolve(args.job), args.to, args.keep_work)


def _cmd_stop(args: argparse.Namespace) -> int:
    job = Job.resolve(args.job)
    st = State(job)
    pids = [st.data.get("pid")] + [p for u in st.data["units"].values() for p in u.get("pids", [])]
    for pid in filter(None, pids):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    st.data["status"] = "stopped"
    st.save()
    print("stopped")
    return 0


def add_commands(sub) -> None:
    p = sub.add_parser("run", help="stage 5: encode, mux and QA in a detached worker")
    p.add_argument("job")
    p.add_argument("--unit", action="append", help="only these episode ids")
    p.add_argument("--foreground", action="store_true", help="run in this process")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("status", help="progress of one job or all jobs")
    p.add_argument("job", nargs="?")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("stop", help="kill a running worker")
    p.add_argument("job")
    p.set_defaults(func=_cmd_stop)

    p = sub.add_parser("deliver", help="stage 6: copy QA-passed outputs to the NAS, verify CRC, clean up")
    p.add_argument("job")
    p.add_argument("--to", help="NAS output directory (else job.toml output.nas_dir)")
    p.add_argument("--keep-work", action="store_true")
    p.set_defaults(func=_cmd_deliver)
