"""Generate or check the production requirements export from the authored lock."""

import argparse
import subprocess  # nosec B404
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORT_COMMAND = (
    "uv",
    "export",
    "--frozen",
    "--no-default-groups",
    "--no-emit-project",
    "--format",
    "requirements.txt",
    "--no-header",
)


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the canonical hashed export, or check it without rewriting.

    Args:
        argv: Command arguments; omitted uses the process command line.

    Returns:
        Zero when generated/matching, one for drift, two for tool/I/O failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "requirements.runtime.txt")
    args = parser.parse_args(argv)
    try:
        # No user-controlled arguments or shell expansion enter this command.
        result = subprocess.run(  # nosec B603
            EXPORT_COMMAND, cwd=ROOT, capture_output=True, check=False
        )
        if result.returncode:
            print("Locked dependency export failed.", file=sys.stderr)
            return 2
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != result.stdout:
                print("Runtime export differs; regenerate with tools/dependency_export.py.")
                return 1
            return 0
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(result.stdout)
        return 0
    except OSError:
        print("Dependency export could not be read or written.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
