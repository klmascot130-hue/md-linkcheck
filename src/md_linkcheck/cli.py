"""Command-line interface for md-linkcheck."""

from __future__ import annotations

import argparse
import json
import sys

from .checker import CheckResult, Config, check_paths, summarize

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="md-linkcheck",
        description="Check HTTP(S) and local links in Markdown files.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Markdown files or directories to check (directories are scanned recursively).",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds (default: 10).")
    parser.add_argument("--workers", type=int, default=10, help="Concurrent HTTP checks (default: 10).")
    parser.add_argument("--retries", type=int, default=1, help="Retries per URL after the first attempt (default: 1).")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REGEX",
        help="Skip URLs matching this regex. Repeatable.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--only-external",
        action="store_true",
        help="Only check http(s) links; skip local file links.",
    )
    parser.add_argument(
        "--only-local",
        action="store_true",
        help="Only check local file links; skip http(s) links.",
    )
    return parser


def format_text(path: str, results: list[CheckResult]) -> list[str]:
    lines: list[str] = []
    for res in results:
        if res.note == "excluded" or res.ok:
            continue
        detail = res.error or (f"HTTP {res.status}" if res.status else "failed")
        lines.append(f"{path}:{res.link.line}: BROKEN {res.link.url} ({detail})")
    return lines


def format_json(all_results: dict[str, list[CheckResult]]) -> str:
    payload = {}
    for path, results in all_results.items():
        payload[path] = [
            {
                "url": r.link.url,
                "line": r.link.line,
                "kind": r.link.kind,
                "ok": r.ok,
                "status": r.status,
                "error": r.error,
            }
            for r in results
            if r.note != "excluded"
        ]
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.only_external and args.only_local:
        parser.error("--only-external and --only-local are mutually exclusive")

    config = Config(
        timeout=args.timeout,
        workers=args.workers,
        retries=args.retries,
        excludes=tuple(args.exclude),
        check_local=not args.only_external,
        check_external=not args.only_local,
    )

    try:
        all_results = check_paths(args.paths, config)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR

    broken_lines: list[str] = []
    if args.format == "json":
        print(format_json(all_results))
    else:
        for path, results in all_results.items():
            broken_lines.extend(format_text(path, results))
        for line in broken_lines:
            print(line)

    _files, total, broken = summarize(all_results)
    if args.format == "text":
        print(f"\n{total} link(s) checked, {broken} broken.")
    return EXIT_BROKEN if broken else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
