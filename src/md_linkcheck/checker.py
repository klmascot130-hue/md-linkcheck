"""Core link extraction and checking logic for md-linkcheck.

Zero third-party dependencies: everything is stdlib (urllib, re,
concurrent.futures).
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

USER_AGENT = "md-linkcheck/0.1.0 (+https://github.com/klmascot130-hue/md-linkcheck)"

#: Schemes that are never fetched over the network.
SKIP_SCHEMES = {"mailto", "tel", "ftp", "data", "javascript", "file"}

INLINE_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE_LINK_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
AUTOLINK_RE = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
BARE_URL_RE = re.compile(r"(?<!\]\()(?<!<)(https?://[^\s<>)\"']+)")
REF_DEF_RE = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)
REF_USE_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")


@dataclass
class Link:
    url: str
    line: int
    kind: str  # "link", "image", "reference", "autolink", "bare"


@dataclass
class CheckResult:
    link: Link
    ok: bool
    status: int | None = None
    error: str | None = None
    note: str | None = None


@dataclass
class Config:
    timeout: float = 10.0
    workers: int = 10
    retries: int = 1
    excludes: tuple[str, ...] = ()
    check_local: bool = True
    check_external: bool = True
    user_agent: str = USER_AGENT


def _strip_code(text: str) -> str:
    """Blank out fenced and inline code (keeping line numbers) so example
    links inside them are not checked."""

    def _blank(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    text = FENCED_CODE_RE.sub(_blank, text)
    text = INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    return text


def extract_links(markdown: str) -> list[Link]:
    """Extract all links from a markdown string, with 1-based line numbers."""
    links: list[Link] = []
    seen: set[tuple[int, str]] = set()

    def add(url: str, line: int, kind: str) -> None:
        key = (line, url)
        if key not in seen:
            seen.add(key)
            links.append(Link(url=url, line=line, kind=kind))

    # Reference definitions: [label]: url
    ref_targets = {label.strip().lower(): url for label, url in REF_DEF_RE.findall(markdown)}

    body = _strip_code(markdown)
    lines = body.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for _text, url in IMAGE_LINK_RE.findall(line):
            add(url, lineno, "image")
        for _text, url in INLINE_LINK_RE.findall(line):
            add(url, lineno, "link")
        for url in AUTOLINK_RE.findall(line):
            add(url, lineno, "autolink")
        for url in BARE_URL_RE.findall(line):
            add(url.rstrip(".,;!?)"), lineno, "bare")

    # Reference-style links: [text][label] / [label][] / [label]
    ref_uses: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        for _text, label in REF_USE_RE.findall(line):
            ref_uses.append((lineno, label or _text))
    for lineno, line in enumerate(lines, start=1):
        for match in re.finditer(r"\[([^\]]+)\](?!\()", line):
            label = match.group(1)
            if label.strip().lower() in ref_targets:
                ref_uses.append((lineno, label))
    for lineno, label in ref_uses:
        target = ref_targets.get(label.strip().lower())
        if target:
            add(target, lineno, "reference")

    links.sort(key=lambda link: (link.line, link.url))
    return links


def is_external(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def is_skippable(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in SKIP_SCHEMES or (not parsed.scheme and url.startswith("#"))


def _check_http(url: str, config: Config) -> CheckResult | None:
    """Return a CheckResult for an http(s) URL, or None if skipped/excluded."""
    if any(re.search(pattern, url) for pattern in config.excludes):
        return CheckResult(link=Link(url, 0, "link"), ok=True, note="excluded")
    last_error: str | None = None
    for _attempt in range(config.retries + 1):
        # HEAD first (cheap); some servers reject HEAD, so fall back to GET.
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(
                url, method=method, headers={"User-Agent": config.user_agent}
            )
            try:
                with urllib.request.urlopen(req, timeout=config.timeout) as resp:
                    return CheckResult(
                        link=Link(url, 0, "link"),
                        ok=resp.status < 400,
                        status=resp.status,
                    )
            except urllib.error.HTTPError as exc:
                if method == "HEAD" and exc.code in (400, 403, 405, 501):
                    continue  # try GET before judging the URL
                return CheckResult(
                    link=Link(url, 0, "link"), ok=exc.code < 400, status=exc.code
                )
            except urllib.error.URLError as exc:  # DNS failure, refused, timeout...
                last_error = str(exc.reason)
                break  # retry the whole URL, not the HEAD->GET ladder
            except (OSError, ValueError) as exc:
                last_error = str(exc)
                break
    return CheckResult(link=Link(url, 0, "link"), ok=False, error=last_error)


def _check_local(url: str, base_dir: str) -> CheckResult:
    clean = url.split("#", 1)[0].split("?", 1)[0]
    if not clean:
        return CheckResult(link=Link(url, 0, "link"), ok=True, note="anchor-only")
    path = os.path.normpath(os.path.join(base_dir, clean))
    ok = os.path.exists(path)
    return CheckResult(
        link=Link(url, 0, "link"),
        ok=ok,
        error=None if ok else f"file not found: {clean}",
    )


def check_file(path: str, config: Config) -> list[CheckResult]:
    """Check every link in one markdown file."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return [CheckResult(link=Link(path, 0, "link"), ok=False, error=str(exc))]

    results: list[CheckResult] = []
    externals: list[Link] = []
    for link in extract_links(text):
        url = link.url
        if is_skippable(url):
            continue
        if is_external(url):
            if config.check_external:
                externals.append(link)
        else:
            if config.check_local:
                results.append(_with_link(_check_local(url, os.path.dirname(os.path.abspath(path))), link))

    if externals:
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            checked = list(pool.map(lambda link: _check_http(link.url, config), externals))
        for link, res in zip(externals, checked):
            assert res is not None
            results.append(_with_link(res, link))

    results.sort(key=lambda r: (r.link.line, r.link.url))
    return results


def _with_link(result: CheckResult, link: Link) -> CheckResult:
    result.link = link
    return result


def check_paths(paths: Iterable[str], config: Config) -> dict[str, list[CheckResult]]:
    """Check a list of files/directories; directories are scanned recursively for *.md."""
    files: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                files.extend(
                    os.path.join(root, name)
                    for name in sorted(names)
                    if name.lower().endswith((".md", ".markdown"))
                )
        elif os.path.isfile(path):
            files.append(path)
        else:
            files.append(path)  # let check_file report the error
    return {path: check_file(path, config) for path in files}


def summarize(results: dict[str, list[CheckResult]]) -> tuple[int, int, int]:
    """Return (files_checked, total_links, broken_links)."""
    total = broken = 0
    for file_results in results.values():
        for res in file_results:
            if res.note == "excluded":
                continue
            total += 1
            if not res.ok:
                broken += 1
    return len(results), total, broken
