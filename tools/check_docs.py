"""Check repository-local Markdown links without fetching external resources."""

import argparse
import html
import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed Git read command, no shell
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

_INLINE = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
_REFERENCE = re.compile(r"!?\[([^\]\n]+)\](?:\[([^\]\n]*)\])?")
_DEFINITION = re.compile(r"^ {0,3}\[([^]]+)\]:\s*(<[^>]+>|\S+)")


def _source(path: Path) -> str:
    lines = []
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if not fence:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = ""
            continue
        if not fence:
            lines.append(re.sub(r"(`+).*?\1", "", line))
    return "\n".join(lines)


def _anchors(path: Path) -> set[str]:
    source = _source(path)
    anchors = set(re.findall(r'\bid=["\']([^"\']+)', source))
    counts: dict[str, int] = {}
    lines = source.splitlines()
    for index, line in enumerate(lines):
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        text = heading.group(1) if heading else None
        if (
            text is None
            and index + 1 < len(lines)
            and re.fullmatch(r" {0,3}(=+|-+)\s*", lines[index + 1])
        ):
            text = line.strip()
        if text is None:
            continue
        text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
        slug = re.sub(r"[^\w\- ]", "", html.unescape(re.sub(r"<[^>]*>", "", text)).lower())
        slug = slug.replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def _targets(path: Path) -> list[str]:
    source = _source(path)
    definitions = {
        m.group(1).casefold(): m.group(2).strip("<>")
        for line in source.splitlines()
        if (m := _DEFINITION.match(line))
    }
    source = "\n".join(line for line in source.splitlines() if not _DEFINITION.match(line))
    targets = [match.group(1).strip("<>") for match in _INLINE.finditer(source)]
    source = _INLINE.sub("", source)
    for match in _REFERENCE.finditer(source):
        key = (match.group(2) or match.group(1)).casefold()
        if key in definitions:
            targets.append(definitions[key])
    return targets


def _tracked(root: Path) -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise OSError("git_unavailable")
    # Fixed read-only argument vector; no shell and no target content executed.
    result = subprocess.run(  # nosec B603
        [git, "-C", str(root), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        check=True,
    )
    return [root / os.fsdecode(name) for name in result.stdout.split(b"\0") if name]


def main(argv: Sequence[str] | None = None) -> int:
    """Validate selected local document links and emit only path, target and reason.

    Args:
        argv: CLI arguments; defaults to process arguments.

    Returns:
        Zero for valid links, one for link failures, two for inspection failures.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true")
    parser.add_argument("--path", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    failures = 0
    try:
        paths = (_tracked(root) if args.tracked else []) + [root / path for path in args.path]
        if not paths:
            parser.error("select --tracked or --path")
        for document in sorted(set(paths)):
            document = document.resolve()
            if not document.is_relative_to(root):
                print(json.dumps({"path": str(document), "target": "", "reason": "outside_root"}))
                failures += 1
                continue
            for target in _targets(document):
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                destination = (
                    (document.parent / unquote(parsed.path)).resolve() if parsed.path else document
                )
                reason = None
                if not destination.is_relative_to(root):
                    reason = "outside_root"
                elif not destination.exists():
                    reason = "missing_file"
                elif re.fullmatch(r"L\d+(?:-L\d+)?", parsed.fragment):
                    pass
                elif parsed.fragment and destination.suffix.lower() == ".md":
                    if unquote(parsed.fragment) not in _anchors(destination):
                        reason = "missing_anchor"
                elif parsed.fragment and not re.fullmatch(r"L\d+(?:-L\d+)?", parsed.fragment):
                    reason = "unsupported_anchor"
                if reason:
                    print(
                        json.dumps(
                            {
                                "path": str(document.relative_to(root)),
                                "target": target,
                                "reason": reason,
                            }
                        )
                    )
                    failures += 1
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(json.dumps({"path": ".", "target": "", "reason": "inspection_failed"}))
        return 2
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
