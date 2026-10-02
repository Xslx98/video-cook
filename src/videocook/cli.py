"""`vcook` command line entry point."""

from __future__ import annotations

import argparse
import sys


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from videocook import bootstrap

    return bootstrap.main(skip_bench=args.skip_bench)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from videocook import bootstrap

    from videocook import fsutil

    problems = bootstrap.verify()
    if not fsutil.long_paths_enabled():
        problems.append("Windows LongPathsEnabled is 0: paths over 260 chars will break external tools")
    for p in problems:
        print(f"PROBLEM: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vcook", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap", help="install the pinned toolchain and plugins")
    p.add_argument("--skip-bench", action="store_true", help="skip the x265 benchmark")
    p.set_defaults(func=_cmd_bootstrap)

    p = sub.add_parser("doctor", help="verify the toolchain")
    p.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
