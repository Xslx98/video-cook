"""`vcook` — assessment-first encoding pipeline (see docs/design.md)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_trim(values: list[str] | None) -> list[list[int]]:
    """'--trim 1000:1720 --trim 30000:30720' → [[1000, 1720], [30000, 30720]]."""
    out = []
    for v in values or []:
        a, b = v.split(":")
        out.append([int(a), int(b)])
    return out


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from videocook import bootstrap

    return bootstrap.main(skip_bench=args.skip_bench)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from videocook import bootstrap, fsutil

    problems = bootstrap.verify()
    import vapoursynth as vs

    loaded = {p.namespace for p in vs.core.plugins()}
    for group, names in bootstrap.GPU_NAMESPACES.items():
        have = [n for n in names if n in loaded]
        lacking = [n for n in names if n not in loaded]
        print(f"GPU {group}: {', '.join(have) or 'not available'}"
              + (f" (not loaded: {', '.join(lacking)}; filters fall back to other backends)"
                 if have and lacking else ""))
    print(f"VapourSynth {vs.__version__}")
    if not fsutil.long_paths_enabled():
        problems.append("Windows LongPathsEnabled is 0: paths over 260 chars will break external tools")
    for p in problems:
        print(f"PROBLEM: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _cmd_probe(args: argparse.Namespace) -> int:
    from videocook import probe

    result = probe.probe(Path(args.source))
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else probe.to_markdown(result))
    return 0


def _cmd_new(args: argparse.Namespace) -> int:
    from videocook import intake
    from videocook.job import Job

    trim = _parse_trim(args.trim)
    if args.from_job:
        job = intake.derive_job(Job.resolve(args.from_job), Path(args.source), args.name, trim)
    else:
        job = intake.new_job(Path(args.source), args.name, trim)
    print(job.dir)
    print((job.dir / "probe.md").read_text(encoding="utf-8"))
    return 0


def _assess_budget(job, override: int | None) -> int:
    if override:
        return override
    return 8 if job.is_series else 24


def _cmd_assess(args: argparse.Namespace) -> int:
    from videocook import assess
    from videocook.job import Job

    job = Job.resolve(args.job)
    facts = job.stream_facts()
    budget = _assess_budget(job, args.budget)
    trim = job.data["run"].get("trim") or None
    units = job.units()
    if args.unit:
        units = [u for u in units if u["id"] in args.unit]
    for u in units:
        out = job.dir / "assess" / (u["id"] if job.is_series else "")
        print(f"[assess] {u['id']}")
        assess.run(job.dir, u["source"], facts["colour"], budget, trim=trim,
                   rescan=args.rescan, out=out)
        print(out / "report.md")
    if job.is_series and len(units) > 1:
        from videocook import season

        print(season.summary(job))
    return 0


def _cmd_script(args: argparse.Namespace) -> int:
    from videocook import script
    from videocook.job import Job

    for p in script.generate(Job.resolve(args.job)):
        print(p)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    from videocook import compare, script
    from videocook.job import Job

    job = Job.resolve(args.job)
    script.generate(job)
    unit = args.unit or job.units()[0]["id"]
    for p in compare.triptychs(job, unit):
        print(p)
    return 0


def _cmd_trial(args: argparse.Namespace) -> int:
    from videocook import script, trial
    from videocook.job import Job

    job = Job.resolve(args.job)
    script.generate(job)
    r = trial.run(job, args.unit, quick=args.quick)
    print(trial.report_md(r))
    return 0


def _cmd_fastcheck(args: argparse.Namespace) -> int:
    from videocook import fasttrack
    from videocook.job import Job

    job = Job.resolve(args.job)
    if not job.data["job"].get("derived_from"):
        print("this job was not derived from another job (use `vcook new --from`)")
        return 2
    r = fasttrack.check(job)
    print("fast track OK" if r["fast_track"] else "leave fast track:")
    for m in r["mismatches"]:
        print(f"  - {m}")
    return 0 if r["fast_track"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vcook", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap", help="install the pinned toolchain and plugins")
    p.add_argument("--skip-bench", action="store_true", help="skip the x265 benchmark")
    p.set_defaults(func=_cmd_bootstrap)

    p = sub.add_parser("doctor", help="verify the toolchain")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("probe", help="probe a source without creating a job")
    p.add_argument("source")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_probe)

    p = sub.add_parser("new", help="stage 1: probe a source and create a job")
    p.add_argument("source")
    p.add_argument("--name", help="job name (default: derived from the source)")
    p.add_argument("--from", dest="from_job", help="fast track: derive decisions from a past job")
    p.add_argument("--trim", action="append", metavar="A:B",
                   help="only process frames [A, B) (repeatable; for test runs)")
    p.set_defaults(func=_cmd_new)

    p = sub.add_parser("assess", help="stage 2: scan, sample frames and write the report")
    p.add_argument("job")
    p.add_argument("--unit", action="append", help="episode id(s) to assess (default: all)")
    p.add_argument("--budget", type=int, help="sampled frames per unit (default 24 film / 8 episode)")
    p.add_argument("--rescan", action="store_true")
    p.set_defaults(func=_cmd_assess)

    p = sub.add_parser("script", help="generate .vpy scripts from job.toml")
    p.add_argument("job")
    p.set_defaults(func=_cmd_script)

    p = sub.add_parser("compare", help="stage 4a: source/filtered/difference triptychs")
    p.add_argument("job")
    p.add_argument("--unit")
    p.set_defaults(func=_cmd_compare)

    p = sub.add_parser("trial", help="stage 4b: test clips with final x265 settings")
    p.add_argument("job")
    p.add_argument("--unit")
    p.add_argument("--quick", action="store_true", help="one risk clip, no size estimate")
    p.set_defaults(func=_cmd_trial)

    p = sub.add_parser("fastcheck", help="fast track: compare a derived job with its reference")
    p.add_argument("job")
    p.set_defaults(func=_cmd_fastcheck)

    from videocook import runner, units

    runner.add_commands(sub)
    units.add_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
